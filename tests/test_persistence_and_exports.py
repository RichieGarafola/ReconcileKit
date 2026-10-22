from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import date

import pytest

from conftest import INV_HEADER, PAY_HEADER, csv_bytes, inv, pay, run_rows
from reconcilekit import reporting
from reconcilekit.cli import main
from reconcilekit.demo import DEMO_AS_OF, load_demo
from reconcilekit.exports import PACKAGE_FILES, build_export_package, escape_formula
from reconcilekit.reconciliation import compute_fingerprint, reconcile_files
from reconcilekit.repository import ReviewError, add_review_event, connect, save_run

TABLES = (
    "runs",
    "source_rows",
    "validation_issues",
    "invoice_results",
    "payment_results",
    "exceptions",
    "review_events",
)


def _counts(db_path) -> dict[str, int]:
    with connect(db_path) as con:
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def _demo_run():
    i, p = load_demo()
    return reconcile_files(i, p, DEMO_AS_OF)


def _zip_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8"))))


# ------------------------------------------------------------------ persistence


def test_repeated_import_reuses_run_without_duplicating_facts(db_path):
    first = save_run(db_path, _demo_run())
    add_review_event(
        db_path, first.run_id, "EX-0001", "analyst", "ACKNOWLEDGED", "seen", event_id="evt-1"
    )
    before = _counts(db_path)
    second = save_run(db_path, _demo_run())
    assert first.created and not second.created
    assert first.run_id == second.run_id
    assert _counts(db_path) == before


def test_as_of_content_and_rule_changes_create_new_runs(db_path):
    inv_b, pay_b = load_demo()
    base = reconcile_files(inv_b, pay_b, DEMO_AS_OF)
    other_date = reconcile_files(inv_b, pay_b, date(2026, 4, 30))
    other_bytes = reconcile_files(
        inv_b + b"INV-2000,CUST-Z,2026-01-01,2026-02-01,USD,1.00\n", pay_b, DEMO_AS_OF
    )
    ids = {save_run(db_path, r).run_id for r in (base, other_date, other_bytes)}
    assert len(ids) == 3
    sha_i, sha_p = base.invoice_source.sha256, base.payment_source.sha256
    assert compute_fingerprint(sha_i, sha_p, DEMO_AS_OF) == base.fingerprint
    assert compute_fingerprint(sha_i, sha_p, DEMO_AS_OF, rule_version="rules-v2") != (
        base.fingerprint
    )
    assert compute_fingerprint(sha_i, sha_p, DEMO_AS_OF, config={"tolerance": 1}) != (
        base.fingerprint
    )


def test_byte_level_identity_only(db_path):
    # Same logical rows, different bytes (CRLF) -> distinct run. Documented v1 behavior.
    a = run_rows([inv()], [pay()])
    b = reconcile_files(
        csv_bytes(INV_HEADER, [inv()]).replace(b"\n", b"\r\n"),
        csv_bytes(PAY_HEADER, [pay()]),
        date(2026, 3, 31),
    )
    assert a.run_id != b.run_id
    assert a.summary == b.summary


def test_failed_save_is_atomic(db_path):
    run = _demo_run()
    bad = replace(run, exceptions=run.exceptions + run.exceptions[:1])  # duplicate PK
    with pytest.raises(Exception):  # noqa: B017 - any DB constraint error
        save_run(db_path, bad)
    assert _counts(db_path) == dict.fromkeys(TABLES, 0)
    assert save_run(db_path, run).created


def test_review_history_is_append_only_and_latest_wins(db_path):
    run_id = save_run(db_path, _demo_run()).run_id
    add_review_event(db_path, run_id, "EX-0014", "ana", "INVESTIGATING", "calling customer")
    add_review_event(db_path, run_id, "EX-0014", "ana", "ACKNOWLEDGED", "")
    add_review_event(db_path, run_id, "EX-0014", "ben", "RESOLVED_EXTERNALLY", "credit memo 7")
    with connect(db_path) as con:
        history = reporting.review_history(con, run_id, "EX-0014")
        exc = reporting.exceptions_with_disposition(con, run_id).set_index("exception_id")
    assert list(history["disposition"]) == ["INVESTIGATING", "ACKNOWLEDGED", "RESOLVED_EXTERNALLY"]
    assert exc.loc["EX-0014", "disposition"] == "RESOLVED_EXTERNALLY"
    assert exc.loc["EX-0014", "review_event_count"] == 3
    assert exc.loc["EX-0001", "disposition"] == "OPEN"


def test_review_event_replay_is_idempotent(db_path):
    run_id = save_run(db_path, _demo_run()).run_id
    first = add_review_event(db_path, run_id, "EX-0001", "a", "ACKNOWLEDGED", event_id="same")
    again = add_review_event(db_path, run_id, "EX-0001", "a", "ACKNOWLEDGED", event_id="same")
    assert first.inserted and not again.inserted
    assert _counts(db_path)["review_events"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"disposition": "RESOLVED_EXTERNALLY", "note": "  "}, "note is required"),
        ({"reviewer": " "}, "reviewer label"),
        ({"disposition": "DELETED"}, "Unknown disposition"),
        ({"exception_id": "EX-9999"}, "does not exist"),
    ],
)
def test_invalid_reviews_rejected(db_path, kwargs, message):
    run_id = save_run(db_path, _demo_run()).run_id
    args = {
        "exception_id": "EX-0001",
        "reviewer": "a",
        "disposition": "ACKNOWLEDGED",
        "note": "",
        **kwargs,
    }
    with pytest.raises(ReviewError, match=message):
        add_review_event(db_path, run_id, **args)
    assert _counts(db_path)["review_events"] == 0


def test_reviews_do_not_change_findings_or_totals(db_path):
    run_id = save_run(db_path, _demo_run()).run_id

    def snapshot():
        with connect(db_path) as con:
            return (
                reporting.get_run(con, run_id),
                reporting.invoice_results(con, run_id).to_json(date_format="iso"),
                reporting.payment_results(con, run_id).to_json(date_format="iso"),
                reporting.exceptions_with_disposition(con, run_id)
                .drop(
                    columns=[
                        "disposition",
                        "last_reviewer",
                        "last_reviewed_at_utc",
                        "review_event_count",
                    ]
                )
                .to_json(),
            )

    before = snapshot()
    for n in range(1, 20):
        add_review_event(db_path, run_id, f"EX-{n:04d}", "r", "RESOLVED_EXTERNALLY", "done")
    after = snapshot()
    assert before == after
    assert not after[0]["is_complete"]  # reviews never make an incomplete run complete


# ------------------------------------------------------------------ exports


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ('=HYPERLINK("x")', '\'=HYPERLINK("x")'),
        ("+1", "'+1"),
        ("-1+2", "'-1+2"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("  =1+1", "'  =1+1"),
        ("\tcmd", "'\tcmd"),
        ("\rcmd", "'\rcmd"),
        ("INV-1001", "INV-1001"),
        ("000123", "000123"),
        ("", ""),
    ],
)
def test_escape_formula(value, escaped):
    assert escape_formula(value) == escaped


def test_export_package_contents_precision_and_injection(db_path):
    invoices = [inv("=1+1", "000777", amount="10.10"), inv("000123", "00042", amount="0.01")]
    payments = [
        pay("@P1", "=1+1", "000777", amount="10.1"),
        pay("P2", "000123", "00042", amount="0.01"),
        pay("P3", "", "C9", amount="1.00"),
    ]
    run = run_rows(invoices, payments)
    run_id = save_run(db_path, run).run_id
    add_review_event(
        db_path, run_id, run.exceptions[0].exception_id, "+reviewer", "ACKNOWLEDGED", "-note"
    )
    data = build_export_package(db_path, run_id)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == sorted(PACKAGE_FILES)
        inv_rows = _zip_csv(zf, "invoice_results.csv")
        pay_rows = _zip_csv(zf, "payment_results.csv")
        history = _zip_csv(zf, "review_history.csv")
        manifest = json.loads(zf.read("manifest.json"))
        summary = json.loads(zf.read("summary.json"))
        for name, meta in manifest["files"].items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == meta["sha256"]

    by_id = {r["invoice_id"]: r for r in inv_rows}
    assert by_id["'=1+1"]["invoice_amount"] == "10.10"
    assert by_id["'=1+1"]["customer_id"] == "000777"
    assert by_id["000123"]["customer_id"] == "00042"
    assert by_id["000123"]["applied_payment_amount"] == "0.01"
    assert {r["payment_id"] for r in pay_rows} == {"'@P1", "P2", "P3"}
    assert next(r for r in pay_rows if r["payment_id"] == "'@P1")["payment_amount"] == "10.10"
    assert history[0]["reviewer"] == "'+reviewer" and history[0]["note"] == "'-note"
    assert manifest["run_id"] == run_id
    assert manifest["sources"]["invoices"]["sha256"] == run.invoice_source.sha256
    assert manifest["as_of_date"] == "2026-03-31"
    assert {"rule_version", "engine_version", "exported_at_utc"} <= manifest.keys()
    assert summary["money_usd"]["I_eligible_invoices"] == {"amount": "10.11", "cents": 1011}
    assert summary["money_usd"]["U_unmatched_payments"]["amount"] == "1.00"
    assert all(c["passed"] for c in summary["invariants"])
    assert summary["exception_dispositions_at_export"]["ACKNOWLEDGED"] == 1


def test_export_reflects_review_history_at_export_time(db_path):
    run_id = save_run(db_path, _demo_run()).run_id
    before = build_export_package(db_path, run_id)
    add_review_event(db_path, run_id, "EX-0001", "r", "INVESTIGATING", "")
    after = build_export_package(db_path, run_id)
    with zipfile.ZipFile(io.BytesIO(before)) as a, zipfile.ZipFile(io.BytesIO(after)) as b:
        assert _zip_csv(a, "review_history.csv") == []
        assert len(_zip_csv(b, "review_history.csv")) == 1
        assert a.read("invoice_results.csv") == b.read("invoice_results.csv")


# ------------------------------------------------------------------ end to end


def _demo_paths(tmp_path):
    inv_b, pay_b = load_demo()
    (tmp_path / "invoices.csv").write_bytes(inv_b)
    (tmp_path / "payments.csv").write_bytes(pay_b)
    return tmp_path / "invoices.csv", tmp_path / "payments.csv"


def test_end_to_end_cli_files_to_db_to_zip(tmp_path, capsys):
    demo_invoices, demo_payments = _demo_paths(tmp_path)
    db = tmp_path / "e2e.duckdb"
    out = tmp_path / "exports"
    argv = [
        "reconcile",
        "--invoices",
        str(demo_invoices),
        "--payments",
        str(demo_payments),
        "--as-of",
        "2026-03-31",
        "--db",
        str(db),
        "--export-dir",
        str(out),
        "--json",
    ]
    code = main(argv)
    result = json.loads(capsys.readouterr().out)
    assert code == 2  # demo intentionally contains quarantined rows
    assert result["status"] == "incomplete" and result["new_run"]
    assert result["net_difference_cents"] == -133500
    (zip_path,) = out.glob("*.zip")
    with zipfile.ZipFile(zip_path) as zf:
        summary = json.loads(zf.read("summary.json"))
        exceptions = _zip_csv(zf, "exceptions.csv")
        errors = _zip_csv(zf, "validation_errors.csv")
    assert summary["money_usd"]["P_eligible_payments"]["amount"] == "8920.75"
    assert len(exceptions) == 19
    assert len(errors) == 12

    # Re-running reuses the run and does not duplicate anything.
    before = _counts(db)
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["new_run"] is False
    assert _counts(db) == before


def test_cli_complete_run_exits_zero_despite_business_findings(tmp_path, capsys):
    i = tmp_path / "i.csv"
    p = tmp_path / "p.csv"
    i.write_bytes(csv_bytes(INV_HEADER, [inv(amount="100.00")]))
    p.write_bytes(csv_bytes(PAY_HEADER, [pay(amount="40.00")]))
    code = main(
        [
            "reconcile",
            "--invoices",
            str(i),
            "--payments",
            str(p),
            "--as-of",
            "2026-03-31",
            "--db",
            str(tmp_path / "d.duckdb"),
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["status"] == "complete"
    assert result["exception_count"] == 1  # partially paid invoice


def test_cli_file_level_error_exits_two_and_saves_nothing(tmp_path, capsys):
    _, demo_payments = _demo_paths(tmp_path)
    i = tmp_path / "i.csv"
    i.write_bytes(b"wrong,headers\n1,2\n")
    db = tmp_path / "d.duckdb"
    code = main(
        [
            "reconcile",
            "--invoices",
            str(i),
            "--payments",
            str(demo_payments),
            "--as-of",
            "2026-03-31",
            "--db",
            str(db),
        ]
    )
    assert code == 2
    assert "Missing required columns" in capsys.readouterr().err
    assert not db.exists()


def test_cli_rejects_bad_as_of(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["reconcile", "--invoices", "a", "--payments", "b", "--as-of", "03/31/2026"])
    assert exc.value.code == 2
