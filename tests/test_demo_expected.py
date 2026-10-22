"""The bundled demo against INDEPENDENTLY calculated expectations.

Every number below was worked out by hand from src/reconcilekit/demo_data/invoices.csv
and payments.csv (see docs/demo-expected-results.md). None was produced by
running the engine.
"""

from __future__ import annotations

from reconcilekit.demo import DEMO_AS_OF, load_demo
from reconcilekit.reconciliation import reconcile_files

EXPECTED_INVOICES = {
    # invoice_id: (status, applied, outstanding, overpayment, overdue)
    "INV-1001": ("PAID", 120000, 0, 0, False),
    "INV-1002": ("PAID", 250000, 0, 0, False),
    "INV-1003": ("PARTIALLY_PAID", 50000, 30000, 0, True),
    "INV-1004": ("OVERPAID", 50000, 0, 5000, False),
    "INV-1005": ("UNPAID", 0, 97550, 0, True),
    "000123": ("PAID", 64025, 0, 0, False),
    "INV-1007": ("UNPAID", 0, 30000, 0, False),
    "INV-1008": ("PAID", 72000, 0, 0, False),
}

EXPECTED_PAYMENTS = {
    "PAY-5001": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5002": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5003": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5004": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5005": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5006": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5007": ("APPLIED", "EXACT_REFERENCE_MATCH"),
    "PAY-5008": ("UNMATCHED", "PAYMENT_REFERENCE_MISSING"),
    "PAY-5009": ("UNMATCHED", "PAYMENT_REFERENCE_NOT_FOUND"),
    "PAY-5010": ("UNMATCHED", "PAYMENT_REFERENCE_NOT_FOUND"),
    "PAY-5011": ("UNMATCHED", "CUSTOMER_REFERENCE_CONFLICT"),
    "PAY-5012": ("AMBIGUOUS", "AMBIGUOUS_INVOICE_REFERENCE"),
    "PAY-5013": ("BLOCKED", "REFERENCED_INVOICE_INVALID"),
}

EXPECTED_QUARANTINE = {
    ("invoices", 10): "DUPLICATE_INVOICE_IDENTITY",
    ("invoices", 11): "DUPLICATE_INVOICE_IDENTITY",
    ("invoices", 12): "DUPLICATE_INVOICE_IDENTITY",
    ("invoices", 13): "DUPLICATE_INVOICE_IDENTITY",
    ("invoices", 14): "MISSING_REQUIRED_VALUE",
    ("invoices", 15): "INVALID_AMOUNT",
    ("invoices", 16): "UNSUPPORTED_CURRENCY",
    ("invoices", 17): "TRANSACTION_AFTER_AS_OF",
    ("payments", 15): "DUPLICATE_PAYMENT_ID",
    ("payments", 16): "DUPLICATE_PAYMENT_ID",
    ("payments", 17): "TRANSACTION_AFTER_AS_OF",
    ("payments", 18): "INVALID_AMOUNT",
}


def _demo_run():
    inv, pay = load_demo()
    return reconcile_files(inv, pay, DEMO_AS_OF)


def test_demo_invoice_results():
    run = _demo_run()
    actual = {
        r.invoice_id: (
            r.status.value,
            r.applied_payment_cents,
            r.outstanding_cents,
            r.overpayment_cents,
            r.is_overdue,
        )
        for r in run.invoice_results
    }
    assert actual == EXPECTED_INVOICES


def test_demo_payment_results():
    run = _demo_run()
    actual = {r.payment_id: (r.status.value, r.rule_code.value) for r in run.payment_results}
    assert actual == EXPECTED_PAYMENTS


def test_demo_quarantine():
    run = _demo_run()
    actual = {(i.kind, i.source_row): i.rule_code.value for i in run.validation_issues}
    assert actual == EXPECTED_QUARANTINE


def test_demo_totals():
    s = _demo_run().summary
    assert s.eligible_invoice_cents == 758575  # I
    assert s.applied_payment_cents == 606025  # A
    assert s.outstanding_cents == 157550  # O
    assert s.overpayment_cents == 5000  # X
    assert s.eligible_payment_cents == 892075  # P
    assert s.unmatched_payment_cents == 166050  # U
    assert s.ambiguous_payment_cents == 100000  # M
    assert s.blocked_payment_cents == 20000  # B
    assert s.net_difference_cents == -133500
    assert s.overdue_cents == 127550
    assert (s.invoice_rows, s.eligible_invoices, s.quarantined_invoice_rows) == (16, 8, 8)
    assert (s.payment_rows, s.eligible_payments, s.quarantined_payment_rows) == (17, 13, 4)
    assert not s.is_complete
    assert s.exception_count == 19


def test_demo_specific_explanations():
    run = _demo_run()
    by_id = {r.payment_id: r for r in run.payment_results}
    assert by_id["PAY-5007"].normalization_applied
    assert "differs only by letter case" in by_id["PAY-5009"].explanation
    assert [w.value for w in by_id["PAY-5005"].warnings] == ["PAYMENT_BEFORE_INVOICE_DATE"]
