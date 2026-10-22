"""The reconciliation engine: explicit, exact-reference matching in integer cents.

This module has no UI or database dependencies. ``reconcile_files`` is the single
entry point used by both the CLI and the Streamlit app.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from reconcilekit import ENGINE_VERSION, RULE_VERSION
from reconcilekit.ingestion import FileValidationError, ParsedFile, parse_csv, safe_label
from reconcilekit.models import (
    QUARANTINED,
    DuplicateGroup,
    ExceptionCategory,
    ExceptionRecord,
    Invoice,
    InvoiceResult,
    InvoiceStatus,
    Payment,
    PaymentResult,
    PaymentStatus,
    RuleCode,
    Severity,
    SourceInfo,
    SourceRow,
    Summary,
    ValidationIssue,
)
from reconcilekit.money import format_usd
from reconcilekit.normalization import comparison_hint_key, normalize_identifier
from reconcilekit.validation import ValidationResult, validate

# The rule configuration is fixed in v1 but is recorded and fingerprinted so that a
# future configurable rule would automatically produce a distinct run.
RULE_CONFIG: dict[str, object] = {
    "allocation": "exact_reference_only",
    "amount_tolerance_cents": 0,
    "currency": "USD",
    "identifier_normalization": "trim_surrounding_whitespace",
    "invoice_identity": ["customer_id", "invoice_id"],
    "payment_identity": ["payment_id"],
    "unpaid_exception_only_when_overdue": True,
}


class InvariantViolation(Exception):
    """A financial invariant failed; the run must not be presented as successful."""


@dataclass(frozen=True)
class ReconciliationRun:
    run_id: str
    fingerprint: str
    as_of: date
    config: dict[str, object]
    engine_version: str
    rule_version: str
    invoice_source: SourceInfo
    payment_source: SourceInfo
    source_rows: tuple[SourceRow, ...]
    validation_issues: tuple[ValidationIssue, ...]
    duplicate_groups: tuple[DuplicateGroup, ...]
    invoice_results: tuple[InvoiceResult, ...]
    payment_results: tuple[PaymentResult, ...]
    exceptions: tuple[ExceptionRecord, ...]
    summary: Summary


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compute_fingerprint(
    invoice_sha256: str,
    payment_sha256: str,
    as_of: date,
    config: dict[str, object] = RULE_CONFIG,
    engine_version: str = ENGINE_VERSION,
    rule_version: str = RULE_VERSION,
) -> str:
    """Deterministic identity of a logical run (byte-identical inputs + same settings)."""
    payload = canonical_json(
        {
            "as_of": as_of.isoformat(),
            "config": config,
            "engine_version": engine_version,
            "invoice_sha256": invoice_sha256,
            "payment_sha256": payment_sha256,
            "rule_version": rule_version,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_id_for(fingerprint: str) -> str:
    return f"RUN-{fingerprint[:16]}"


# --------------------------------------------------------------------------- matching


@dataclass(frozen=True)
class _InvoiceIndex:
    """Lookups over ALL invoice rows with a usable identity, valid or not."""

    rows_by_key: dict[tuple[str, str], list[SourceRow]]
    eligible_by_key: dict[tuple[str, str], Invoice]
    customers_by_invoice_id: dict[str, set[str]]
    ids_by_customer_hint: dict[tuple[str, str], set[str]]
    issues_by_record: dict[str, list[ValidationIssue]]


def _build_index(inv: ValidationResult) -> _InvoiceIndex:
    rows_by_key: dict[tuple[str, str], list[SourceRow]] = defaultdict(list)
    customers: dict[str, set[str]] = defaultdict(set)
    hints: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in inv.parsed.rows:
        cust = normalize_identifier(row.raw["customer_id"])
        inv_id = normalize_identifier(row.raw["invoice_id"])
        if cust and inv_id:
            rows_by_key[(cust, inv_id)].append(row)
            customers[inv_id].add(cust)
            hints[(cust, comparison_hint_key(inv_id))].add(inv_id)
    issues: dict[str, list[ValidationIssue]] = defaultdict(list)
    for issue in inv.issues:
        issues[issue.record_id].append(issue)
    return _InvoiceIndex(rows_by_key, {i.key: i for i in inv.eligible}, customers, hints, issues)


def _rows_text(kind: str, rows: list[int] | tuple[int, ...]) -> str:
    label = "row" if len(rows) == 1 else "rows"
    return f"{kind} {label} {', '.join(str(r) for r in rows)}"


def _normalization_note(p: Payment) -> str:
    changed = []
    if (p.invoice_id or "") != p.invoice_id_raw:
        changed.append(f"invoice_id {p.invoice_id_raw!r}")
    if p.customer_id != p.customer_id_raw:
        changed.append(f"customer_id {p.customer_id_raw!r}")
    if p.payment_id != p.payment_id_raw:
        changed.append(f"payment_id {p.payment_id_raw!r}")
    if not changed:
        return ""
    return (
        " Permitted normalization was applied: surrounding whitespace was trimmed from "
        + ", ".join(changed)
        + "."
    )


def _classify_payment(
    p: Payment, idx: _InvoiceIndex
) -> tuple[PaymentStatus, RuleCode, Invoice | None, tuple[str, ...], str]:
    """Deterministic decision order; the first matching rule wins.

    1. Blank reference                         -> UNMATCHED / PAYMENT_REFERENCE_MISSING
    2. Identity exists on more than one row    -> AMBIGUOUS / AMBIGUOUS_INVOICE_REFERENCE
    3. Identity exists on one quarantined row  -> BLOCKED   / REFERENCED_INVOICE_INVALID
    4. Identity exists on one eligible row     -> APPLIED   / EXACT_REFERENCE_MATCH
    5. invoice_id exists for other customer(s) -> UNMATCHED / CUSTOMER_REFERENCE_CONFLICT
    6. Otherwise                               -> UNMATCHED / PAYMENT_REFERENCE_NOT_FOUND
    """
    if p.invoice_id is None:
        return (
            PaymentStatus.UNMATCHED,
            RuleCode.PAYMENT_REFERENCE_MISSING,
            None,
            (),
            "Payment has no invoice reference. ReconcileKit never allocates a payment by "
            "amount or date, so it is unmatched and needs review.",
        )
    key = (p.customer_id, p.invoice_id)
    rows = idx.rows_by_key.get(key, [])
    ident = f"customer {p.customer_id!r} / invoice {p.invoice_id!r}"
    if len(rows) > 1:
        return (
            PaymentStatus.AMBIGUOUS,
            RuleCode.AMBIGUOUS_INVOICE_REFERENCE,
            None,
            tuple(r.record_id for r in rows),
            f"The referenced invoice identity ({ident}) appears on "
            f"{_rows_text('invoices', [r.source_row for r in rows])}, so the payment "
            "cannot be allocated to a single invoice. All rows in that duplicate group "
            "were quarantined.",
        )
    if len(rows) == 1 and key not in idx.eligible_by_key:
        row = rows[0]
        codes = sorted({i.rule_code.value for i in idx.issues_by_record.get(row.record_id, [])})
        return (
            PaymentStatus.BLOCKED,
            RuleCode.REFERENCED_INVOICE_INVALID,
            None,
            (row.record_id,),
            f"The referenced invoice ({ident}, invoices row {row.source_row}) exists but was "
            f"quarantined ({', '.join(codes)}). The payment is held for review and not "
            "applied until the invoice data is corrected and re-imported.",
        )
    if len(rows) == 1:
        invoice = idx.eligible_by_key[key]
        return (
            PaymentStatus.APPLIED,
            RuleCode.EXACT_REFERENCE_MATCH,
            invoice,
            (invoice.record_id,),
            f"Exact match on customer_id and invoice_id to invoices row {invoice.source_row}."
            + _normalization_note(p),
        )
    owners = sorted(idx.customers_by_invoice_id.get(p.invoice_id, set()))
    if owners:
        related = tuple(r.record_id for o in owners for r in idx.rows_by_key[(o, p.invoice_id)])
        return (
            PaymentStatus.UNMATCHED,
            RuleCode.CUSTOMER_REFERENCE_CONFLICT,
            None,
            related,
            f"Invoice {p.invoice_id!r} exists only for customer(s) "
            f"{', '.join(repr(o) for o in owners)}, not for payment customer "
            f"{p.customer_id!r}. Cross-customer references are never matched automatically.",
        )
    near = sorted(
        idx.ids_by_customer_hint.get((p.customer_id, comparison_hint_key(p.invoice_id)), set())
    )
    hint = ""
    if near:
        hint = (
            f" Note: invoice {', '.join(repr(n) for n in near)} exists for this customer and "
            "differs only by letter case or punctuation. It was not matched because v1 "
            "requires exact references."
        )
    return (
        PaymentStatus.UNMATCHED,
        RuleCode.PAYMENT_REFERENCE_NOT_FOUND,
        None,
        (),
        f"No invoice with {ident} exists in the invoice register." + hint,
    )


def _invoice_result(inv: Invoice, payments: list[Payment], as_of: date) -> InvoiceResult:
    applied = sum(p.amount_cents for p in payments)
    outstanding = max(inv.amount_cents - applied, 0)
    overpayment = max(applied - inv.amount_cents, 0)
    if applied == 0:
        status, rule = InvoiceStatus.UNPAID, RuleCode.INVOICE_UNPAID
    elif applied < inv.amount_cents:
        status, rule = InvoiceStatus.PARTIALLY_PAID, RuleCode.INVOICE_PARTIALLY_PAID
    elif applied == inv.amount_cents:
        status, rule = InvoiceStatus.PAID, RuleCode.INVOICE_PAID
    else:
        status, rule = InvoiceStatus.OVERPAID, RuleCode.INVOICE_OVERPAID
    is_overdue = outstanding > 0 and inv.due_date < as_of
    days_overdue = (as_of - inv.due_date).days if is_overdue else 0

    pay_ids = ", ".join(p.payment_id for p in payments)
    n = len(payments)
    via = f"{n} payment{'s' if n != 1 else ''} ({pay_ids})"
    if status is InvoiceStatus.UNPAID:
        text = "No eligible payment references this invoice."
    elif status is InvoiceStatus.PAID:
        text = f"Applied {format_usd(applied)} from {via} exactly equals the invoice amount."
    elif status is InvoiceStatus.PARTIALLY_PAID:
        text = (
            f"Applied {format_usd(applied)} from {via} against {format_usd(inv.amount_cents)}; "
            f"{format_usd(outstanding)} remains outstanding."
        )
    else:
        text = (
            f"Applied {format_usd(applied)} from {via} exceeds the invoice amount "
            f"{format_usd(inv.amount_cents)} by {format_usd(overpayment)}."
        )
    if is_overdue:
        text += f" Overdue by {days_overdue} days as of {as_of.isoformat()}."
    elif outstanding > 0:
        text += f" Not overdue: due {inv.due_date.isoformat()}."
    if inv.normalized:
        text += " Invoice identifiers had surrounding whitespace trimmed."
    return InvoiceResult(
        record_id=inv.record_id,
        source_row=inv.source_row,
        invoice_id_raw=inv.invoice_id_raw,
        invoice_id=inv.invoice_id,
        customer_id_raw=inv.customer_id_raw,
        customer_id=inv.customer_id,
        invoice_date=inv.invoice_date,
        due_date=inv.due_date,
        invoice_amount_cents=inv.amount_cents,
        applied_payment_cents=applied,
        outstanding_cents=outstanding,
        overpayment_cents=overpayment,
        status=status,
        rule_code=rule,
        is_overdue=is_overdue,
        days_overdue=days_overdue,
        payment_record_ids=tuple(p.record_id for p in payments),
        normalization_applied=inv.normalized,
        explanation=text,
    )


# --------------------------------------------------------------------------- exceptions


def _validation_exceptions(v: ValidationResult) -> list[tuple[int, ExceptionRecord]]:
    """One exception per duplicate group and one per row with field-level issues."""
    kind = v.kind
    id_field = "invoice_id" if kind == "invoices" else "payment_id"
    rows = {r.record_id: r for r in v.parsed.rows}
    in_group = {rid: g for g in v.duplicate_groups for rid in g.record_ids}
    out: list[tuple[int, ExceptionRecord]] = []

    for g in v.duplicate_groups:
        first = rows[g.record_ids[0]]
        if kind == "invoices":
            code = RuleCode.DUPLICATE_INVOICE_IDENTITY
            ident = f"customer {g.identity[0]!r} / invoice {g.identity[1]!r}"
        else:
            code = RuleCode.DUPLICATE_PAYMENT_ID
            ident = f"payment_id {g.identity[0]!r}"
        nature = (
            "The rows are identical duplicates."
            if g.identical
            else f"The rows conflict on: {', '.join(g.differing_fields)}."
        )
        out.append(
            (
                g.source_rows[0],
                ExceptionRecord(
                    exception_id="",
                    category=ExceptionCategory.VALIDATION,
                    rule_code=code,
                    severity=Severity.QUARANTINE,
                    classification=QUARANTINED,
                    customer_id=normalize_identifier(first.raw["customer_id"]) or None,
                    invoice_id=normalize_identifier(first.raw["invoice_id"]) or None,
                    payment_id=g.identity[0] if kind == "payments" else None,
                    record_ids=g.record_ids,
                    source_refs=_rows_text(kind, g.source_rows),
                    amount_cents=None,
                    explanation=(
                        f"{ident} appears on {len(g.record_ids)} rows. {nature} No row was "
                        "chosen arbitrarily: every row in the group is quarantined and "
                        "excluded from eligible totals."
                    ),
                ),
            )
        )

    by_record: dict[str, list[ValidationIssue]] = defaultdict(list)
    for issue in v.issues:
        by_record[issue.record_id].append(issue)
    for record_id, issues in by_record.items():
        row = rows[record_id]
        text = " ".join(i.message for i in issues)
        if record_id in in_group:
            text += " This row is also part of a duplicate identity group."
        text += " The row is quarantined and excluded from eligible totals."
        out.append(
            (
                row.source_row,
                ExceptionRecord(
                    exception_id="",
                    category=ExceptionCategory.VALIDATION,
                    rule_code=issues[0].rule_code,
                    severity=Severity.QUARANTINE,
                    classification=QUARANTINED,
                    customer_id=normalize_identifier(row.raw["customer_id"]) or None,
                    invoice_id=normalize_identifier(row.raw["invoice_id"]) or None,
                    payment_id=(
                        normalize_identifier(row.raw[id_field]) or None
                        if kind == "payments"
                        else None
                    ),
                    record_ids=(record_id,),
                    source_refs=_rows_text(kind, [row.source_row]),
                    # Quarantined amounts are never presented as USD money; the raw
                    # value is available in the evidence and validation report.
                    amount_cents=None,
                    explanation=text,
                ),
            )
        )
    return sorted(out, key=lambda t: t[0])


def _invoice_exception(r: InvoiceResult) -> ExceptionRecord | None:
    if r.status is InvoiceStatus.PAID:
        return None
    if r.status is InvoiceStatus.UNPAID and not r.is_overdue:
        return None  # not yet due: reported in results, not queued for review
    amount = r.overpayment_cents if r.status is InvoiceStatus.OVERPAID else r.outstanding_cents
    return ExceptionRecord(
        exception_id="",
        category=ExceptionCategory.INVOICE,
        rule_code=r.rule_code,
        severity=Severity.REVIEW,
        classification=r.status.value,
        customer_id=r.customer_id,
        invoice_id=r.invoice_id,
        payment_id=None,
        record_ids=(r.record_id, *r.payment_record_ids),
        source_refs=_rows_text("invoices", [r.source_row]),
        amount_cents=amount,
        explanation=r.explanation,
    )


def _payment_exceptions(r: PaymentResult) -> list[ExceptionRecord]:
    out = []
    common = {
        "customer_id": r.customer_id,
        "invoice_id": r.invoice_id,
        "payment_id": r.payment_id,
        "record_ids": (r.record_id, *r.related_invoice_record_ids),
        "source_refs": _rows_text("payments", [r.source_row]),
        "amount_cents": r.payment_amount_cents,
        "classification": r.status.value,
    }
    if r.status is not PaymentStatus.APPLIED:
        out.append(
            ExceptionRecord(
                exception_id="",
                category=ExceptionCategory.PAYMENT,
                rule_code=r.rule_code,
                severity=Severity.REVIEW,
                explanation=r.explanation,
                **common,
            )
        )
    if RuleCode.PAYMENT_BEFORE_INVOICE_DATE in r.warnings:
        out.append(
            ExceptionRecord(
                exception_id="",
                category=ExceptionCategory.PAYMENT,
                rule_code=RuleCode.PAYMENT_BEFORE_INVOICE_DATE,
                severity=Severity.INFO,
                explanation=(
                    "Informational: the payment is dated before the invoice date. The exact "
                    "reference still governs allocation, so the amount remains applied."
                ),
                **common,
            )
        )
    return out


def _duplicate_member_issues(v: ValidationResult) -> tuple[ValidationIssue, ...]:
    """Per-row quarantine reasons for duplicate-group members (for the validation report)."""
    rows = {r.record_id: r for r in v.parsed.rows}
    field = "invoice_id" if v.kind == "invoices" else "payment_id"
    code = (
        RuleCode.DUPLICATE_INVOICE_IDENTITY
        if v.kind == "invoices"
        else RuleCode.DUPLICATE_PAYMENT_ID
    )
    out = []
    for g in v.duplicate_groups:
        nature = "identical" if g.identical else f"conflicting on {', '.join(g.differing_fields)}"
        for rid in g.record_ids:
            row = rows[rid]
            out.append(
                ValidationIssue(
                    rid,
                    v.kind,
                    row.source_row,
                    code,
                    field,
                    row.raw[field],
                    f"Identity {' / '.join(g.identity)!r} is shared by "
                    f"{_rows_text(v.kind, g.source_rows)} ({nature}); all rows quarantined.",
                )
            )
    return tuple(out)


def _quarantine_report(v: ValidationResult) -> tuple[ValidationIssue, ...]:
    return tuple(sorted(v.issues + _duplicate_member_issues(v), key=lambda i: i.source_row))


# --------------------------------------------------------------------------- invariants


def check_invariants(
    invoices: tuple[InvoiceResult, ...], payments: tuple[PaymentResult, ...]
) -> dict[str, int]:
    """Compute I, A, O, X, P, U, M, B and verify the reconciliation bridge."""
    by_status: dict[PaymentStatus, int] = defaultdict(int)
    for p in payments:
        by_status[p.status] += p.payment_amount_cents
    t = {
        "I": sum(r.invoice_amount_cents for r in invoices),
        "A": sum(r.applied_payment_cents for r in invoices),
        "O": sum(r.outstanding_cents for r in invoices),
        "X": sum(r.overpayment_cents for r in invoices),
        "P": sum(p.payment_amount_cents for p in payments),
        "U": by_status[PaymentStatus.UNMATCHED],
        "M": by_status[PaymentStatus.AMBIGUOUS],
        "B": by_status[PaymentStatus.BLOCKED],
    }
    failures = []
    for r in invoices:
        if r.outstanding_cents < 0 or r.overpayment_cents < 0:
            failures.append(f"{r.record_id}: negative outstanding/overpayment")
        if r.invoice_amount_cents - r.applied_payment_cents != (
            r.outstanding_cents - r.overpayment_cents
        ):
            failures.append(f"{r.record_id}: amount - applied != outstanding - overpayment")
    if t["A"] != by_status[PaymentStatus.APPLIED]:
        failures.append("applied total differs between invoice and payment views")
    if t["I"] - t["A"] != t["O"] - t["X"]:
        failures.append("I - A != O - X")
    if t["P"] != t["A"] + t["U"] + t["M"] + t["B"]:
        failures.append("P != A + U + M + B")
    if t["I"] - t["P"] != t["O"] - t["X"] - t["U"] - t["M"] - t["B"]:
        failures.append("I - P != O - X - U - M - B")
    applied_ids = [pid for r in invoices for pid in r.payment_record_ids]
    if len(applied_ids) != len(set(applied_ids)):
        failures.append("a payment was applied to more than one invoice")
    if failures:
        raise InvariantViolation("; ".join(failures))
    return t


# --------------------------------------------------------------------------- entry points


def reconcile(inv_v: ValidationResult, pay_v: ValidationResult, as_of: date) -> ReconciliationRun:
    idx = _build_index(inv_v)

    applied: dict[tuple[str, str], list[Payment]] = defaultdict(list)
    payment_results: list[PaymentResult] = []
    for p in sorted(pay_v.eligible, key=lambda x: x.source_row):
        status, rule, invoice, related, text = _classify_payment(p, idx)
        warnings: tuple[RuleCode, ...] = ()
        if invoice is not None:
            applied[invoice.key].append(p)
            if p.payment_date < invoice.invoice_date:
                warnings = (RuleCode.PAYMENT_BEFORE_INVOICE_DATE,)
                text += (
                    f" Payment date {p.payment_date.isoformat()} is before the invoice date "
                    f"{invoice.invoice_date.isoformat()} (informational only)."
                )
        payment_results.append(
            PaymentResult(
                record_id=p.record_id,
                source_row=p.source_row,
                payment_id_raw=p.payment_id_raw,
                payment_id=p.payment_id,
                invoice_id_raw=p.invoice_id_raw,
                invoice_id=p.invoice_id,
                customer_id_raw=p.customer_id_raw,
                customer_id=p.customer_id,
                payment_date=p.payment_date,
                payment_amount_cents=p.amount_cents,
                status=status,
                rule_code=rule,
                matched_invoice_record_id=invoice.record_id if invoice else None,
                related_invoice_record_ids=related,
                warnings=warnings,
                normalization_applied=p.normalized,
                explanation=text,
            )
        )

    invoice_results = tuple(
        _invoice_result(inv, applied.get(inv.key, []), as_of)
        for inv in sorted(inv_v.eligible, key=lambda x: x.source_row)
    )
    payment_tuple = tuple(payment_results)
    totals = check_invariants(invoice_results, payment_tuple)

    raw_exceptions: list[ExceptionRecord] = [e for _, e in _validation_exceptions(inv_v)]
    raw_exceptions += [e for _, e in _validation_exceptions(pay_v)]
    raw_exceptions += [e for r in invoice_results if (e := _invoice_exception(r))]
    raw_exceptions += [e for r in payment_tuple for e in _payment_exceptions(r)]
    exceptions = tuple(
        replace(e, exception_id=f"EX-{n:04d}") for n, e in enumerate(raw_exceptions, start=1)
    )

    inv_counts: dict[str, int] = {s.value: 0 for s in InvoiceStatus}
    for r in invoice_results:
        inv_counts[r.status.value] += 1
    pay_counts: dict[str, int] = {s.value: 0 for s in PaymentStatus}
    for r in payment_tuple:
        pay_counts[r.status.value] += 1
    overdue = [r for r in invoice_results if r.is_overdue]
    summary = Summary(
        eligible_invoice_cents=totals["I"],
        applied_payment_cents=totals["A"],
        outstanding_cents=totals["O"],
        overpayment_cents=totals["X"],
        eligible_payment_cents=totals["P"],
        unmatched_payment_cents=totals["U"],
        ambiguous_payment_cents=totals["M"],
        blocked_payment_cents=totals["B"],
        overdue_cents=sum(r.outstanding_cents for r in overdue),
        invoice_rows=inv_v.parsed.info.row_count,
        payment_rows=pay_v.parsed.info.row_count,
        eligible_invoices=len(invoice_results),
        eligible_payments=len(payment_tuple),
        quarantined_invoice_rows=len(inv_v.quarantined),
        quarantined_payment_rows=len(pay_v.quarantined),
        invoice_status_counts=inv_counts,
        payment_status_counts=pay_counts,
        overdue_invoices=len(overdue),
        exception_count=len(exceptions),
    )

    fingerprint = compute_fingerprint(inv_v.parsed.info.sha256, pay_v.parsed.info.sha256, as_of)
    return ReconciliationRun(
        run_id=run_id_for(fingerprint),
        fingerprint=fingerprint,
        as_of=as_of,
        config=dict(RULE_CONFIG),
        engine_version=ENGINE_VERSION,
        rule_version=RULE_VERSION,
        invoice_source=inv_v.parsed.info,
        payment_source=pay_v.parsed.info,
        source_rows=inv_v.parsed.rows + pay_v.parsed.rows,
        validation_issues=_quarantine_report(inv_v) + _quarantine_report(pay_v),
        duplicate_groups=inv_v.duplicate_groups + pay_v.duplicate_groups,
        invoice_results=invoice_results,
        payment_results=payment_tuple,
        exceptions=exceptions,
        summary=summary,
    )


def parse_inputs(
    invoice_bytes: bytes,
    payment_bytes: bytes,
    invoice_label: str | None = None,
    payment_label: str | None = None,
) -> tuple[ParsedFile, ParsedFile]:
    """Parse both files, reporting file-level problems for both at once."""
    problems: dict[str, list[str]] = {}
    parsed: dict[str, ParsedFile] = {}
    for kind, data, label in (
        ("invoices", invoice_bytes, invoice_label),
        ("payments", payment_bytes, payment_label),
    ):
        try:
            parsed[kind] = parse_csv(data, kind, safe_label(label, f"{kind}.csv"))
        except FileValidationError as exc:
            problems.update(exc.problems)
    if problems:
        raise FileValidationError(problems)
    return parsed["invoices"], parsed["payments"]


def reconcile_files(
    invoice_bytes: bytes,
    payment_bytes: bytes,
    as_of: date,
    invoice_label: str | None = None,
    payment_label: str | None = None,
) -> ReconciliationRun:
    """Full pipeline: parse -> validate -> reconcile -> verify invariants.

    Raises FileValidationError for file-level problems and InvariantViolation if the
    financial bridge does not hold.
    """
    inv_file, pay_file = parse_inputs(invoice_bytes, payment_bytes, invoice_label, payment_label)
    return reconcile(validate(inv_file, as_of), validate(pay_file, as_of), as_of)
