"""Row-level validation and duplicate-identity detection.

Field rules are declared as a Pandera schema whose checks are named after rule
codes. Row-level failures quarantine only the affected rows. Duplicate identity
groups are detected across every structurally readable row with a usable
identifier *before* invalid rows are filtered out, so a malformed sibling can
never make a conflicting identity look unique.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import pandas as pd
import pandera.pandas as pa

from reconcilekit.ingestion import COLUMNS, ParsedFile
from reconcilekit.models import (
    DuplicateGroup,
    Invoice,
    Payment,
    RuleCode,
    SourceRow,
    ValidationIssue,
)
from reconcilekit.money import AmountError, parse_amount_cents
from reconcilekit.normalization import normalize_currency, normalize_identifier, parse_iso_date

SUPPORTED_CURRENCY = "USD"


def _blank_ok(fn):
    """Wrap a format check so blank values pass (blanks are reported once, as missing)."""
    return lambda v: v.strip() == "" or fn(v)


def _is_amount(v: str) -> bool:
    try:
        parse_amount_cents(v)
    except AmountError:
        return False
    return True


_REQUIRED = pa.Check(
    lambda v: v.strip() != "", element_wise=True, name=RuleCode.MISSING_REQUIRED_VALUE.value
)
_ISO_DATE = pa.Check(
    _blank_ok(lambda v: parse_iso_date(v) is not None),
    element_wise=True,
    name=RuleCode.INVALID_DATE.value,
)
_USD = pa.Check(
    _blank_ok(lambda v: normalize_currency(v) == SUPPORTED_CURRENCY),
    element_wise=True,
    name=RuleCode.UNSUPPORTED_CURRENCY.value,
)
_AMOUNT = pa.Check(_blank_ok(_is_amount), element_wise=True, name=RuleCode.INVALID_AMOUNT.value)


def _col(*checks: pa.Check) -> pa.Column:
    return pa.Column(str, checks=list(checks), nullable=False)


SCHEMAS: dict[str, pa.DataFrameSchema] = {
    "invoices": pa.DataFrameSchema(
        {
            "invoice_id": _col(_REQUIRED),
            "customer_id": _col(_REQUIRED),
            "invoice_date": _col(_REQUIRED, _ISO_DATE),
            "due_date": _col(_REQUIRED, _ISO_DATE),
            "currency": _col(_REQUIRED, _USD),
            "invoice_amount": _col(_REQUIRED, _AMOUNT),
        },
        strict=False,
        name="invoice_register",
    ),
    "payments": pa.DataFrameSchema(
        {
            "payment_id": _col(_REQUIRED),
            "invoice_id": _col(),  # may be blank -> unmatched payment
            "customer_id": _col(_REQUIRED),
            "payment_date": _col(_REQUIRED, _ISO_DATE),
            "currency": _col(_REQUIRED, _USD),
            "payment_amount": _col(_REQUIRED, _AMOUNT),
        },
        strict=False,
        name="payment_ledger",
    ),
}

# Identity fields per file kind; duplicates are detected on their normalized values.
IDENTITY_FIELDS = {"invoices": ("customer_id", "invoice_id"), "payments": ("payment_id",)}


@dataclass(frozen=True)
class ValidationResult:
    parsed: ParsedFile
    eligible: tuple  # tuple[Invoice, ...] or tuple[Payment, ...]
    issues: tuple[ValidationIssue, ...]
    duplicate_groups: tuple[DuplicateGroup, ...]
    quarantined: frozenset[str]

    @property
    def kind(self) -> str:
        return self.parsed.info.kind


def _message(code: RuleCode, field: str, raw: str) -> str:
    if code is RuleCode.MISSING_REQUIRED_VALUE:
        return f"Required value '{field}' is blank."
    if code is RuleCode.INVALID_DATE:
        return f"'{raw}' in '{field}' is not a valid YYYY-MM-DD date."
    if code is RuleCode.UNSUPPORTED_CURRENCY:
        return f"Currency '{raw}' is not supported; v1 reconciles USD only."
    if code is RuleCode.INVALID_AMOUNT:
        try:
            parse_amount_cents(raw)
        except AmountError as exc:
            return f"Invalid '{field}': {exc}"
    return f"'{field}' failed {code.value}."  # pragma: no cover - defensive


def _schema_issues(parsed: ParsedFile) -> list[ValidationIssue]:
    frame = parsed.frame
    if frame.empty:
        return []
    try:
        SCHEMAS[parsed.info.kind].validate(frame, lazy=True)
        return []
    except pa.errors.SchemaErrors as exc:
        failures = exc.failure_cases
    issues = []
    for case in failures.to_dict("records"):
        if case["check"] not in RuleCode.__members__ or pd.isna(case["index"]):
            # Only our named element-wise checks are expected; anything else would be a
            # dtype/column failure that ingestion already prevents.
            raise RuntimeError(f"Unexpected schema failure: {case}")  # pragma: no cover
        row = frame.iloc[int(case["index"])]
        code = RuleCode(case["check"])
        column = str(case["column"])
        raw = str(row[column])
        issues.append(
            ValidationIssue(
                record_id=str(row["record_id"]),
                kind=parsed.info.kind,
                source_row=int(row["source_row"]),
                rule_code=code,
                field=column,
                raw_value=raw,
                message=_message(code, column, raw),
            )
        )
    return issues


def _cross_field_issues(
    rows: tuple[SourceRow, ...], bad_fields: dict[str, set[str]], kind: str, as_of: date
) -> list[ValidationIssue]:
    """Rules that need several parsed fields; only applied when those fields parsed."""
    issues = []
    date_field = "invoice_date" if kind == "invoices" else "payment_date"
    for row in rows:
        bad = bad_fields.get(row.record_id, set())
        if date_field not in bad:
            txn_date = parse_iso_date(row.raw[date_field])
            if txn_date is not None and txn_date > as_of:
                issues.append(
                    ValidationIssue(
                        row.record_id,
                        kind,
                        row.source_row,
                        RuleCode.TRANSACTION_AFTER_AS_OF,
                        date_field,
                        row.raw[date_field],
                        f"{date_field} {txn_date.isoformat()} is after the as-of date "
                        f"{as_of.isoformat()}; inputs are treated as a snapshot at the as-of date.",
                    )
                )
        if kind == "invoices" and not bad & {"invoice_date", "due_date"}:
            inv_date = parse_iso_date(row.raw["invoice_date"])
            due = parse_iso_date(row.raw["due_date"])
            if inv_date and due and due < inv_date:
                issues.append(
                    ValidationIssue(
                        row.record_id,
                        kind,
                        row.source_row,
                        RuleCode.DUE_DATE_BEFORE_INVOICE_DATE,
                        "due_date",
                        row.raw["due_date"],
                        f"due_date {due.isoformat()} is before invoice_date "
                        f"{inv_date.isoformat()}.",
                    )
                )
    return issues


def _comparable(row: SourceRow, kind: str) -> dict[str, str]:
    """Contract values after the same permitted normalization used for matching."""
    out = {}
    for col in COLUMNS[kind]:
        value = row.raw[col]
        out[col] = normalize_currency(value) if col == "currency" else value.strip()
    return out


def find_duplicate_groups(rows: tuple[SourceRow, ...], kind: str) -> list[DuplicateGroup]:
    fields = IDENTITY_FIELDS[kind]
    groups: dict[tuple[str, ...], list[SourceRow]] = defaultdict(list)
    for row in rows:
        identity = tuple(normalize_identifier(row.raw[f]) for f in fields)
        if all(identity):  # rows without a usable identifier cannot form an identity
            groups[identity].append(row)
    result = []
    for identity, members in groups.items():
        if len(members) < 2:
            continue
        values = [_comparable(m, kind) for m in members]
        differing = tuple(c for c in COLUMNS[kind] if len({v[c] for v in values}) > 1)
        result.append(
            DuplicateGroup(
                kind=kind,
                identity=identity,
                record_ids=tuple(m.record_id for m in members),
                source_rows=tuple(m.source_row for m in members),
                identical=not differing,
                differing_fields=differing,
            )
        )
    return sorted(result, key=lambda g: g.source_rows)


def _build_invoice(row: SourceRow) -> Invoice:
    r = row.raw
    return Invoice(
        record_id=row.record_id,
        source_row=row.source_row,
        invoice_id_raw=r["invoice_id"],
        invoice_id=normalize_identifier(r["invoice_id"]),
        customer_id_raw=r["customer_id"],
        customer_id=normalize_identifier(r["customer_id"]),
        invoice_date=parse_iso_date(r["invoice_date"]),  # type: ignore[arg-type]
        due_date=parse_iso_date(r["due_date"]),  # type: ignore[arg-type]
        currency=normalize_currency(r["currency"]),
        amount_cents=parse_amount_cents(r["invoice_amount"]),
    )


def _build_payment(row: SourceRow) -> Payment:
    r = row.raw
    return Payment(
        record_id=row.record_id,
        source_row=row.source_row,
        payment_id_raw=r["payment_id"],
        payment_id=normalize_identifier(r["payment_id"]),
        invoice_id_raw=r["invoice_id"],
        invoice_id=normalize_identifier(r["invoice_id"]) or None,
        customer_id_raw=r["customer_id"],
        customer_id=normalize_identifier(r["customer_id"]),
        payment_date=parse_iso_date(r["payment_date"]),  # type: ignore[arg-type]
        currency=normalize_currency(r["currency"]),
        amount_cents=parse_amount_cents(r["payment_amount"]),
    )


def validate(parsed: ParsedFile, as_of: date) -> ValidationResult:
    kind = parsed.info.kind
    issues = _schema_issues(parsed)
    bad_fields: dict[str, set[str]] = defaultdict(set)
    for i in issues:
        bad_fields[i.record_id].add(i.field or "")
    issues += _cross_field_issues(parsed.rows, bad_fields, kind, as_of)
    issues.sort(key=lambda i: (i.source_row, COLUMNS[kind].index(i.field) if i.field else -1))

    groups = find_duplicate_groups(parsed.rows, kind)
    quarantined = {i.record_id for i in issues} | {r for g in groups for r in g.record_ids}
    build = _build_invoice if kind == "invoices" else _build_payment
    eligible = tuple(build(row) for row in parsed.rows if row.record_id not in quarantined)
    return ValidationResult(parsed, eligible, tuple(issues), tuple(groups), frozenset(quarantined))
