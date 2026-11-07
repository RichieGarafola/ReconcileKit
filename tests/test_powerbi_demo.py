"""The Power BI demonstration dataset (powerbi/).

Expected figures below are hand calculations from the snapshot CSVs (worked in
powerbi/model/model.md), not engine output. The DAX and Power Query files cannot be
executed here; they are checked statically against the model files.
"""

from __future__ import annotations

import filecmp
import importlib.util
import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from reconcilekit.reporting_views import VIEW_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
POWERBI = ROOT / "powerbi"
MODEL = POWERBI / "dataset" / "model"
RUN1, RUN2 = "RUN-2e17cda32757cc29", "RUN-f1b80b3097639ecd"

HAND = {
    RUN1: {  # 2026-03-31, bundled demo (docs/demo-expected-results.md)
        "eligible_invoice_cents": 758575,
        "applied_payment_cents": 606025,
        "outstanding_cents": 157550,
        "overpayment_cents": 5000,
        "eligible_payment_cents": 892075,
        "unmatched_payment_cents": 166050,
        "ambiguous_payment_cents": 100000,
        "blocked_payment_cents": 20000,
        "settled_invoice_cents": 601025,
        "settled_pct": "79.23",
        "quarantined_rows": 12,
        "exceptions": 19,
        "unresolved": 17,  # 2 resolved by scripted reviews
        "validation": 9,
        "payment_exceptions": (6, 286050),
        "open_invoices": 3,
        "customers": {"CUST-C": 97550, "CUST-B": 30000, "CUST-D": 30000},
    },
    RUN2: {  # 2026-04-30 follow-up (powerbi/model/model.md, "Hand calculation")
        "eligible_invoice_cents": 1162575,
        "applied_payment_cents": 989575,
        "outstanding_cents": 184000,
        "overpayment_cents": 11000,
        "eligible_payment_cents": 1122075,
        "unmatched_payment_cents": 132500,
        "ambiguous_payment_cents": 0,
        "blocked_payment_cents": 0,
        "settled_invoice_cents": 978575,
        "settled_pct": "84.17",
        "quarantined_rows": 7,
        "exceptions": 14,
        "unresolved": 14,  # its two reviews are INVESTIGATING
        "validation": 5,
        "payment_exceptions": (4, 132500),
        "open_invoices": 4,
        "customers": {"CUST-A": 80000, "CUST-H": 64000, "CUST-F": 25000, "CUST-G": 15000},
    },
}
EXTRAS = {
    "run": ("run_summary", ("snapshot_label", "snapshot_order", "is_report_default")),
    "open_invoice": ("open_invoices", ("run_invoice_key", "run_exception_key", "aging_order")),
    "payment_exception": ("payment_exceptions", ("run_payment_key", "run_exception_key")),
    "exception": ("exception_status", ("run_exception_key",)),
}


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_dataset", POWERBI / "scripts" / "build_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def q(sql: str, params=None) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def m(table: str) -> str:
    return f"read_parquet('{(MODEL / f'{table}.parquet').as_posix()}')"


# ------------------------------------------------------------------ reproducibility


def test_committed_dataset_is_current():
    assert builder.stale_files() == [], "Run: python powerbi/scripts/build_dataset.py"


def test_build_is_byte_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    builder.build(a)
    builder.build(b)
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files and files == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    for rel in files:
        assert filecmp.cmp(a / rel, b / rel, shallow=False), rel


def test_snapshot_csvs_keep_exact_bytes():
    """Run IDs hash input bytes, so CRLF conversion would change the snapshot-2 run ID."""
    for csv_file in (POWERBI / "dataset" / "source").rglob("*.csv"):
        assert b"\r" not in csv_file.read_bytes(), csv_file
        attrs = subprocess.run(
            ["git", "check-attr", "text", "--", str(csv_file.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert attrs.strip().endswith("unset"), attrs


def test_dataset_folder_is_not_git_ignored():
    """Regression: the root .gitignore ignores data/, so the folder is named dataset/."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "powerbi/dataset/model/run.parquet"], cwd=ROOT
    )
    assert result.returncode == 1


# ------------------------------------------------------------------ hand-calculated figures


@pytest.mark.parametrize("run_id", [RUN1, RUN2])
def test_run_totals_match_hand_calculation(run_id):
    h = HAND[run_id]
    cols = [k for k in h if k.endswith("_cents")]
    (got,) = q(f"SELECT {', '.join(cols)} FROM {m('run')} WHERE run_id = ?", [run_id])
    assert dict(zip(cols, got, strict=True)) == {c: h[c] for c in cols}
    (pct, status, completeness) = q(
        f"SELECT settled_invoice_pct, arithmetic_status, data_completeness FROM {m('run')} "
        "WHERE run_id = ?",
        [run_id],
    )[0]
    assert (str(pct), status, completeness) == (h["settled_pct"], "PASS", "INCOMPLETE")


@pytest.mark.parametrize("run_id", [RUN1, RUN2])
def test_expected_kpis_json_matches_hand_calculation(run_id):
    data = json.loads((POWERBI / "dataset" / "expected_kpis.json").read_text(encoding="utf-8"))
    (k,) = [r for r in data["runs"] if r["run_id"] == run_id]
    h = HAND[run_id]

    def usd(cents):
        return str((Decimal(cents) / 100).quantize(Decimal("0.01")))

    assert k["eligible_invoice_value"] == usd(h["eligible_invoice_cents"])
    assert k["settled_invoice_value"] == usd(h["settled_invoice_cents"])
    assert k["outstanding_balance"] == usd(h["outstanding_cents"])
    assert k["overpayments"] == usd(h["overpayment_cents"])
    assert k["net_difference"] == usd(h["eligible_invoice_cents"] - h["eligible_payment_cents"])
    assert k["settled_pct"] == h["settled_pct"]
    assert k["quarantined_rows"] == h["quarantined_rows"]
    assert (k["payment_exception_count"], k["payment_exception_amount"]) == (
        h["payment_exceptions"][0],
        usd(h["payment_exceptions"][1]),
    )
    assert k["exception_count"] == h["exceptions"]
    assert k["unresolved_exceptions"] == h["unresolved"]
    assert k["validation_exceptions"] == h["validation"]
    assert k["open_invoice_count"] == h["open_invoices"]
    assert k["customer_outstanding"] == {c: usd(v) for c, v in h["customers"].items()}
    assert list(k["customer_outstanding"]) == list(h["customers"])  # largest first


def test_settled_pct_agrees_with_independent_decimal():
    for run_id, h in HAND.items():
        pct = Decimal(h["settled_invoice_cents"]) / Decimal(h["eligible_invoice_cents"]) * 100
        assert str(pct.quantize(Decimal("0.01"))) == h["settled_pct"], run_id


def test_snapshot_change_log_lists_exactly_the_planned_changes():
    text = (POWERBI / "dataset" / "snapshot_changes.md").read_text(encoding="utf-8")
    changed = set(re.findall(r"\| (invoices|payments) \| `([^`]+)` \| \*\*(\w+)\*\*", text))
    assert changed == {
        ("invoices", "CUST-E / INV-1009", "changed"),  # duplicate row removed at source
        ("invoices", "CUST-F / INV-1011", "changed"),  # missing amount supplied
        ("payments", "PAY-5009", "changed"),  # reference corrected inv-1005 -> INV-1005
        ("invoices", "CUST-A / INV-1015", "new"),
        ("invoices", "CUST-H / INV-1016", "new"),
        ("payments", "PAY-5017", "new"),
        ("payments", "PAY-5018", "new"),
        ("payments", "PAY-5019", "new"),
        ("payments", "PAY-5020", "new"),
    }
    assert "removed" not in {c for *_, c in changed}
    assert text.count("| unchanged |") == 27


# ------------------------------------------------------------------ schema & keys


@pytest.mark.parametrize("table", sorted(EXTRAS))
def test_model_table_columns(table):
    view, extras = EXTRAS[table]
    described = q(f"DESCRIBE SELECT * FROM {m(table)}")
    assert tuple(r[0] for r in described) == VIEW_COLUMNS[view] + extras
    for name, dtype, *_ in described:
        assert dtype not in ("DOUBLE", "FLOAT", "REAL") and not dtype.endswith("[]"), name
        if name.endswith("_cents"):
            assert dtype == "BIGINT", name
        if name.endswith(("_usd", "_pct")):
            assert dtype == "DECIMAL(38,2)", name


def test_customer_and_bridge_tables():
    assert q(f"DESCRIBE SELECT * FROM {m('customer')}")[0][:2] == ("customer_id", "VARCHAR")
    assert [r[0] for r in q(f"DESCRIBE SELECT * FROM {m('bridge_step')}")] == [
        "run_id",
        "step_order",
        "step_code",
        "step_label",
        "step_kind",
        "amount_cents",
        "amount_usd",
        "signed_cents",
        "signed_usd",
    ]


@pytest.mark.parametrize(
    "table, key",
    [
        ("run", "run_id"),
        ("open_invoice", "run_invoice_key"),
        ("payment_exception", "run_payment_key"),
        ("exception", "run_exception_key"),
        ("customer", "customer_id"),
    ],
)
def test_keys_are_unique_and_not_null(table, key):
    (total, distinct, nulls) = q(
        f"SELECT count(*), count(DISTINCT {key}), count(*) FILTER (WHERE {key} IS NULL) "
        f"FROM {m(table)}"
    )[0]
    assert total > 0 and total == distinct and nulls == 0


def test_bridge_step_key_is_unique():
    assert q(f"SELECT count(*) = count(DISTINCT (run_id, step_order)) FROM {m('bridge_step')}") == [
        (True,)
    ]


def test_composite_keys_embed_their_run():
    for table, key in (
        ("open_invoice", "run_invoice_key"),
        ("payment_exception", "run_payment_key"),
        ("exception", "run_exception_key"),
    ):
        assert q(
            f"SELECT count(*) FROM {m(table)} WHERE NOT starts_with({key}, run_id || '|')"
        ) == [(0,)]


def test_exception_ids_repeat_across_runs_so_keys_must_be_composite():
    assert (
        q(
            f"SELECT count(*) FROM (SELECT exception_id FROM {m('exception')} "
            "GROUP BY exception_id HAVING count(DISTINCT run_id) > 1)"
        )[0][0]
        > 0
    )


def test_foreign_keys_resolve_within_the_same_run():
    for table in ("open_invoice", "payment_exception", "exception", "bridge_step"):
        assert q(
            f"SELECT count(*) FROM {m(table)} WHERE run_id NOT IN (SELECT run_id FROM {m('run')})"
        ) == [(0,)]
    for table in ("payment_exception", "open_invoice"):
        assert q(
            f"SELECT count(*) FROM {m(table)} WHERE run_exception_key IS NOT NULL AND "
            f"run_exception_key NOT IN (SELECT run_exception_key FROM {m('exception')})"
        ) == [(0,)]
    for table in ("open_invoice", "payment_exception", "exception"):
        assert q(
            f"SELECT count(*) FROM {m(table)} WHERE customer_id IS NOT NULL AND "
            f"customer_id NOT IN (SELECT customer_id FROM {m('customer')})"
        ) == [(0,)]


# ------------------------------------------------------------------ money & isolation


def test_usd_columns_equal_cents_exactly():
    for table in ("run", "open_invoice", "payment_exception", "exception", "bridge_step"):
        cols = [r[0] for r in q(f"DESCRIBE SELECT * FROM {m(table)}")]
        for col in cols:
            if not col.endswith("_usd"):
                continue
            cents = "amount_cents" if col == "amount_usd" else col[: -len("_usd")] + "_cents"
            for c, d in q(f"SELECT {cents}, {col} FROM {m(table)}"):
                assert (c is None and d is None) or Decimal(c) / 100 == d, (table, col)


def test_model_tables_are_the_union_of_per_run_exports():
    rk = POWERBI / "dataset" / "rk_v1"
    assert sorted(p.name for p in rk.iterdir()) == sorted([RUN1, RUN2])
    for table, (view, _) in EXTRAS.items():
        for run_dir in rk.iterdir():
            path = (run_dir / f"{view}.parquet").as_posix()
            assert q(f"SELECT DISTINCT run_id FROM read_parquet('{path}')") in (
                [(run_dir.name,)],
                [],
            )
            plain = [c for c in VIEW_COLUMNS[view] if c not in builder.LIST_COLUMNS]
            cols = ", ".join(plain)
            exported = q(f"SELECT {cols} FROM read_parquet('{path}') ORDER BY ALL")
            modelled = q(
                f"SELECT {cols} FROM {m(table)} WHERE run_id = ? ORDER BY ALL", [run_dir.name]
            )
            assert exported == modelled, (table, run_dir.name)


@pytest.mark.parametrize("run_id", [RUN1, RUN2])
def test_control_totals_tie_per_run(run_id):
    (o, u, mm, b, i, a, x, net, explained) = q(
        f"SELECT outstanding_cents, unmatched_payment_cents, ambiguous_payment_cents, "
        f"blocked_payment_cents, eligible_invoice_cents, applied_payment_cents, "
        f"overpayment_cents, net_difference_cents, explained_difference_cents "
        f"FROM {m('run')} WHERE run_id = ?",
        [run_id],
    )[0]
    open_sum = q(
        f"SELECT coalesce(sum(outstanding_cents), 0) FROM {m('open_invoice')} WHERE run_id = ?",
        [run_id],
    )[0][0]
    pay_sum = q(
        f"SELECT coalesce(sum(payment_amount_cents), 0) FROM {m('payment_exception')} "
        "WHERE run_id = ?",
        [run_id],
    )[0][0]
    steps = q(
        f"SELECT sum(signed_cents) FILTER (WHERE step_kind = 'explains'), "
        f"sum(signed_cents) FILTER (WHERE step_code IN ('I', 'P')) "
        f"FROM {m('bridge_step')} WHERE run_id = ?",
        [run_id],
    )[0]
    assert open_sum == o
    assert pay_sum == u + mm + b
    assert i - a == o - x
    assert net == explained == steps[0] == steps[1]


def test_report_default_is_the_latest_as_of_date():
    rows = q(f"SELECT run_id, as_of_date, is_report_default FROM {m('run')} ORDER BY as_of_date")
    assert [r[2] for r in rows] == [False, True]
    assert rows[-1][0] == RUN2


def test_scripted_reviews_apply_only_to_their_own_run():
    got = q(
        f"SELECT run_id, payment_id, invoice_id, rule_code, current_disposition, "
        f"review_event_count FROM {m('exception')} WHERE review_event_count > 0 "
        "ORDER BY run_id, payment_id NULLS FIRST, invoice_id"
    )
    assert got == [
        (RUN1, None, "INV-1005", "INVOICE_UNPAID", "ACKNOWLEDGED", 1),
        (RUN1, None, "INV-1009", "DUPLICATE_INVOICE_IDENTITY", "RESOLVED_EXTERNALLY", 1),
        (RUN1, "PAY-5009", "inv-1005", "PAYMENT_REFERENCE_NOT_FOUND", "RESOLVED_EXTERNALLY", 2),
        (RUN1, "PAY-5011", "INV-1001", "CUSTOMER_REFERENCE_CONFLICT", "INVESTIGATING", 1),
        (RUN1, "PAY-5012", "INV-1009", "AMBIGUOUS_INVOICE_REFERENCE", "ACKNOWLEDGED", 1),
        (RUN2, "PAY-5011", "INV-1001", "CUSTOMER_REFERENCE_CONFLICT", "INVESTIGATING", 1),
        (RUN2, "PAY-5020", "INV-1061", "PAYMENT_REFERENCE_NOT_FOUND", "INVESTIGATING", 1),
    ]


# ------------------------------------------------------------------ Power BI source files


def _m_sections() -> dict[str, list[tuple[str, str]]]:
    text = (POWERBI / "model" / "power-query.m").read_text(encoding="utf-8")
    sections = re.split(r"// ===== Query: (.+?) =====", text)[1:]
    return {
        name: re.findall(r'\{"(\w+)", ([\w.]+(?: \w+)?)\}', body)
        for name, body in zip(sections[::2], sections[1::2], strict=True)
    }


def test_power_query_types_every_column_from_the_parquet_schema():
    sections = _m_sections()
    assert list(sections) == list(builder.POWERBI_TABLES)
    for table, file in builder.POWERBI_TABLES.items():
        schema = q(f"DESCRIBE SELECT * FROM {m(file)}")
        assert [c for c, _ in sections[table]] == [r[0] for r in schema], table
        for (col, mtype), (_, dtype, *_) in zip(sections[table], schema, strict=True):
            assert mtype == builder.M_TYPES[dtype], (table, col)
            if col.endswith(("_usd", "_pct")):
                assert mtype == "Currency.Type", (table, col)  # Fixed decimal, never Decimal


def test_dax_references_only_existing_tables_columns_and_measures():
    dax = (POWERBI / "model" / "measures.dax").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", dax)
    measures = set(re.findall(r"MEASURE '[^']+'\[([^\]]+)\]", code))
    columns = {
        table: {r[0] for r in q(f"DESCRIBE SELECT * FROM {m(file)}")}
        for table, file in builder.POWERBI_TABLES.items()
    }
    refs = re.findall(r"'([^']+)'\[([^\]]+)\]", code)
    assert refs
    for table, name in refs:
        assert table in columns, f"unknown table {table!r}"
        assert name in columns[table] or name in measures, f"{table}[{name}] does not exist"
    for name in re.findall(r"(?<!')\[([^\]]+)\]", code):
        assert name in measures or any(name in c for c in columns.values()), name


def test_model_doc_lists_every_measure():
    dax = (POWERBI / "model" / "measures.dax").read_text(encoding="utf-8")
    doc = (POWERBI / "model" / "model.md").read_text(encoding="utf-8")
    for name in re.findall(r"MEASURE '[^']+'\[([^\]]+)\]", dax):
        assert f"`{name}`" in doc, name


def test_readme_kpi_table_matches_expected_kpis():
    data = json.loads((POWERBI / "dataset" / "expected_kpis.json").read_text(encoding="utf-8"))
    readme = (POWERBI / "README.md").read_text(encoding="utf-8")
    for run in data["runs"]:
        row = (
            f"| {run['snapshot_label']} | `{run['run_id']}` | {run['eligible_invoice_value']} | "
            f"{run['settled_invoice_value']} | {run['settled_pct']}% | "
            f"{run['outstanding_balance']} | {run['payment_exception_count']} / "
            f"{run['payment_exception_amount']} | {run['exception_count']} | "
            f"{run['unresolved_exceptions']} | {run['quarantined_rows']} | "
            f"{run['arithmetic_status']} |"
        )
        assert row in readme, row
