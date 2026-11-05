"""Versioned, read-only SQL reporting views (contract ``rk_v1``) over the DuckDB storage.

The view definitions live in ``reconcilekit/sql/rk_v1.sql``. ``install_reporting_views``
is called by ``repository.connect`` on every open, so any database written or opened by
this version exposes them. The functions below add two consumer paths: an ad-hoc query
on a read-only connection, and a per-run Parquet export.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

import duckdb

CONTRACT_VERSION = "rk_v1"

# The public column contract, in order. tests/test_reporting_views.py checks the installed
# views against it, so any change here is deliberate and visible in review.
VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "run_summary": (
        "run_id",
        "created_at_utc",
        "as_of_date",
        "engine_version",
        "rule_version",
        "invoice_label",
        "invoice_sha256",
        "invoice_rows",
        "eligible_invoices",
        "quarantined_invoice_rows",
        "payment_label",
        "payment_sha256",
        "payment_rows",
        "eligible_payments",
        "quarantined_payment_rows",
        "is_complete",
        "data_completeness",
        "eligible_invoice_cents",
        "applied_payment_cents",
        "outstanding_cents",
        "overpayment_cents",
        "eligible_payment_cents",
        "unmatched_payment_cents",
        "ambiguous_payment_cents",
        "blocked_payment_cents",
        "overdue_cents",
        "overdue_invoices",
        "settled_invoice_cents",
        "net_difference_cents",
        "explained_difference_cents",
        "eligible_invoice_usd",
        "applied_payment_usd",
        "outstanding_usd",
        "overpayment_usd",
        "eligible_payment_usd",
        "unmatched_payment_usd",
        "ambiguous_payment_usd",
        "blocked_payment_usd",
        "overdue_usd",
        "settled_invoice_usd",
        "net_difference_usd",
        "explained_difference_usd",
        "settled_invoice_pct",
        "invariant_invoice_side_ok",
        "invariant_payment_side_ok",
        "invariant_bridge_ok",
        "arithmetic_status",
        "exception_count",
        "unresolved_exception_count",
    ),
    "open_invoices": (
        "run_id",
        "as_of_date",
        "invoice_record_id",
        "source_row",
        "customer_id",
        "invoice_id",
        "invoice_date",
        "due_date",
        "invoice_amount_cents",
        "applied_payment_cents",
        "outstanding_cents",
        "invoice_amount_usd",
        "applied_payment_usd",
        "outstanding_usd",
        "status",
        "rule_code",
        "applied_payment_count",
        "is_overdue",
        "days_overdue",
        "aging_bucket",
        "exception_id",
        "current_disposition",
    ),
    "payment_exceptions": (
        "run_id",
        "as_of_date",
        "payment_record_id",
        "source_row",
        "payment_id",
        "customer_id",
        "invoice_id",
        "invoice_id_raw",
        "payment_date",
        "payment_amount_cents",
        "payment_amount_usd",
        "status",
        "rule_code",
        "explanation",
        "related_invoice_record_ids",
        "exception_id",
        "current_disposition",
        "is_resolved",
        "latest_reviewed_at_utc",
    ),
    "exception_status": (
        "run_id",
        "exception_id",
        "category",
        "rule_code",
        "severity",
        "classification",
        "customer_id",
        "invoice_id",
        "payment_id",
        "source_refs",
        "record_ids",
        "amount_cents",
        "amount_usd",
        "explanation",
        "current_disposition",
        "is_resolved",
        "review_event_count",
        "first_reviewed_at_utc",
        "latest_reviewed_at_utc",
        "latest_reviewer",
        "latest_note",
    ),
}
# Natural ordering used for exports, so files are byte-stable for the same run.
VIEW_ORDER_BY = {
    "run_summary": "run_id",
    "open_invoices": "source_row",
    "payment_exceptions": "source_row",
    "exception_status": "exception_id",
}
_RUN_ID_RE = re.compile(r"^RUN-[0-9a-f]{16}$")


class ReportingError(ValueError):
    """A reporting request that cannot be served (user-facing message)."""


def views_sql() -> str:
    return files("reconcilekit").joinpath("sql", f"{CONTRACT_VERSION}.sql").read_text("utf-8")


def install_reporting_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create or replace the rk_v1 schema, macro, and views. Idempotent."""
    con.execute(views_sql())


def open_read_only(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open an existing database for querying only.

    The connection is read-only (no writes to the database) and has external access
    disabled (no reading or writing other files, no extension downloads). Before that, the
    views are (re)installed through a short regular connection so that a database created
    by ReconcileKit 0.1 also exposes them; that is the only write, and it touches only the
    rk_v1 schema. A DuckDB file without ReconcileKit's ``runs`` table is refused untouched.
    """
    from reconcilekit.repository import connect  # local import: repository imports us

    path = Path(db_path)
    if not path.is_file():
        raise ReportingError(f"Database not found: {path}")
    probe = duckdb.connect(str(path), read_only=True)
    try:
        has_runs = probe.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE schema_name = 'main' AND table_name = 'runs'"
        ).fetchone()[0]
    finally:
        probe.close()
    if not has_runs:
        raise ReportingError(f"{path} is not a ReconcileKit database (no runs table).")
    with connect(path):
        pass
    return duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})


def run_query(
    db_path: str | Path, sql: str, params: dict[str, str] | None = None
) -> tuple[list[str], list[tuple]]:
    """Run one SQL statement on a read-only connection; return (columns, rows).

    Only parameters the SQL actually references (as ``$name``) are bound, so the same
    ``--param run_id=...`` can be passed to every example query.
    """
    used = {k: v for k, v in (params or {}).items() if re.search(rf"\${re.escape(k)}\b", sql)}
    con = open_read_only(db_path)
    try:
        cur = con.execute(sql, used or None)
        if cur.description is None:
            raise ReportingError("The statement returned no result set.")
        return [d[0] for d in cur.description], cur.fetchall()
    except duckdb.Error as exc:
        raise ReportingError(f"Query failed: {exc}") from exc
    finally:
        con.close()


def export_parquet(db_path: str | Path, run_id: str, out_dir: str | Path) -> list[Path]:
    """Write each rk_v1 view, filtered to one run, as ``<view>.parquet`` in ``out_dir``."""
    if not _RUN_ID_RE.match(run_id):
        raise ReportingError(f"Invalid run ID {run_id!r}; expected RUN- and 16 hex digits.")
    from reconcilekit.repository import connect

    out = Path(out_dir)
    written = []
    with connect(db_path) as con:
        if (
            con.execute("SELECT count(*) FROM main.runs WHERE run_id = ?", [run_id]).fetchone()[0]
            == 0
        ):
            raise ReportingError(f"Run {run_id} not found in {db_path}")
        con.execute("SET VARIABLE rk_export_run_id = ?", [run_id])
        out.mkdir(parents=True, exist_ok=True)
        for view, columns in VIEW_COLUMNS.items():
            cols = ", ".join(columns)
            rel = con.sql(
                f"SELECT {cols} FROM {CONTRACT_VERSION}.{view} "
                f"WHERE run_id = getvariable('rk_export_run_id') ORDER BY {VIEW_ORDER_BY[view]}"
            )
            path = out / f"{view}.parquet"
            rel.write_parquet(str(path))
            written.append(path)
    return written
