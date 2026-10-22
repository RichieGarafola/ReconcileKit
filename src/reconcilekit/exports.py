"""Reconciliation package export (ZIP of CSV + JSON), built from persisted data.

Money is written as exact two-decimal strings derived from integer cents.
Spreadsheet-facing CSV text fields are protected against formula injection:
any text cell that starts with a control or invisible format character (tab,
CR, LF, zero-width space, ...), or whose first non-whitespace character is one
of ``= + - @`` (or their full-width forms), is prefixed with a single quote (').
Stored data is never modified; escaping applies to the exported CSV only.
"""

from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from reconcilekit import reporting
from reconcilekit.money import format_cents
from reconcilekit.repository import connect, utc_now

# ASCII prefixes plus full-width forms that some spreadsheet locales also evaluate.
FORMULA_PREFIXES = ("=", "+", "-", "@", *(chr(c) for c in (0xFF1D, 0xFF0B, 0xFF0D, 0xFF20)))
PACKAGE_FILES = (
    "summary.json",
    "invoice_results.csv",
    "payment_results.csv",
    "exceptions.csv",
    "validation_errors.csv",
    "review_history.csv",
    "manifest.json",
    "README.txt",
)


class ExportError(LookupError):
    pass


def escape_formula(value: object) -> object:
    """Neutralize spreadsheet formula prefixes in an untrusted text cell."""
    if not isinstance(value, str) or value == "":
        return value
    # Leading control/format characters (tab, CR, LF, NUL, zero-width space, ...) are
    # escaped outright; otherwise check the first non-whitespace character.
    if unicodedata.category(value[0]) in ("Cc", "Cf") or value.lstrip().startswith(
        FORMULA_PREFIXES
    ):
        return "'" + value
    return value


def escape_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Presentation copy with ``escape_formula`` applied to every string cell.

    Used for on-screen grids whose built-in CSV download cannot be disabled. The
    input frame (and therefore stored data) is left untouched.
    """
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = [escape_formula(v) for v in out[col]]
    return out


def _cell(value: object) -> object:
    """Render one stored value as CSV text (lists are ';'-joined, booleans lowercase)."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return ";".join(str(v) for v in value)
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _to_csv(df: pd.DataFrame, money_columns: dict[str, str]) -> bytes:
    """Format money columns (cents -> 'd.dd'), escape all text, and serialize."""
    out = pd.DataFrame(index=df.index)
    for col in df.columns:
        if col in money_columns:
            if not pd.api.types.is_integer_dtype(df[col]) and not df[col].isna().all():
                # Cents must arrive as integers; a float column could already have lost cents.
                raise TypeError(f"Money column {col!r} has non-integer dtype {df[col].dtype}.")
            out[money_columns[col]] = ["" if pd.isna(v) else format_cents(int(v)) for v in df[col]]
        else:
            out[col] = [escape_formula(_cell(v)) for v in df[col]]
    return out.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _dates_to_text(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    df = df.copy()
    for col in columns:
        df[col] = [pd.Timestamp(v).date().isoformat() for v in df[col]]
    return df


def _money_summary(run: dict) -> dict[str, dict[str, object]]:
    keys = {
        "I_eligible_invoices": "eligible_invoice_cents",
        "A_applied_payments": "applied_payment_cents",
        "O_outstanding": "outstanding_cents",
        "X_overpayments": "overpayment_cents",
        "P_eligible_payments": "eligible_payment_cents",
        "U_unmatched_payments": "unmatched_payment_cents",
        "M_ambiguous_payments": "ambiguous_payment_cents",
        "B_blocked_payments": "blocked_payment_cents",
        "overdue_outstanding": "overdue_cents",
    }
    totals = {name: run[col] for name, col in keys.items()}
    totals["net_difference_I_minus_P"] = (
        run["eligible_invoice_cents"] - run["eligible_payment_cents"]
    )
    return {k: {"amount": format_cents(v), "cents": v} for k, v in totals.items()}


_README = """ReconcileKit reconciliation package
===================================

Run: {run_id}
As-of date: {as_of}
Exported (UTC): {exported_at}
Data completeness: {completeness}

Files
-----
summary.json           Run metadata, counts, monetary totals, invariant checks, and
                       exception disposition counts at export time.
invoice_results.csv    One row per ELIGIBLE invoice with applied/outstanding/overpaid amounts.
payment_results.csv    One row per ELIGIBLE payment with APPLIED/UNMATCHED/AMBIGUOUS/BLOCKED.
exceptions.csv         Immutable automated exceptions plus the current review disposition.
validation_errors.csv  Every quarantine reason for every excluded source row.
review_history.csv     Append-only review events in chronological order.
manifest.json          Source file hashes, versions, and SHA-256 of each file in this package.

Conventions
-----------
* Money columns are exact decimal strings with two places (from integer cents), USD.
* Identifiers are written as text exactly as stored, so the file bytes keep leading zeros
  (e.g. 000123). CSV carries no column types: spreadsheet programs may convert such
  columns to numbers when opening the file. Import identifier columns as Text.
* Text cells that start with a control/invisible character (tab, CR, LF, ...) or whose
  first non-whitespace character is = + - @ (or a full-width form) are prefixed with a
  single quote (') to prevent spreadsheet formula execution. Raw values are unchanged in
  the ReconcileKit database.
* Totals cover eligible records only. Quarantined rows are listed in
  validation_errors.csv and are NOT included in any monetary total.
* Review dispositions are self-reported labels from a local single-user tool. They never
  change automated findings or totals.
* Source files are not included. Verify inputs against the hashes in manifest.json.
"""


def build_export_package(
    db_path: str | Path, run_id: str, exported_at: datetime | None = None
) -> bytes:
    """Return the ZIP bytes of the reconciliation package for a persisted run."""
    exported_at = exported_at or utc_now()
    with connect(db_path) as con:
        run = reporting.get_run(con, run_id)
        if run is None:
            raise ExportError(f"Run {run_id!r} was not found in {db_path}.")
        invoices = reporting.invoice_results(con, run_id)
        payments = reporting.payment_results(con, run_id)
        exceptions = reporting.exceptions_with_disposition(con, run_id)
        issues = reporting.validation_issues(con, run_id)
        history = reporting.review_history(con, run_id)
        dispositions = reporting.disposition_counts(con, run_id)

    as_of = pd.Timestamp(run["as_of_date"]).date().isoformat()
    created = pd.Timestamp(run["created_at_utc"]).isoformat() + "Z"
    sources = {
        kind: {
            "label": run[f"{prefix}_label"],
            "sha256": run[f"{prefix}_sha256"],
            "data_rows": run[f"{prefix}_rows"],
            "ignored_extra_columns": list(run[f"{prefix}_extra_columns"]),
        }
        for kind, prefix in (("invoices", "invoice"), ("payments", "payment"))
    }
    checks = reporting.invariant_checks(run)
    summary = {
        "run_id": run_id,
        "fingerprint": run["fingerprint"],
        "as_of_date": as_of,
        "run_created_at_utc": created,
        "data_complete": bool(run["is_complete"]),
        "completeness_note": (
            "All source rows were eligible."
            if run["is_complete"]
            else "INCOMPLETE: some rows were quarantined. Totals cover eligible records only "
            "and do not represent the entire uploaded dataset."
        ),
        "counts": {
            "invoice_rows": run["invoice_rows"],
            "payment_rows": run["payment_rows"],
            "eligible_invoices": run["eligible_invoices"],
            "eligible_payments": run["eligible_payments"],
            "quarantined_invoice_rows": run["quarantined_invoice_rows"],
            "quarantined_payment_rows": run["quarantined_payment_rows"],
            "overdue_invoices": run["overdue_invoices"],
            "exceptions": run["exception_count"],
        },
        "invoice_status_counts": run["invoice_status_counts"],
        "payment_status_counts": run["payment_status_counts"],
        "money_usd": _money_summary(run),
        "invariants": [{"check": c, "passed": ok, "detail": d} for c, ok, d in checks],
        "exception_dispositions_at_export": dispositions,
    }

    files: dict[str, bytes] = {}
    files["summary.json"] = json.dumps(summary, indent=2).encode("utf-8")
    files["invoice_results.csv"] = _to_csv(
        _dates_to_text(invoices, ["invoice_date", "due_date"]).rename(
            columns={"record_id": "result_id"}
        ),
        {
            "invoice_amount_cents": "invoice_amount",
            "applied_payment_cents": "applied_payment_amount",
            "outstanding_cents": "outstanding_amount",
            "overpayment_cents": "overpayment_amount",
        },
    )
    files["payment_results.csv"] = _to_csv(
        _dates_to_text(payments, ["payment_date"]).rename(columns={"record_id": "result_id"}),
        {"payment_amount_cents": "payment_amount"},
    )
    files["exceptions.csv"] = _to_csv(exceptions, {"amount_cents": "amount"})
    files["validation_errors.csv"] = _to_csv(issues, {})
    files["review_history.csv"] = _to_csv(history, {})
    files["README.txt"] = _README.format(
        run_id=run_id,
        as_of=as_of,
        exported_at=exported_at.isoformat() + "Z",
        completeness="COMPLETE" if run["is_complete"] else "INCOMPLETE (rows quarantined)",
    ).encode("utf-8")
    manifest = {
        "package": "reconcilekit-reconciliation",
        "run_id": run_id,
        "fingerprint": run["fingerprint"],
        "as_of_date": as_of,
        "rule_version": run["rule_version"],
        "engine_version": run["engine_version"],
        "rule_config": run["config"],
        "exported_at_utc": exported_at.isoformat() + "Z",
        "sources": sources,
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in sorted(files.items())
        },
    }
    files["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in PACKAGE_FILES:
            zf.writestr(name, files[name])
    return buffer.getvalue()


def export_filename(run_id: str) -> str:
    """Server-generated filename; never derived from uploaded filenames."""
    safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
    return f"reconcilekit_{safe}.zip"


def validation_report_csv(db_path: str | Path, run_id: str) -> bytes:
    with connect(db_path) as con:
        return _to_csv(reporting.validation_issues(con, run_id), {})
