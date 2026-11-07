"""Build (or check) the synthetic Power BI demonstration dataset.

Pipeline, using only ReconcileKit's public functions:

    snapshot CSVs --reconcile_files--> run --save_run--> DuckDB --add_review_event-->
    rk_v1 views --export_parquet--> dataset/rk_v1/<run_id>/*.parquet
    --DuckDB SQL--> dataset/model/*.parquet  (+ expected_kpis.json, snapshot_changes.md)

Every input is fixed: the snapshot files, the as-of dates, the run creation times, and the
review events (with explicit timestamps). Running the script twice gives the same output.

    python powerbi/scripts/build_dataset.py           # rewrite powerbi/dataset/
    python powerbi/scripts/build_dataset.py --check   # exit 1 if the committed files are stale
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import duckdb

from reconcilekit.demo import load_demo
from reconcilekit.reconciliation import reconcile_files
from reconcilekit.reporting_views import VIEW_COLUMNS, export_parquet
from reconcilekit.repository import add_review_event, connect, save_run

POWERBI = Path(__file__).resolve().parents[1]
DATASET = POWERBI / "dataset"
SOURCE = DATASET / "source"

# Each snapshot is a complete point-in-time export of both registers, reconciled as one run.
SNAPSHOTS = (
    {
        "as_of": date(2026, 3, 31),
        "label": "2026-03 baseline",
        "order": 1,
        "source": "ReconcileKit bundled demo (src/reconcilekit/demo_data)",
        "created_at_utc": datetime(2026, 4, 1, 8, 0, 0),
    },
    {
        "as_of": date(2026, 4, 30),
        "label": "2026-04 follow-up",
        "order": 2,
        "source": "powerbi/dataset/source/snapshot-2026-04-30",
        "created_at_utc": datetime(2026, 5, 1, 8, 0, 0),
    },
)

# Columns of the model files consumed by Power BI, in order. Lists are joined with ';'
# so that every column is a scalar in Power Query.
MODEL_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "run": ("run_summary", ("snapshot_label", "snapshot_order", "is_report_default")),
    "open_invoice": ("open_invoices", ("run_invoice_key", "run_exception_key", "aging_order")),
    "payment_exception": ("payment_exceptions", ("run_payment_key", "run_exception_key")),
    "exception": ("exception_status", ("run_exception_key",)),
}
LIST_COLUMNS = {"related_invoice_record_ids", "record_ids"}


def _snapshot_bytes(snapshot: dict) -> tuple[bytes, bytes]:
    if snapshot["order"] == 1:
        return load_demo()
    folder = POWERBI.parent / snapshot["source"]
    return (folder / "invoices.csv").read_bytes(), (folder / "payments.csv").read_bytes()


def _apply_reviews(db: Path, run_ids: dict[str, str]) -> None:
    events = json.loads((SOURCE / "review_events.json").read_text(encoding="utf-8"))["events"]
    for ev in events:
        run_id = run_ids[ev["snapshot"]]
        where = " AND ".join(f"{k} = ?" for k in ev["select"])
        with connect(db) as con:
            matches = con.execute(
                f"SELECT exception_id FROM rk_v1.exception_status WHERE run_id = ? AND {where}",
                [run_id, *ev["select"].values()],
            ).fetchall()
        if len(matches) != 1:
            raise SystemExit(f"{ev['event_id']}: selector matched {len(matches)} exceptions")
        add_review_event(
            db,
            run_id,
            matches[0][0],
            ev["reviewer"],
            ev["disposition"],
            ev["note"],
            event_id=ev["event_id"],
            created_at_utc=datetime.fromisoformat(ev["created_at_utc"]),
        )


def _sql_path(path: Path) -> str:
    """A SQL string literal for a file path (forward slashes, quotes doubled)."""
    return "'" + path.as_posix().replace("'", "''") + "'"


def _select_list(view: str) -> str:
    cols = []
    for col in VIEW_COLUMNS[view]:
        cols.append(f"array_to_string({col}, ';') AS {col}" if col in LIST_COLUMNS else col)
    return ", ".join(cols)


_EXTRA_SQL = {
    "snapshot_label": "v.snapshot_label",
    "snapshot_order": "v.snapshot_order",
    # Report default: latest as-of date; ties go to the latest stored run, then run_id.
    "is_report_default": (
        "row_number() OVER (ORDER BY v.as_of_date DESC, v.created_at_utc DESC, v.run_id DESC) = 1"
    ),
    "run_invoice_key": "v.run_id || '|' || v.invoice_record_id",
    "run_payment_key": "v.run_id || '|' || v.payment_record_id",
    "run_exception_key": "CASE WHEN v.exception_id IS NULL THEN NULL "
    "ELSE v.run_id || '|' || v.exception_id END",
    "aging_order": "CASE v.aging_bucket WHEN 'NOT_OVERDUE' THEN 0 WHEN 'DAYS_1_30' THEN 1 "
    "WHEN 'DAYS_31_60' THEN 2 WHEN 'DAYS_61_90' THEN 3 ELSE 4 END",
}
_ORDER = {
    "run": "v.snapshot_order",
    "open_invoice": "v.run_id, v.source_row",
    "payment_exception": "v.run_id, v.source_row",
    "exception": "v.run_id, v.exception_id",
}


def _build_model(con: duckdb.DuckDBPyConnection, rk_dir: Path, out: Path, labels: dict) -> None:
    con.execute(
        "CREATE TEMP TABLE snapshot_meta (run_id VARCHAR, snapshot_label VARCHAR, "
        "snapshot_order INTEGER)"
    )
    con.executemany("INSERT INTO snapshot_meta VALUES (?, ?, ?)", list(labels.values()))
    for table, (view, extras) in MODEL_TABLES.items():
        files = sorted(_sql_path(p) for p in rk_dir.glob(f"*/{view}.parquet"))
        base = f"SELECT {_select_list(view)} FROM read_parquet([{', '.join(files)}])"
        if table == "run":
            base = (
                f"SELECT b.*, s.snapshot_label, s.snapshot_order FROM ({base}) AS b "
                "JOIN snapshot_meta AS s USING (run_id)"
            )
        cols = ", ".join(f"v.{c}" for c in VIEW_COLUMNS[view])
        extra_sql = ", ".join(f"{_EXTRA_SQL[c]} AS {c}" for c in extras)
        con.sql(
            f"SELECT {cols}, {extra_sql} FROM ({base}) AS v ORDER BY {_ORDER[table]}"
        ).write_parquet(str(out / f"{table}.parquet"))

    con.sql(
        f"""
        SELECT DISTINCT customer_id FROM (
            SELECT customer_id FROM read_parquet({_sql_path(out / "open_invoice.parquet")})
            UNION ALL
            SELECT customer_id FROM read_parquet({_sql_path(out / "payment_exception.parquet")})
            UNION ALL
            SELECT customer_id FROM read_parquet({_sql_path(out / "exception.parquet")})
        )
        WHERE customer_id IS NOT NULL
        ORDER BY customer_id
        """
    ).write_parquet(str(out / "customer.parquet"))

    # Reconciliation bridge, one row per line; signed_cents drives a waterfall visual whose
    # explanation steps sum to the net difference.
    con.sql(
        f"""
        WITH r AS (SELECT * FROM read_parquet({_sql_path(out / "run.parquet")}))
        SELECT run_id, step_order, step_code, step_label, step_kind, amount_cents,
               rk_cents_to_usd(amount_cents) AS amount_usd,
               signed_cents, rk_cents_to_usd(signed_cents) AS signed_usd
        FROM (
            SELECT run_id, 1 AS step_order, 'I' AS step_code, 'Eligible invoices' AS step_label,
                   'total' AS step_kind, eligible_invoice_cents AS amount_cents,
                   eligible_invoice_cents AS signed_cents FROM r
            UNION ALL SELECT run_id, 2, 'P', 'Eligible payments', 'total',
                   eligible_payment_cents, -eligible_payment_cents FROM r
            UNION ALL SELECT run_id, 3, 'I-P', 'Net difference', 'subtotal',
                   net_difference_cents, net_difference_cents FROM r
            UNION ALL SELECT run_id, 4, 'O', 'Outstanding invoice balances', 'explains',
                   outstanding_cents, outstanding_cents FROM r
            UNION ALL SELECT run_id, 5, 'X', 'Overpayments', 'explains',
                   overpayment_cents, -overpayment_cents FROM r
            UNION ALL SELECT run_id, 6, 'U', 'Unmatched payments', 'explains',
                   unmatched_payment_cents, -unmatched_payment_cents FROM r
            UNION ALL SELECT run_id, 7, 'M', 'Ambiguous payments', 'explains',
                   ambiguous_payment_cents, -ambiguous_payment_cents FROM r
            UNION ALL SELECT run_id, 8, 'B', 'Blocked payments', 'explains',
                   blocked_payment_cents, -blocked_payment_cents FROM r
            UNION ALL SELECT run_id, 9, 'O-X-U-M-B', 'Explained difference', 'subtotal',
                   explained_difference_cents, explained_difference_cents FROM r
        )
        ORDER BY run_id, step_order
        """
    ).write_parquet(str(out / "bridge_step.parquet"))


def _expected_kpis(con: duckdb.DuckDBPyConnection, model: Path) -> dict:
    """KPI values per run, computed in SQL with the same definitions as the DAX catalog."""
    m = {t: f"read_parquet({_sql_path(model / (t + '.parquet'))})" for t in MODEL_TABLES}
    rows = con.execute(
        f"""
        SELECT r.run_id, r.snapshot_label, r.as_of_date, r.is_report_default,
               r.eligible_invoice_usd, r.settled_invoice_usd, r.settled_invoice_pct,
               r.outstanding_usd, r.overpayment_usd, r.net_difference_usd,
               r.quarantined_invoice_rows + r.quarantined_payment_rows,
               r.arithmetic_status, r.data_completeness,
               (SELECT count(*) FROM {m["payment_exception"]} p WHERE p.run_id = r.run_id),
               (SELECT coalesce(sum(payment_amount_cents), 0) FROM {m["payment_exception"]} p
                 WHERE p.run_id = r.run_id),
               (SELECT count(*) FROM {m["exception"]} e WHERE e.run_id = r.run_id),
               (SELECT count(*) FROM {m["exception"]} e
                 WHERE e.run_id = r.run_id AND NOT e.is_resolved),
               (SELECT count(*) FROM {m["exception"]} e
                 WHERE e.run_id = r.run_id AND e.category = 'VALIDATION'),
               (SELECT count(*) FROM {m["open_invoice"]} o WHERE o.run_id = r.run_id)
        FROM {m["run"]} r ORDER BY r.snapshot_order
        """
    ).fetchall()
    customers = con.execute(
        f"""
        SELECT run_id, customer_id, sum(outstanding_cents) FROM {m["open_invoice"]}
        GROUP BY ALL ORDER BY run_id, sum(outstanding_cents) DESC, customer_id
        """
    ).fetchall()
    by_customer: dict[str, dict[str, str]] = defaultdict(dict)
    for run_id, cust, cents in customers:
        by_customer[run_id][cust] = _usd(cents)
    out = {"_comment": "Generated by build_dataset.py from dataset/model; see README.", "runs": []}
    for r in rows:
        out["runs"].append(
            {
                "run_id": r[0],
                "snapshot_label": r[1],
                "as_of_date": r[2].isoformat(),
                "is_report_default": r[3],
                "eligible_invoice_value": str(r[4]),
                "settled_invoice_value": str(r[5]),
                "settled_pct": str(r[6]),
                "outstanding_balance": str(r[7]),
                "overpayments": str(r[8]),
                "net_difference": str(r[9]),
                "quarantined_rows": r[10],
                "arithmetic_status": r[11],
                "data_completeness": r[12],
                "payment_exception_count": r[13],
                "payment_exception_amount": _usd(r[14]),
                "exception_count": r[15],
                "unresolved_exceptions": r[16],
                "validation_exceptions": r[17],
                "open_invoice_count": r[18],
                "customer_outstanding": by_customer[r[0]],
            }
        )
    return out


def _usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}{whole}.{frac:02d}"


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))[1:]


def snapshot_changes() -> str:
    """Row-level comparison of the two snapshots, grouped by business identity."""
    first, second = _snapshot_bytes(SNAPSHOTS[0]), _snapshot_bytes(SNAPSHOTS[1])
    lines = [
        "# Snapshot changes: 2026-03-31 → 2026-04-30",
        "",
        "Generated by `powerbi/scripts/build_dataset.py`; do not edit. Each snapshot is a",
        "complete export of both registers. Rows are grouped by identity (invoice:",
        "`customer_id + invoice_id`; payment: `payment_id`) after trimming whitespace.",
        "",
        "| Register | Identity | Change | 2026-03-31 rows | 2026-04-30 rows |",
        "|---|---|---|---|---|",
    ]
    for kind, idx, key in (("invoices", 0, (1, 0)), ("payments", 1, (0,))):
        old: dict[tuple, list] = defaultdict(list)
        new: dict[tuple, list] = defaultdict(list)
        for target, data in ((old, first[idx]), (new, second[idx])):
            for row in _rows(data):
                target[tuple(row[k].strip() for k in key)].append(row)
        for ident in sorted(set(old) | set(new)):
            a, b = old.get(ident, []), new.get(ident, [])
            change = (
                "new" if not a else "removed" if not b else "unchanged" if a == b else "changed"
            )
            fmt = lambda rows: "<br>".join(",".join(r) for r in rows) or "—"  # noqa: E731
            lines.append(
                f"| {kind} | `{' / '.join(ident)}` | **{change}** | {fmt(a)} | {fmt(b)} |"
                if change != "unchanged"
                else f"| {kind} | `{' / '.join(ident)}` | unchanged | {len(a)} row(s) | same |"
            )
    return "\n".join(lines) + "\n"


def build(out: Path) -> None:
    """Write the complete dataset into ``out`` (which must not exist)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo.duckdb"
        run_ids: dict[str, str] = {}
        labels: dict[str, tuple] = {}
        for snap in SNAPSHOTS:
            inv, pay = _snapshot_bytes(snap)
            run = reconcile_files(inv, pay, snap["as_of"])
            run_id = save_run(db, run, created_at_utc=snap["created_at_utc"]).run_id
            run_ids[snap["as_of"].isoformat()] = run_id
            labels[run_id] = (run_id, snap["label"], snap["order"])
        _apply_reviews(db, run_ids)

        rk_dir = out / "rk_v1"
        for run_id in run_ids.values():
            export_parquet(db, run_id, rk_dir / run_id)
        model = out / "model"
        model.mkdir(parents=True)
        con = duckdb.connect()
        try:
            con.execute(
                "CREATE MACRO rk_cents_to_usd(c) AS "
                "CAST(CAST(c AS DECIMAL(38, 0)) * CAST(0.01 AS DECIMAL(3, 2)) AS DECIMAL(38, 2))"
            )
            _build_model(con, rk_dir, model, labels)
            kpis = _expected_kpis(con, model)
        finally:
            con.close()
    (out / "expected_kpis.json").write_text(
        json.dumps(kpis, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "snapshot_changes.md").write_text(snapshot_changes(), encoding="utf-8", newline="\n")


GENERATED = ("rk_v1", "model", "expected_kpis.json", "snapshot_changes.md")

# Power BI table name -> model file. The names are used by power-query.m and measures.dax.
POWERBI_TABLES = {
    "Run": "run",
    "Open Invoice": "open_invoice",
    "Payment Exception": "payment_exception",
    "Exception": "exception",
    "Customer": "customer",
    "Bridge Step": "bridge_step",
}
# DuckDB/Parquet type -> Power Query type. Money and percentage-point columns become
# Fixed decimal (Currency.Type, exact to 4 places); cents stay 64-bit whole numbers.
M_TYPES = {
    "VARCHAR": "type text",
    "BIGINT": "Int64.Type",
    "INTEGER": "Int64.Type",
    "DECIMAL(38,2)": "Currency.Type",
    "BOOLEAN": "type logical",
    "DATE": "type date",
    "TIMESTAMP": "type datetime",
}
POWER_QUERY = POWERBI / "model" / "power-query.m"


def power_query_m(model_dir: Path = DATASET / "model") -> str:
    """Power Query (M) for every model table, with every column typed explicitly."""
    parts = [
        "// Generated by powerbi/scripts/build_dataset.py from the model Parquet schemas.",
        "// The same queries are the table partitions in powerbi/RK.SemanticModel, where they",
        "// were executed by Power BI Desktop (see powerbi/validation/desktop-validation.md).",
        "//",
        "// To use them in another report: create a text parameter named DataFolder holding the",
        "// absolute path of powerbi\\dataset\\model (Power Query does not resolve relative",
        "// paths), then add one blank query per section, named exactly as the section title.",
    ]
    for table, file in POWERBI_TABLES.items():
        schema, _ = _parquet_rows(model_dir / f"{file}.parquet")
        types = ",\n".join(f'            {{"{col}", {M_TYPES[dtype]}}}' for col, dtype in schema)
        parts.append(
            f"""
// ===== Query: {table} =====
let
    Source = Parquet.Document(File.Contents(DataFolder & "\\{file}.parquet")),
    Typed = Table.TransformColumnTypes(
        Source,
        {{
{types}
        }}
    )
in
    Typed"""
        )
    return "\n".join(parts) + "\n"


def _parquet_rows(path: Path) -> tuple[list, list]:
    con = duckdb.connect()
    try:
        cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        schema = [(d[0], str(d[1])) for d in cur.description]
        return schema, cur.fetchall()
    finally:
        con.close()


def _generated_files(root: Path) -> set[str]:
    names = set()
    for g in GENERATED:
        target = root / g
        if target.is_file():
            names.add(g)
        elif target.is_dir():
            names |= {p.relative_to(root).as_posix() for p in target.rglob("*") if p.is_file()}
    return names


def stale_files(committed: Path = DATASET) -> list[str]:
    """Generated files that are missing, extra, or different from a fresh build."""
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "dataset"
        build(fresh)
        names = _generated_files(fresh) | _generated_files(committed)
        stale = []
        for name in sorted(names):
            a, b = fresh / name, committed / name
            if not a.exists() or not b.exists():
                stale.append(name)
            elif name.endswith(".parquet"):
                if _parquet_rows(a) != _parquet_rows(b):
                    stale.append(name)
            elif a.read_text(encoding="utf-8") != b.read_text(encoding="utf-8").replace(
                "\r\n", "\n"
            ):
                stale.append(name)
        m_current = POWER_QUERY.read_text(encoding="utf-8") if POWER_QUERY.exists() else None
        if m_current is None or m_current.replace("\r\n", "\n") != power_query_m(fresh / "model"):
            stale.append("../model/power-query.m")
        return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Only report stale files")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_files()
        for name in stale:
            print(f"stale: powerbi/dataset/{name}", file=sys.stderr)
        return 1 if stale else 0
    for g in GENERATED:
        target = DATASET / g
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "dataset"
        build(fresh)
        for item in fresh.iterdir():
            dest = DATASET / item.name
            shutil.copytree(item, dest) if item.is_dir() else shutil.copy2(item, dest)
    POWER_QUERY.parent.mkdir(parents=True, exist_ok=True)
    POWER_QUERY.write_text(power_query_m(), encoding="utf-8", newline="\n")
    print("wrote powerbi/dataset/{" + ",".join(GENERATED) + "} and powerbi/model/power-query.m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
