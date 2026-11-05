"""Command-line interface. Uses the same engine and database as the Streamlit app.

Exit codes
----------
0  Run completed, every source row was eligible, and invariants passed. Business
   findings (unpaid invoices, unmatched payments, ...) do NOT change the exit code;
   inspect ``exception_count`` in the output (or ``--json``) to act on findings.
2  Invalid input: a file-level error prevented the run, or the run completed but
   some rows were quarantined (incomplete). An incomplete run is still saved and
   exported so the quarantined rows can be reviewed. Command-line usage errors, and
   ``query``/``parquet`` requests that cannot be served (missing database or run,
   invalid SQL), also exit 2.
1  Internal failure, including a financial-invariant violation (nothing is saved).

Set ``RECONCILEKIT_DEBUG=1`` (or ``true``/``yes``/``on``) to print the full traceback of
an unexpected internal error to stderr. Any other value leaves it off. Exit codes are the
same either way.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import traceback
from datetime import date
from pathlib import Path

from reconcilekit import reporting
from reconcilekit.demo import DEMO_AS_OF, load_demo
from reconcilekit.exports import (
    ExportError,
    build_export_package,
    escape_formula,
    export_filename,
)
from reconcilekit.ingestion import FileValidationError
from reconcilekit.money import format_usd
from reconcilekit.normalization import parse_iso_date
from reconcilekit.reconciliation import InvariantViolation, reconcile_files
from reconcilekit.reporting_views import ReportingError, export_parquet, run_query
from reconcilekit.repository import connect, save_run

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID_INPUT = 2
DEFAULT_DB = Path("data") / "reconcilekit.duckdb"
DEBUG_ENV = "RECONCILEKIT_DEBUG"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _as_of(text: str) -> date:
    parsed = parse_iso_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"as-of date must be YYYY-MM-DD, got {text!r}")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcilekit",
        description="Reconcile an invoice register against a payment ledger (USD, exact cents).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DuckDB path ({DEFAULT_DB})")
        p.add_argument("--export-dir", type=Path, help="Write the reconciliation ZIP here")
        p.add_argument("--json", action="store_true", help="Print a machine-readable summary")

    rec = sub.add_parser("reconcile", help="Reconcile two CSV files")
    rec.add_argument("--invoices", type=Path, required=True, help="Invoice register CSV")
    rec.add_argument("--payments", type=Path, required=True, help="Payment ledger CSV")
    rec.add_argument("--as-of", type=_as_of, required=True, help="Snapshot date YYYY-MM-DD")
    common(rec)

    demo = sub.add_parser("demo", help=f"Reconcile the bundled demo (as of {DEMO_AS_OF})")
    common(demo)

    exp = sub.add_parser("export", help="Re-export a stored run with current review history")
    exp.add_argument("--run-id", required=True)
    exp.add_argument("--db", type=Path, default=DEFAULT_DB)
    exp.add_argument("--export-dir", type=Path, required=True)

    query = sub.add_parser(
        "query", help="Run one read-only SQL statement (e.g. against the rk_v1 views)"
    )
    query.add_argument("--db", type=Path, default=DEFAULT_DB)
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("--sql", help="SQL text")
    source.add_argument("--file", type=Path, help="File containing one SQL statement")
    query.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Named parameter referenced in the SQL as $NAME (repeatable)",
    )
    query.add_argument("--format", choices=("csv", "markdown"), default="csv")

    parquet = sub.add_parser("parquet", help="Export the rk_v1 views for one run as Parquet")
    parquet.add_argument("--run-id", required=True)
    parquet.add_argument("--db", type=Path, default=DEFAULT_DB)
    parquet.add_argument("--out-dir", type=Path, required=True)
    return parser


def _text(value: object) -> str:
    """Stable text for query output: exact decimals, ISO dates, lists joined by ';'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(_text(v) for v in value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _query(args: argparse.Namespace) -> int:
    params: dict[str, str] = {}
    for item in args.param:
        name, sep, value = item.partition("=")
        if not sep or not name.isidentifier():
            raise ReportingError(f"--param must look like NAME=VALUE, got {item!r}")
        params[name] = value
    sql = args.sql if args.sql is not None else args.file.read_text(encoding="utf-8")
    columns, rows = run_query(args.db, sql, params)
    if args.format == "csv":
        # Untrusted text cells get the same formula escaping as the export package.
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_text(escape_formula(v)) for v in row])
    else:
        print("| " + " | ".join(columns) + " |")
        print("|" + "|".join("---" for _ in columns) + "|")
        for row in rows:
            cells = [_text(v).replace("|", "\\|").replace("\n", " ") for v in row]
            print("| " + " | ".join(cells) + " |")
    return EXIT_OK


def _write_export(db: Path, run_id: str, export_dir: Path) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / export_filename(run_id)
    path.write_bytes(build_export_package(db, run_id))
    return path


def _report(db: Path, run_id: str, created: bool, export: Path | None, as_json: bool) -> int:
    with connect(db) as con:
        run = reporting.get_run(con, run_id)
        dispositions = reporting.disposition_counts(con, run_id)
    assert run is not None
    complete = bool(run["is_complete"])
    code = EXIT_OK if complete else EXIT_INVALID_INPUT
    if as_json:
        print(
            json.dumps(
                {
                    "status": "complete" if complete else "incomplete",
                    "exit_code": code,
                    "run_id": run_id,
                    "new_run": created,
                    "as_of_date": str(run["as_of_date"]),
                    "quarantined_invoice_rows": run["quarantined_invoice_rows"],
                    "quarantined_payment_rows": run["quarantined_payment_rows"],
                    "exception_count": run["exception_count"],
                    "exception_dispositions": dispositions,
                    "invoice_status_counts": run["invoice_status_counts"],
                    "payment_status_counts": run["payment_status_counts"],
                    "net_difference_cents": run["eligible_invoice_cents"]
                    - run["eligible_payment_cents"],
                    "export_path": str(export) if export else None,
                },
                indent=2,
            )
        )
        return code
    print(f"Run {run_id} ({'new' if created else 'reused: identical inputs and settings'})")
    print(f"As-of date: {run['as_of_date']}")
    if complete:
        print("Data completeness: COMPLETE (all rows eligible)")
    else:
        print(
            f"Data completeness: INCOMPLETE - {run['quarantined_invoice_rows']} invoice and "
            f"{run['quarantined_payment_rows']} payment rows quarantined. Totals cover eligible "
            "records only."
        )
    for line in reporting.bridge(run):
        print(f"  {line.sign:>1} {line.label:<30} {format_usd(line.cents):>16}")
    for check, ok, _ in reporting.invariant_checks(run):
        print(f"  Invariant {check}: {'PASS' if ok else 'FAIL'}")
    print(f"Exceptions requiring review: {run['exception_count']} {dispositions}")
    if export:
        print(f"Export written: {export}")
    return code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "query":
            return _query(args)
        if args.command == "parquet":
            for path in export_parquet(args.db, args.run_id, args.out_dir):
                print(f"Parquet written: {path}")
            return EXIT_OK
        if args.command == "export":
            path = _write_export(args.db, args.run_id, args.export_dir)
            print(f"Export written: {path}")
            return EXIT_OK
        if args.command == "demo":
            inv_bytes, pay_bytes = load_demo()
            as_of, inv_label, pay_label = DEMO_AS_OF, "demo_invoices.csv", "demo_payments.csv"
        else:
            inv_bytes, pay_bytes = args.invoices.read_bytes(), args.payments.read_bytes()
            as_of, inv_label, pay_label = args.as_of, args.invoices.name, args.payments.name
        run = reconcile_files(inv_bytes, pay_bytes, as_of, inv_label, pay_label)
        outcome = save_run(args.db, run)
        export = (
            _write_export(args.db, outcome.run_id, args.export_dir) if args.export_dir else None
        )
        return _report(args.db, outcome.run_id, outcome.created, export, args.json)
    except FileValidationError as exc:
        print("Input rejected; no run was created:", file=sys.stderr)
        for kind, messages in exc.problems.items():
            for message in messages:
                print(f"  [{kind}] {message}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        print(f"Cannot read input: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except (ExportError, ReportingError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID_INPUT
    except InvariantViolation as exc:
        print(f"Financial invariant failed; run not saved: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # last-resort guard so callers get a defined exit code
        print(f"Internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get(DEBUG_ENV, "").strip().lower() in _TRUTHY:
            traceback.print_exc(file=sys.stderr)
        else:
            print(f"Set {DEBUG_ENV}=1 to print the full traceback.", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
