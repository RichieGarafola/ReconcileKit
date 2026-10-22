"""Regression tests added during the pre-publication audit."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

import pandas as pd
import pytest

from conftest import inv, invoice_by_id, pay, run_rows
from reconcilekit import reporting
from reconcilekit.demo import load_demo
from reconcilekit.exports import _to_csv, build_export_package, escape_formula
from reconcilekit.models import ExceptionCategory, InvoiceStatus
from reconcilekit.money import MAX_AMOUNT_CENTS
from reconcilekit.repository import add_review_event, connect, save_run


def test_packaged_demo_resources_load():
    invoices, payments = load_demo()
    assert invoices.startswith(b"invoice_id,customer_id,")
    assert payments.startswith(b"payment_id,invoice_id,")


def test_latest_review_is_deterministic_when_timestamps_tie(db_path):
    run = run_rows([inv()], [pay(invoice_id="")])
    run_id = save_run(db_path, run).run_id
    exc_id = run.exceptions[0].exception_id
    same_time = datetime(2026, 4, 1, 12, 0, 0)
    for disposition in ("INVESTIGATING", "ACKNOWLEDGED", "INVESTIGATING"):
        add_review_event(db_path, run_id, exc_id, "r", disposition, created_at_utc=same_time)
    with connect(db_path) as con:
        current = reporting.exceptions_with_disposition(con, run_id)
        history = reporting.review_history(con, run_id, exc_id)
    assert current.loc[0, "disposition"] == "INVESTIGATING"  # last inserted, not arbitrary
    assert list(history["disposition"]) == ["INVESTIGATING", "ACKNOWLEDGED", "INVESTIGATING"]


def test_identical_later_review_with_new_event_id_is_recorded(db_path):
    run = run_rows([inv()], [pay(invoice_id="")])
    run_id = save_run(db_path, run).run_id
    exc_id = run.exceptions[0].exception_id
    a = add_review_event(db_path, run_id, exc_id, "r", "ACKNOWLEDGED", "same")
    b = add_review_event(db_path, run_id, exc_id, "r", "ACKNOWLEDGED", "same")
    assert a.inserted and b.inserted and a.event_id != b.event_id
    with connect(db_path) as con:
        assert len(reporting.review_history(con, run_id, exc_id)) == 2


def test_maximum_amounts_stay_exact_through_db_and_export(db_path):
    top = "999999999999.99"
    invoices = [inv(f"INV-{n}", amount=top) for n in range(5)]
    payments = [pay(f"P-{n}", f"INV-{n}", amount=top) for n in range(4)]
    payments.append(pay("P-X", "", amount="0.01"))
    run = run_rows(invoices, payments)
    assert run.summary.eligible_invoice_cents == 5 * MAX_AMOUNT_CENTS
    run_id = save_run(db_path, run).run_id
    with zipfile.ZipFile(io.BytesIO(build_export_package(db_path, run_id))) as zf:
        money = json.loads(zf.read("summary.json"))["money_usd"]
        invoice_csv = zf.read("invoice_results.csv").decode()
    assert money["I_eligible_invoices"]["amount"] == "4999999999999.95"
    assert money["A_applied_payments"]["amount"] == "3999999999999.96"
    assert money["O_outstanding"]["amount"] == "999999999999.99"
    assert money["P_eligible_payments"]["amount"] == "3999999999999.97"
    assert ",999999999999.99,999999999999.99,0.00,0.00," in invoice_csv


def test_amount_above_limit_is_quarantined_not_truncated():
    run = run_rows([inv(amount="1000000000000.00"), inv("INV-2")], [])
    assert run.summary.eligible_invoice_cents == 10000
    assert "maximum" in run.validation_issues[0].message


def test_offsetting_balances_do_not_look_settled():
    run = run_rows(
        [inv("A", amount="100.00"), inv("B", amount="100.00")],
        [pay("P1", "A", amount="110.00"), pay("P2", "B", amount="90.00")],
    )
    s = run.summary
    assert s.net_difference_cents == 0
    assert (s.outstanding_cents, s.overpayment_cents) == (1000, 1000)
    assert invoice_by_id(run, "A").status is InvoiceStatus.OVERPAID
    assert invoice_by_id(run, "B").status is InvoiceStatus.PARTIALLY_PAID
    assert len(run.exceptions) == 2


def test_quarantined_row_amount_is_not_presented_as_usd():
    run = run_rows([inv(currency="EUR", amount="410.00")], [])
    (exc,) = [e for e in run.exceptions if e.category is ExceptionCategory.VALIDATION]
    assert exc.amount_cents is None


def test_row_with_many_errors_yields_one_row_exception_and_no_money():
    run = run_rows([inv(currency="EUR", amount="1e3", due_date="bad")], [])
    assert len(run.exceptions) == 1
    assert len(run.validation_issues) == 3
    assert run.summary.eligible_invoice_cents == 0


@pytest.mark.parametrize(
    "value",
    [
        "\n=1+1",
        "\x0b=1+1",
        "\x00x",
        "​=1+1",
        " \t=1+1",
        "　=1+1",
        chr(0xFF1D) + "1+1",
        chr(0xFF20) + "SUM(A1)",
    ],
)
def test_escape_formula_control_and_unicode_cases(value):
    assert escape_formula(value) == "'" + value


def test_export_refuses_float_money_column():
    with pytest.raises(TypeError, match="non-integer"):
        _to_csv(pd.DataFrame({"x_cents": [1.5]}), {"x_cents": "x"})
