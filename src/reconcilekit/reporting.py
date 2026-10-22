"""SQL reporting over persisted runs. All reads go through DuckDB."""

from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb
import pandas as pd

from reconcilekit.money import format_usd


def list_runs(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """
        SELECT run_id, created_at_utc, as_of_date, invoice_label, payment_label,
               is_complete, exception_count
        FROM runs
        ORDER BY created_at_utc DESC, run_id
        """
    ).df()


def get_run(con: duckdb.DuckDBPyConnection, run_id: str) -> dict | None:
    cur = con.execute("SELECT * FROM runs WHERE run_id = ?", [run_id])
    row = cur.fetchone()
    if row is None:
        return None
    run = dict(zip([d[0] for d in cur.description], row, strict=True))
    for key in ("config_json", "invoice_status_counts_json", "payment_status_counts_json"):
        run[key.removesuffix("_json")] = json.loads(run.pop(key))
    return run


def invoice_results(con: duckdb.DuckDBPyConnection, run_id: str) -> pd.DataFrame:
    return con.execute(
        "SELECT * EXCLUDE (run_id) FROM invoice_results WHERE run_id = ? ORDER BY source_row",
        [run_id],
    ).df()


def payment_results(con: duckdb.DuckDBPyConnection, run_id: str) -> pd.DataFrame:
    return con.execute(
        "SELECT * EXCLUDE (run_id) FROM payment_results WHERE run_id = ? ORDER BY source_row",
        [run_id],
    ).df()


def validation_issues(con: duckdb.DuckDBPyConnection, run_id: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT kind, source_row, record_id, rule_code, field, raw_value, message
        FROM validation_issues WHERE run_id = ?
        ORDER BY kind, source_row, issue_seq
        """,
        [run_id],
    ).df()


def source_rows(con: duckdb.DuckDBPyConnection, run_id: str, record_ids: list[str]) -> pd.DataFrame:
    """Raw source values exactly as read, for evidence panels."""
    df = con.execute(
        """
        SELECT record_id, kind, source_row, raw_json FROM source_rows
        WHERE run_id = ? AND list_contains(?, record_id)
        ORDER BY kind, source_row
        """,
        [run_id, record_ids],
    ).df()
    raw = [json.loads(v) for v in df["raw_json"]]
    df = df.drop(columns=["raw_json"])
    return df.assign(raw=raw)


def exceptions_with_disposition(con: duckdb.DuckDBPyConnection, run_id: str) -> pd.DataFrame:
    """Immutable exceptions joined with the disposition from the latest review event."""
    return con.execute(
        """
        WITH latest AS (
            SELECT exception_id,
                   arg_max(disposition, seq) AS disposition,
                   arg_max(reviewer, seq) AS last_reviewer,
                   max(created_at_utc) AS last_reviewed_at_utc,
                   count(*) AS review_event_count
            FROM review_events
            WHERE run_id = ?
            GROUP BY exception_id
        )
        SELECT e.exception_id, e.category, e.rule_code, e.severity, e.classification,
               coalesce(l.disposition, 'OPEN') AS disposition,
               e.customer_id, e.invoice_id, e.payment_id, e.amount_cents, e.source_refs,
               e.record_ids, e.explanation, l.last_reviewer, l.last_reviewed_at_utc,
               coalesce(l.review_event_count, 0) AS review_event_count
        FROM exceptions e
        LEFT JOIN latest l USING (exception_id)
        WHERE e.run_id = ?
        ORDER BY e.exception_id
        """,
        [run_id, run_id],
    ).df()


def review_history(
    con: duckdb.DuckDBPyConnection, run_id: str, exception_id: str | None = None
) -> pd.DataFrame:
    """Review events in chronological (insertion) order."""
    return con.execute(
        """
        SELECT seq, event_id, exception_id, created_at_utc, reviewer, disposition, note
        FROM review_events
        WHERE run_id = ? AND (CAST(? AS VARCHAR) IS NULL OR exception_id = ?)
        ORDER BY seq
        """,
        [run_id, exception_id, exception_id],
    ).df()


def disposition_counts(con: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, int]:
    df = exceptions_with_disposition(con, run_id)
    return {str(k): int(v) for k, v in df["disposition"].value_counts().sort_index().items()}


@dataclass(frozen=True)
class BridgeLine:
    label: str
    symbol: str  # I, P, O, ...
    sign: str  # "", "+", "-", "="
    cents: int
    note: str


def bridge(run: dict) -> list[BridgeLine]:
    """Explain I - P = O - X - U - M - B in business language."""
    i, p = run["eligible_invoice_cents"], run["eligible_payment_cents"]
    o, x = run["outstanding_cents"], run["overpayment_cents"]
    u, m, b = (
        run["unmatched_payment_cents"],
        run["ambiguous_payment_cents"],
        run["blocked_payment_cents"],
    )
    return [
        BridgeLine("Eligible invoices", "I", "", i, "Valid invoices included in this run."),
        BridgeLine("Eligible payments", "P", "-", p, "Valid payments included in this run."),
        BridgeLine("Net difference", "I - P", "=", i - p, "What the totals alone show."),
        BridgeLine("Outstanding invoice balances", "O", "+", o, "Amounts still owed on invoices."),
        BridgeLine("Overpayments", "X", "-", x, "Payments above invoice amounts; they offset O."),
        BridgeLine("Unmatched payments", "U", "-", u, "No usable invoice reference."),
        BridgeLine("Ambiguous payments", "M", "-", m, "Reference points to a duplicated invoice."),
        BridgeLine("Blocked payments", "B", "-", b, "Reference points to an invalid invoice."),
        BridgeLine(
            "Explained difference",
            "O - X - U - M - B",
            "=",
            o - x - u - m - b,
            "Must equal the net difference.",
        ),
    ]


def invariant_checks(run: dict) -> list[tuple[str, bool, str]]:
    """Re-verify the persisted totals (the engine already refused to save a failing run)."""
    i, a, p = (
        run["eligible_invoice_cents"],
        run["applied_payment_cents"],
        run["eligible_payment_cents"],
    )
    o, x = run["outstanding_cents"], run["overpayment_cents"]
    u, m, b = (
        run["unmatched_payment_cents"],
        run["ambiguous_payment_cents"],
        run["blocked_payment_cents"],
    )
    return [
        (
            "I - A = O - X",
            i - a == o - x,
            f"{format_usd(i)} - {format_usd(a)} = {format_usd(o)} - {format_usd(x)}",
        ),
        (
            "P = A + U + M + B",
            p == a + u + m + b,
            f"{format_usd(p)} = {format_usd(a)} + {format_usd(u)} + {format_usd(m)} "
            f"+ {format_usd(b)}",
        ),
        (
            "I - P = O - X - U - M - B",
            i - p == o - x - u - m - b,
            f"{format_usd(i - p)} = {format_usd(o - x - u - m - b)}",
        ),
    ]
