"""The rk_v1 SQL reporting contract.

Expected demo figures are the hand-calculated values from docs/demo-expected-results.md
(written from the CSVs, not from engine output). Property tests compare the views with
the independent model in test_properties.py.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import pytest
from hypothesis import given, settings

from conftest import inv, pay, run_rows
from reconcilekit import cli
from reconcilekit.demo import DEMO_AS_OF, load_demo
from reconcilekit.models import PaymentStatus
from reconcilekit.reconciliation import reconcile_files
from reconcilekit.reporting_views import (
    CONTRACT_VERSION,
    VIEW_COLUMNS,
    ReportingError,
    export_parquet,
    install_reporting_views,
    open_read_only,
    run_query,
    views_sql,
)
from reconcilekit.repository import add_review_event, connect, save_run
from test_properties import ledgers, model

ROOT = Path(__file__).resolve().parents[1]
DEMO_RUN_ID = "RUN-2e17cda32757cc29"


def demo_db(path: Path) -> str:
    inv_bytes, pay_bytes = load_demo()
    run = reconcile_files(inv_bytes, pay_bytes, DEMO_AS_OF)
    return save_run(path, run).run_id


def rows(db: Path, sql: str, params=None) -> list[tuple]:
    with connect(db) as con:
        return con.execute(sql, params or []).fetchall()


def one(db: Path, sql: str, params=None):
    (row,) = rows(db, sql, params)
    return row


def usd(cents: int) -> Decimal:
    """Independent cents -> dollars conversion for expectations."""
    return Decimal(cents) / 100


@pytest.fixture
def demo(tmp_path) -> Path:
    db = tmp_path / "demo.duckdb"
    assert demo_db(db) == DEMO_RUN_ID
    return db


# ------------------------------------------------------------------ contract / schema


def test_contract_view_and_versions(demo):
    assert one(demo, "SELECT contract_version, storage_schema_version FROM rk_v1.contract") == (
        "rk_v1",
        1,
    )
    assert CONTRACT_VERSION == "rk_v1"


@pytest.mark.parametrize("view", sorted(VIEW_COLUMNS))
def test_view_columns_match_contract(demo, view):
    with connect(demo) as con:
        described = con.execute(f"DESCRIBE rk_v1.{view}").fetchall()
    assert tuple(r[0] for r in described) == VIEW_COLUMNS[view]


def test_money_and_metric_columns_are_exact_types(demo):
    with connect(demo) as con:
        for view in VIEW_COLUMNS:
            for name, dtype, *_ in con.execute(f"DESCRIBE rk_v1.{view}").fetchall():
                assert "DOUBLE" not in dtype and "FLOAT" not in dtype and "REAL" not in dtype, (
                    view,
                    name,
                    dtype,
                )
                if name.endswith("_cents"):
                    assert dtype == "BIGINT", (view, name, dtype)
                if name.endswith("_usd") or name.endswith("_pct"):
                    assert dtype == "DECIMAL(38,2)", (view, name, dtype)


def test_view_sql_uses_explicit_columns_only():
    text = re.sub(r"--[^\n]*", "", views_sql())
    assert not re.search(r"SELECT\s+\*|\w\.\*", text, re.I)


def test_every_contract_column_is_documented_with_its_actual_type(demo):
    doc = (ROOT / "docs" / "sql-reporting.md").read_text(encoding="utf-8")
    with connect(demo) as con:
        for view, columns in VIEW_COLUMNS.items():
            section = doc.split(f"### `rk_v1.{view}`", 1)
            assert len(section) == 2, f"docs/sql-reporting.md has no section for {view}"
            body = section[1].split("\n### ", 1)[0]
            documented = dict(re.findall(r"^\| `(\w+)` \| ([A-Z0-9(),\[\]]+) \|", body, re.M))
            actual = {r[0]: r[1] for r in con.execute(f"DESCRIBE rk_v1.{view}").fetchall()}
            assert list(documented) == list(columns), f"{view}: documented columns differ"
            assert documented == actual, f"{view}: documented types differ"


def test_views_are_read_only(demo):
    with connect(demo) as con:
        for view in VIEW_COLUMNS:
            with pytest.raises(duckdb.Error):
                con.execute(f"DELETE FROM rk_v1.{view}")
            with pytest.raises(duckdb.Error):
                con.execute(f"UPDATE rk_v1.{view} SET run_id = 'x'")


def test_install_is_idempotent_and_adds_only_rk_v1_objects(tmp_path):
    db = tmp_path / "x.duckdb"
    with connect(db) as con:
        before = con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() ORDER BY ALL"
        ).fetchall()
        install_reporting_views(con)
        install_reporting_views(con)
        after = con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() ORDER BY ALL"
        ).fetchall()
        views = con.execute(
            "SELECT schema_name, view_name FROM duckdb_views() WHERE NOT internal ORDER BY ALL"
        ).fetchall()
    assert before == after  # storage tables untouched
    assert all(s == "main" for s, _ in before)
    assert views == [("rk_v1", v) for v in sorted([*VIEW_COLUMNS, "contract"])]


def test_database_without_views_gets_them_on_open(demo):
    """A database written by ReconcileKit 0.1 has no rk_v1 schema."""
    con = duckdb.connect(str(demo))
    con.execute("DROP SCHEMA rk_v1 CASCADE")
    con.close()
    ro = open_read_only(demo)
    try:
        assert ro.execute("SELECT count(*) FROM rk_v1.run_summary").fetchone() == (1,)
    finally:
        ro.close()


def test_views_resolve_when_database_is_attached_under_an_alias(demo):
    """Regression: unqualified table names broke under ATTACH ... AS alias."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{demo}' AS rk (READ_ONLY)")
        got = con.execute(
            "SELECT s.outstanding_cents, s.unresolved_exception_count, "
            "(SELECT count(*) FROM rk.rk_v1.open_invoices), "
            "(SELECT count(*) FROM rk.rk_v1.payment_exceptions) "
            "FROM rk.rk_v1.run_summary AS s"
        ).fetchone()
    finally:
        con.close()
    assert got == (157550, 19, 3, 6)


def test_empty_database_returns_empty_views(tmp_path):
    db = tmp_path / "empty.duckdb"
    for view in VIEW_COLUMNS:
        assert rows(db, f"SELECT count(*) FROM rk_v1.{view}") == [(0,)]


# ------------------------------------------------------------------ demo (hand-calculated)


def test_run_summary_matches_hand_calculation(demo):
    expected = {  # docs/demo-expected-results.md, in cents
        "eligible_invoice_cents": 758575,  # I
        "applied_payment_cents": 606025,  # A
        "outstanding_cents": 157550,  # O
        "overpayment_cents": 5000,  # X
        "eligible_payment_cents": 892075,  # P
        "unmatched_payment_cents": 166050,  # U
        "ambiguous_payment_cents": 100000,  # M
        "blocked_payment_cents": 20000,  # B
        "overdue_cents": 127550,  # 300.00 + 975.50
        "overdue_invoices": 2,
        "settled_invoice_cents": 601025,  # I - O
        "net_difference_cents": -133500,  # I - P
        "explained_difference_cents": -133500,  # O - X - U - M - B
    }
    got = one(
        demo,
        f"SELECT {', '.join(expected)} FROM rk_v1.run_summary WHERE run_id = ?",
        [DEMO_RUN_ID],
    )
    assert dict(zip(expected, got, strict=True)) == expected


def test_run_summary_status_and_derived_metrics(demo):
    got = one(
        demo,
        """
        SELECT is_complete, data_completeness, invoice_rows, eligible_invoices,
               quarantined_invoice_rows, payment_rows, eligible_payments,
               quarantined_payment_rows, eligible_invoice_usd, outstanding_usd,
               net_difference_usd, settled_invoice_usd, settled_invoice_pct,
               invariant_invoice_side_ok, invariant_payment_side_ok, invariant_bridge_ok,
               arithmetic_status, exception_count, unresolved_exception_count, as_of_date
        FROM rk_v1.run_summary WHERE run_id = ?
        """,
        [DEMO_RUN_ID],
    )
    expected_pct = (Decimal("6010.25") / Decimal("7585.75") * 100).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )
    assert got == (
        False,
        "INCOMPLETE",
        16,
        8,
        8,
        17,
        13,
        4,
        Decimal("7585.75"),
        Decimal("1575.50"),
        Decimal("-1335.00"),
        Decimal("6010.25"),
        expected_pct,
        True,
        True,
        True,
        "PASS",
        19,
        19,
        DEMO_AS_OF,
    )
    assert expected_pct == Decimal("79.23")


def test_open_invoices_match_hand_calculation(demo):
    got = rows(
        demo,
        """
        SELECT invoice_id, customer_id, invoice_amount_cents, applied_payment_cents,
               outstanding_cents, outstanding_usd, status, is_overdue, days_overdue,
               aging_bucket, applied_payment_count, exception_id IS NOT NULL,
               current_disposition
        FROM rk_v1.open_invoices WHERE run_id = ? ORDER BY source_row
        """,
        [DEMO_RUN_ID],
    )
    assert got == [
        # INV-1003: 800.00 - 500.00, due 2026-02-14, 45 days overdue
        (
            "INV-1003",
            "CUST-B",
            80000,
            50000,
            30000,
            Decimal("300.00"),
            "PARTIALLY_PAID",
            True,
            45,
            "DAYS_31_60",
            1,
            True,
            "OPEN",
        ),
        # INV-1005: unpaid (its payment used "inv-1005"), due 2026-03-12, 19 days overdue
        (
            "INV-1005",
            "CUST-C",
            97550,
            0,
            97550,
            Decimal("975.50"),
            "UNPAID",
            True,
            19,
            "DAYS_1_30",
            0,
            True,
            "OPEN",
        ),
        # INV-1007: unpaid, due 2026-04-30 (after as-of): no exception is opened
        (
            "INV-1007",
            "CUST-D",
            30000,
            0,
            30000,
            Decimal("300.00"),
            "UNPAID",
            False,
            0,
            "NOT_OVERDUE",
            0,
            False,
            None,
        ),
    ]


def test_payment_exceptions_match_hand_calculation(demo):
    got = rows(
        demo,
        """
        SELECT payment_id, customer_id, invoice_id, invoice_id_raw, payment_amount_cents,
               status, rule_code, is_resolved
        FROM rk_v1.payment_exceptions WHERE run_id = ? ORDER BY source_row
        """,
        [DEMO_RUN_ID],
    )
    assert got == [
        ("PAY-5008", "CUST-C", None, "", 12500, "UNMATCHED", "PAYMENT_REFERENCE_MISSING", False),
        (
            "PAY-5009",
            "CUST-C",
            "inv-1005",
            "inv-1005",
            97550,
            "UNMATCHED",
            "PAYMENT_REFERENCE_NOT_FOUND",
            False,
        ),
        (
            "PAY-5010",
            "CUST-A",
            "INV-9999",
            "INV-9999",
            31000,
            "UNMATCHED",
            "PAYMENT_REFERENCE_NOT_FOUND",
            False,
        ),
        (
            "PAY-5011",
            "CUST-B",
            "INV-1001",
            "INV-1001",
            25000,
            "UNMATCHED",
            "CUSTOMER_REFERENCE_CONFLICT",
            False,
        ),
        (
            "PAY-5012",
            "CUST-E",
            "INV-1009",
            "INV-1009",
            100000,
            "AMBIGUOUS",
            "AMBIGUOUS_INVOICE_REFERENCE",
            False,
        ),
        (
            "PAY-5013",
            "CUST-F",
            "INV-1011",
            "INV-1011",
            20000,
            "BLOCKED",
            "REFERENCED_INVOICE_INVALID",
            False,
        ),
    ]
    # U + M + B from the hand calculation
    assert sum(r[4] for r in got) == 166050 + 100000 + 20000


def test_each_payment_exception_links_to_its_own_review_exception(demo):
    got = rows(
        demo,
        """
        SELECT p.payment_id, x.payment_id, x.category, x.severity, x.classification, p.status
        FROM rk_v1.payment_exceptions AS p
        JOIN rk_v1.exception_status AS x USING (run_id, exception_id)
        """,
    )
    assert len(got) == 6
    for pay_id, x_pay_id, category, severity, classification, status in got:
        assert (pay_id, category, severity, classification) == (
            x_pay_id,
            "PAYMENT",
            "REVIEW",
            status,
        )


def test_exception_status_counts_match_hand_calculation(demo):
    got = rows(
        demo,
        """
        SELECT category, severity, count(*), count(*) FILTER (WHERE current_disposition = 'OPEN'),
               count(*) FILTER (WHERE is_resolved), sum(review_event_count)
        FROM rk_v1.exception_status WHERE run_id = ? GROUP BY ALL ORDER BY ALL
        """,
        [DEMO_RUN_ID],
    )
    assert got == [
        ("INVOICE", "REVIEW", 3, 3, 0, 0),
        ("PAYMENT", "INFO", 1, 1, 0, 0),
        ("PAYMENT", "REVIEW", 6, 6, 0, 0),
        ("VALIDATION", "QUARANTINE", 9, 9, 0, 0),
    ]
    # Quarantined rows never carry an amount.
    assert rows(
        demo,
        "SELECT count(*) FROM rk_v1.exception_status "
        "WHERE category = 'VALIDATION' AND (amount_cents IS NOT NULL OR amount_usd IS NOT NULL)",
    ) == [(0,)]


def test_usd_columns_equal_cents_divided_by_100_exactly(demo):
    with connect(demo) as con:
        for view, columns in VIEW_COLUMNS.items():
            for col in columns:
                if not col.endswith("_usd"):
                    continue
                cents_col = col.removesuffix("_usd") + "_cents"
                if col == "amount_usd":
                    cents_col = "amount_cents"
                pairs = con.execute(f"SELECT {cents_col}, {col} FROM rk_v1.{view}").fetchall()
                for cents, dollars in pairs:
                    assert (cents is None and dollars is None) or usd(cents) == dollars, (
                        view,
                        col,
                    )


# ------------------------------------------------------------------ reviews


def test_review_status_reflects_latest_event_by_sequence(demo):
    t1, t2 = datetime(2026, 4, 1, 9, 0, 0), datetime(2026, 4, 2, 10, 30, 0)
    # EX-0014 = PAY-5008 (blank reference); EX-0012 = INV-1005 (unpaid, overdue)
    add_review_event(
        demo, DEMO_RUN_ID, "EX-0014", "analyst-1", "INVESTIGATING", "", created_at_utc=t1
    )
    add_review_event(
        demo,
        DEMO_RUN_ID,
        "EX-0014",
        "analyst-2",
        "RESOLVED_EXTERNALLY",
        "Customer remittance found",
        created_at_utc=t2,
    )
    # Two events with the same timestamp: the later insertion wins.
    add_review_event(demo, DEMO_RUN_ID, "EX-0012", "a", "INVESTIGATING", "", created_at_utc=t1)
    add_review_event(demo, DEMO_RUN_ID, "EX-0012", "b", "ACKNOWLEDGED", "", created_at_utc=t1)

    got = rows(
        demo,
        """
        SELECT exception_id, current_disposition, is_resolved, review_event_count,
               first_reviewed_at_utc, latest_reviewed_at_utc, latest_reviewer, latest_note
        FROM rk_v1.exception_status
        WHERE run_id = ? AND exception_id IN ('EX-0012', 'EX-0014') ORDER BY exception_id
        """,
        [DEMO_RUN_ID],
    )
    assert got == [
        ("EX-0012", "ACKNOWLEDGED", False, 2, t1, t1, "b", ""),
        (
            "EX-0014",
            "RESOLVED_EXTERNALLY",
            True,
            2,
            t1,
            t2,
            "analyst-2",
            "Customer remittance found",
        ),
    ]
    assert one(
        demo,
        "SELECT unresolved_exception_count FROM rk_v1.run_summary WHERE run_id = ?",
        [DEMO_RUN_ID],
    ) == (18,)
    assert one(
        demo,
        "SELECT is_resolved, current_disposition, latest_reviewed_at_utc "
        "FROM rk_v1.payment_exceptions WHERE payment_id = 'PAY-5008'",
    ) == (True, "RESOLVED_EXTERNALLY", t2)
    assert one(
        demo, "SELECT current_disposition FROM rk_v1.open_invoices WHERE invoice_id = 'INV-1005'"
    ) == ("ACKNOWLEDGED",)


def test_reviews_never_change_financial_columns(demo):
    money = "SELECT * EXCLUDE (unresolved_exception_count) FROM rk_v1.run_summary"
    before = rows(demo, money)
    add_review_event(demo, DEMO_RUN_ID, "EX-0015", "r", "RESOLVED_EXTERNALLY", "done")
    assert rows(demo, money) == before


# ------------------------------------------------------------------ isolation & determinism


def second_run(db: Path) -> str:
    run = run_rows(
        [inv("INV-1", "CUST-A", amount="100.00"), inv("INV-2", "CUST-A", amount="50.00")],
        [
            pay("P-1", "INV-1", "CUST-A", amount="40.00"),
            pay("P-2", "INV-404", "CUST-A", amount="5.00"),
        ],
    )
    return save_run(db, run).run_id


def per_run_snapshot(db: Path, run_id: str) -> dict:
    out = {}
    for view, cols in VIEW_COLUMNS.items():
        keep = [c for c in cols if not c.endswith("_at_utc")]
        out[view] = rows(
            db,
            f"SELECT {', '.join(keep)} FROM rk_v1.{view} WHERE run_id = ? ORDER BY ALL",
            [run_id],
        )
    return out


def test_runs_are_isolated(tmp_path):
    shared = tmp_path / "shared.duckdb"
    demo_id = demo_db(shared)
    other_id = second_run(shared)
    # The same exception IDs exist in both runs; a review in one must not leak.
    add_review_event(shared, other_id, "EX-0001", "r", "RESOLVED_EXTERNALLY", "x")

    alone_demo, alone_other = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    demo_db(alone_demo)
    second_run(alone_other)
    add_review_event(alone_other, other_id, "EX-0001", "r", "RESOLVED_EXTERNALLY", "x")

    assert per_run_snapshot(shared, demo_id) == per_run_snapshot(alone_demo, demo_id)
    assert per_run_snapshot(shared, other_id) == per_run_snapshot(alone_other, other_id)
    assert one(
        shared,
        "SELECT current_disposition FROM rk_v1.exception_status "
        "WHERE run_id = ? AND exception_id = 'EX-0001'",
        [demo_id],
    ) == ("OPEN",)
    assert rows(shared, "SELECT count(*) FROM rk_v1.run_summary") == [(2,)]


def test_same_input_in_separate_databases_gives_identical_results(tmp_path):
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    demo_db(a)
    demo_db(b)
    assert per_run_snapshot(a, DEMO_RUN_ID) == per_run_snapshot(b, DEMO_RUN_ID)


# ------------------------------------------------------------------ edge cases


def test_fully_reconciled_run_has_empty_exception_views(tmp_path):
    db = tmp_path / "clean.duckdb"
    run = run_rows([inv(amount="100.00")], [pay(amount="100.00")])
    run_id = save_run(db, run).run_id
    assert rows(db, "SELECT count(*) FROM rk_v1.open_invoices") == [(0,)]
    assert rows(db, "SELECT count(*) FROM rk_v1.payment_exceptions") == [(0,)]
    assert rows(db, "SELECT count(*) FROM rk_v1.exception_status") == [(0,)]
    assert one(
        db,
        "SELECT settled_invoice_pct, unresolved_exception_count, exception_count, "
        "data_completeness, arithmetic_status FROM rk_v1.run_summary WHERE run_id = ?",
        [run_id],
    ) == (Decimal("100.00"), 0, 0, "COMPLETE", "PASS")


def test_run_without_eligible_invoices_has_null_settled_share(tmp_path):
    db = tmp_path / "noinv.duckdb"
    run = run_rows([inv(amount="")], [pay(amount="10.00")])  # the only invoice is invalid
    run_id = save_run(db, run).run_id
    got = one(
        db,
        "SELECT eligible_invoice_cents, settled_invoice_cents, settled_invoice_pct, "
        "blocked_payment_cents, arithmetic_status FROM rk_v1.run_summary WHERE run_id = ?",
        [run_id],
    )
    assert got == (0, 0, None, 1000, "PASS")


def test_payments_only_and_invoices_only_runs(tmp_path):
    db = tmp_path / "edge.duckdb"
    only_pay = save_run(db, run_rows([], [pay(invoice_id="", amount="7.00")])).run_id
    only_inv = save_run(db, run_rows([inv(amount="9.99")], [])).run_id
    assert one(
        db,
        "SELECT eligible_invoice_cents, unmatched_payment_cents, settled_invoice_pct "
        "FROM rk_v1.run_summary WHERE run_id = ?",
        [only_pay],
    ) == (0, 700, None)
    assert one(
        db,
        "SELECT outstanding_cents, settled_invoice_pct FROM rk_v1.run_summary WHERE run_id = ?",
        [only_inv],
    ) == (999, Decimal("0.00"))


@pytest.mark.parametrize(
    "invoice_id, customer_id",
    [
        ("000123", "00042"),
        ("INV/2026#7", "Cust & Co."),
        ("Ünïcødé-请求", "客户-7"),
        ("O'Brien-1", "O'Brien"),
        ('=HYPERLINK("x")', "+cmd"),
        ("'; DROP TABLE runs; --", 'x"y'),
    ],
)
def test_unusual_identifiers_survive_views_and_parquet(tmp_path, invoice_id, customer_id):
    db = tmp_path / "ids.duckdb"
    run = run_rows([inv(invoice_id, customer_id, amount="12.34")], [])
    run_id = save_run(db, run).run_id
    assert one(db, "SELECT invoice_id, customer_id, outstanding_usd FROM rk_v1.open_invoices") == (
        invoice_id,
        customer_id,
        Decimal("12.34"),
    )
    out = tmp_path / "pq"
    export_parquet(db, run_id, out)
    con = duckdb.connect()
    try:
        got = con.execute(
            "SELECT invoice_id, customer_id, outstanding_cents, outstanding_usd "
            "FROM read_parquet(?)",
            [str(out / "open_invoices.parquet")],
        ).fetchone()
    finally:
        con.close()
    assert got == (invoice_id, customer_id, 1234, Decimal("12.34"))
    assert rows(db, "SELECT count(*) FROM main.runs") == [(1,)]


# ------------------------------------------------------------------ property-based


@settings(max_examples=40)  # each example writes and queries a database file
@given(ledgers())
def test_views_agree_with_independent_model(data):
    invoices, payments = data
    eligible_inv, status, applied, totals, by_status = model(invoices, payments)
    expected_open = {
        key: amount - applied[key] for key, amount in eligible_inv.items() if amount > applied[key]
    }
    expected_exceptions = {pid: s for pid, s in status.items() if s.value != "APPLIED"}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "p.duckdb"
        run_id = save_run(db, run_rows(invoices, payments)).run_id
        s = one(
            db,
            "SELECT eligible_invoice_cents, applied_payment_cents, eligible_payment_cents, "
            "outstanding_cents, unmatched_payment_cents, ambiguous_payment_cents, "
            "blocked_payment_cents, settled_invoice_cents, settled_invoice_pct, "
            "arithmetic_status FROM rk_v1.run_summary WHERE run_id = ?",
            [run_id],
        )
        opens = rows(
            db,
            "SELECT customer_id, invoice_id, outstanding_cents FROM rk_v1.open_invoices "
            "WHERE run_id = ?",
            [run_id],
        )
        exc = rows(
            db,
            "SELECT payment_id, status, exception_id IS NOT NULL FROM rk_v1.payment_exceptions "
            "WHERE run_id = ?",
            [run_id],
        )
    i_total, a_total, p_total = totals["I"], totals["A"], totals["P"]
    o_total = sum(expected_open.values())
    expected_pct = (
        None
        if i_total == 0
        else (Decimal(i_total - o_total) / Decimal(i_total) * 100).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
    )
    assert s == (
        i_total,
        a_total,
        p_total,
        o_total,
        by_status[PaymentStatus.UNMATCHED],
        by_status[PaymentStatus.AMBIGUOUS],
        by_status[PaymentStatus.BLOCKED],
        i_total - o_total,
        expected_pct,
        "PASS",
    )
    assert {(c, i): o for c, i, o in opens} == expected_open
    assert {pid: st for pid, st, _ in exc} == {
        pid: st.value for pid, st in expected_exceptions.items()
    }
    assert all(linked for *_, linked in exc)


# ------------------------------------------------------------------ query & parquet APIs


def test_run_query_is_read_only_and_sandboxed(demo):
    with pytest.raises(ReportingError, match="read-only"):
        run_query(demo, "DELETE FROM main.runs")
    target = str(ROOT / "pyproject.toml").replace("'", "''")
    with pytest.raises(ReportingError, match="disabled"):
        run_query(demo, f"SELECT * FROM read_csv('{target}')")
    with pytest.raises(ReportingError, match="disabled"):
        run_query(demo, f"COPY (SELECT 1) TO '{demo.parent / 'leak.csv'}'")
    assert not (demo.parent / "leak.csv").exists()
    assert rows(demo, "SELECT count(*) FROM main.runs") == [(1,)]


def test_run_query_binds_only_referenced_parameters(demo):
    cols, got = run_query(
        demo,
        "SELECT outstanding_cents FROM rk_v1.run_summary WHERE run_id = $run_id",
        {"run_id": DEMO_RUN_ID, "unused": "x"},
    )
    assert (cols, got) == (["outstanding_cents"], [(157550,)])
    # A parameter value is data, never SQL.
    assert run_query(
        demo,
        "SELECT count(*) FROM rk_v1.run_summary WHERE run_id = $run_id",
        {"run_id": "x' OR '1'='1"},
    )[1] == [(0,)]


def test_run_query_refuses_missing_or_foreign_databases(tmp_path):
    with pytest.raises(ReportingError, match="not found"):
        run_query(tmp_path / "missing.duckdb", "SELECT 1")
    foreign = tmp_path / "other.duckdb"
    con = duckdb.connect(str(foreign))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.close()
    with pytest.raises(ReportingError, match="not a ReconcileKit database"):
        run_query(foreign, "SELECT 1")
    con = duckdb.connect(str(foreign), read_only=True)
    try:  # the foreign file was not modified
        assert con.execute("SELECT count(*) FROM duckdb_tables()").fetchone() == (1,)
    finally:
        con.close()


def test_parquet_export_preserves_types_values_and_run_scope(tmp_path):
    db = tmp_path / "pq.duckdb"
    demo_id = demo_db(db)
    second_run(db)
    out = tmp_path / "out"
    written = export_parquet(db, demo_id, out)
    assert sorted(p.name for p in written) == sorted(f"{v}.parquet" for v in VIEW_COLUMNS)
    con = duckdb.connect()
    try:
        for view, columns in VIEW_COLUMNS.items():
            path = str(out / f"{view}.parquet")
            described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
            assert tuple(r[0] for r in described) == columns
            types = {r[0]: r[1] for r in described}
            for col in columns:
                if col.endswith("_cents"):
                    assert types[col] == "BIGINT"
                if col.endswith(("_usd", "_pct")):
                    assert types[col] == "DECIMAL(38,2)"
            run_ids = con.execute("SELECT DISTINCT run_id FROM read_parquet(?)", [path]).fetchall()
            assert run_ids in ([(demo_id,)], [])
        assert con.execute(
            "SELECT sum(outstanding_cents), count(*) FROM read_parquet(?)",
            [str(out / "open_invoices.parquet")],
        ).fetchone() == (157550, 3)
    finally:
        con.close()


def test_parquet_export_rejects_bad_or_unknown_run(demo, tmp_path):
    with pytest.raises(ReportingError, match="Invalid run ID"):
        export_parquet(demo, "RUN-x'; DROP TABLE runs; --", tmp_path / "o")
    with pytest.raises(ReportingError, match="not found"):
        export_parquet(demo, "RUN-0000000000000000", tmp_path / "o")
    assert not (tmp_path / "o").exists()


# ------------------------------------------------------------------ CLI


def test_cli_query_csv_and_markdown(demo, capsys):
    sql = (
        "SELECT invoice_id, outstanding_usd, is_overdue FROM rk_v1.open_invoices "
        "WHERE run_id = $run_id ORDER BY source_row"
    )
    assert (
        cli.main(["query", "--db", str(demo), "--sql", sql, "--param", f"run_id={DEMO_RUN_ID}"])
        == 0
    )
    assert capsys.readouterr().out == (
        "invoice_id,outstanding_usd,is_overdue\n"
        "INV-1003,300.00,true\n"
        "INV-1005,975.50,true\n"
        "INV-1007,300.00,false\n"
    )
    assert (
        cli.main(
            [
                "query",
                "--db",
                str(demo),
                "--sql",
                sql,
                "--param",
                f"run_id={DEMO_RUN_ID}",
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines()[:3] == [
        "| invoice_id | outstanding_usd | is_overdue |",
        "|---|---|---|",
        "| INV-1003 | 300.00 | true |",
    ]


def test_cli_query_escapes_formula_cells_but_not_numbers(tmp_path, capsys):
    db = tmp_path / "f.duckdb"
    save_run(db, run_rows([inv("=1+1", "@x", amount="5.00")], []))
    sql = (
        "SELECT invoice_id, customer_id, net_difference_usd "
        "FROM rk_v1.open_invoices, rk_v1.run_summary"
    )
    assert cli.main(["query", "--db", str(db), "--sql", sql]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "'=1+1,'@x,5.00"


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--sql", "SELEC 1"], "Query failed"),
        (["--sql", "DELETE FROM main.runs"], "read-only"),
        (["--sql", "SELECT 1", "--param", "no-equals"], "NAME=VALUE"),
    ],
)
def test_cli_query_errors_exit_two(demo, capsys, argv, message):
    assert cli.main(["query", "--db", str(demo), *argv]) == cli.EXIT_INVALID_INPUT
    assert message in capsys.readouterr().err


def test_cli_parquet(demo, tmp_path, capsys):
    out = tmp_path / "pq"
    assert (
        cli.main(["parquet", "--db", str(demo), "--run-id", DEMO_RUN_ID, "--out-dir", str(out)])
        == 0
    )
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{v}.parquet" for v in VIEW_COLUMNS)
    assert cli.main(["parquet", "--db", str(demo), "--run-id", "bad", "--out-dir", str(out)]) == 2


@pytest.mark.parametrize(
    "path", sorted((ROOT / "examples" / "sql").glob("*.sql")), ids=lambda p: p.name
)
def test_every_example_query_runs(demo, path):
    cols, _ = run_query(demo, path.read_text(encoding="utf-8"), {"run_id": DEMO_RUN_ID})
    assert cols
