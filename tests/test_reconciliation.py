from __future__ import annotations

from datetime import date

import pytest

from conftest import inv, invoice_by_id, pay, payment_by_id, run_rows
from reconcilekit.models import InvoiceStatus, PaymentStatus, RuleCode
from reconcilekit.reconciliation import InvariantViolation, check_invariants


def test_one_to_one_exact_match():
    run = run_rows([inv(amount="250.00")], [pay(amount="250.00")])
    p = payment_by_id(run, "P-1")
    assert (p.status, p.rule_code) == (PaymentStatus.APPLIED, RuleCode.EXACT_REFERENCE_MATCH)
    i = invoice_by_id(run, "INV-1")
    assert i.status is InvoiceStatus.PAID
    assert (i.applied_payment_cents, i.outstanding_cents, i.overpayment_cents) == (25000, 0, 0)


def test_multiple_payments_sum_to_one_invoice():
    run = run_rows(
        [inv(amount="100.00")],
        [pay("P-1", amount="33.33"), pay("P-2", amount="33.33"), pay("P-3", amount="33.34")],
    )
    i = invoice_by_id(run, "INV-1")
    assert i.status is InvoiceStatus.PAID
    assert i.applied_payment_cents == 10000
    assert len(i.payment_record_ids) == 3


def test_partial_payment():
    i = invoice_by_id(run_rows([inv(amount="100.00")], [pay(amount="40.01")]), "INV-1")
    assert i.status is InvoiceStatus.PARTIALLY_PAID
    assert (i.outstanding_cents, i.overpayment_cents) == (5999, 0)


def test_overpayment():
    i = invoice_by_id(run_rows([inv(amount="100.00")], [pay(amount="100.01")]), "INV-1")
    assert i.status is InvoiceStatus.OVERPAID
    assert (i.outstanding_cents, i.overpayment_cents) == (0, 1)


def test_unpaid_invoice():
    i = invoice_by_id(run_rows([inv(amount="100.00")], []), "INV-1")
    assert i.status is InvoiceStatus.UNPAID
    assert i.outstanding_cents == 10000


def test_zero_tolerance_one_cent_short_is_partial():
    i = invoice_by_id(run_rows([inv(amount="100.00")], [pay(amount="99.99")]), "INV-1")
    assert i.status is InvoiceStatus.PARTIALLY_PAID


def test_missing_reference_is_unmatched_even_with_equal_amount():
    run = run_rows([inv(amount="100.00")], [pay(invoice_id="", amount="100.00")])
    p = payment_by_id(run, "P-1")
    assert (p.status, p.rule_code) == (PaymentStatus.UNMATCHED, RuleCode.PAYMENT_REFERENCE_MISSING)
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.UNPAID


def test_nonexistent_reference_is_unmatched():
    p = payment_by_id(run_rows([inv()], [pay(invoice_id="INV-404")]), "P-1")
    assert (p.status, p.rule_code) == (
        PaymentStatus.UNMATCHED,
        RuleCode.PAYMENT_REFERENCE_NOT_FOUND,
    )


def test_customer_mismatch_does_not_match():
    run = run_rows([inv(customer_id="C1")], [pay(customer_id="C2")])
    p = payment_by_id(run, "P-1")
    assert p.status is PaymentStatus.UNMATCHED
    assert p.rule_code is RuleCode.CUSTOMER_REFERENCE_CONFLICT
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.UNPAID
    assert any(e.rule_code is RuleCode.CUSTOMER_REFERENCE_CONFLICT for e in run.exceptions)


def test_whitespace_normalization_matches_and_is_disclosed():
    run = run_rows([inv("INV-1", "C1")], [pay(" P-1 ", "  INV-1 ", "C1\t")])
    p = payment_by_id(run, "P-1")
    assert p.status is PaymentStatus.APPLIED
    assert p.normalization_applied
    assert p.invoice_id_raw == "  INV-1 "
    assert "normalization" in p.explanation


@pytest.mark.parametrize("reference", ["inv-1", "INV1", "INV_1", "INV-1.", "INV -1", "0INV-1"])
def test_case_punctuation_and_zeros_stay_distinct(reference):
    run = run_rows([inv("INV-1")], [pay(invoice_id=reference)])
    p = payment_by_id(run, "P-1")
    assert p.status is PaymentStatus.UNMATCHED
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.UNPAID


def test_case_difference_explained_as_near_miss():
    p = payment_by_id(run_rows([inv("INV-1")], [pay(invoice_id="inv-1")]), "P-1")
    assert "differs only by letter case or punctuation" in p.explanation


def test_leading_zero_ids_do_not_match_without_zeros():
    run = run_rows([inv("000123", "00042")], [pay(invoice_id="123", customer_id="42")])
    assert payment_by_id(run, "P-1").status is PaymentStatus.UNMATCHED
    run = run_rows([inv("000123", "00042")], [pay(invoice_id="000123", customer_id="00042")])
    assert payment_by_id(run, "P-1").status is PaymentStatus.APPLIED


@pytest.mark.parametrize("identical", [True, False])
def test_duplicate_invoice_identity_quarantines_group_and_payment_is_ambiguous(identical):
    second = inv(amount="100.00" if identical else "999.00")
    run = run_rows([inv(), second], [pay()])
    assert run.summary.eligible_invoices == 0
    assert run.summary.quarantined_invoice_rows == 2
    assert run.summary.eligible_invoice_cents == 0
    p = payment_by_id(run, "P-1")
    assert (p.status, p.rule_code) == (
        PaymentStatus.AMBIGUOUS,
        RuleCode.AMBIGUOUS_INVOICE_REFERENCE,
    )
    (group_exc,) = [e for e in run.exceptions if e.rule_code is RuleCode.DUPLICATE_INVOICE_IDENTITY]
    assert len(group_exc.record_ids) == 2
    assert ("identical" in group_exc.explanation) is identical
    assert ("conflict on: invoice_amount" in group_exc.explanation) is not identical


def test_duplicate_detected_after_whitespace_normalization():
    run = run_rows([inv("INV-1"), inv(" INV-1 ")], [])
    assert run.summary.quarantined_invoice_rows == 2


def test_same_invoice_id_different_customers_is_not_duplicate():
    run = run_rows([inv("INV-1", "C1"), inv("INV-1", "C2")], [pay(customer_id="C2")])
    assert run.summary.eligible_invoices == 2
    assert payment_by_id(run, "P-1").status is PaymentStatus.APPLIED


@pytest.mark.parametrize("identical", [True, False])
def test_duplicate_payment_id_quarantines_all_rows(identical):
    run = run_rows(
        [inv(amount="300.00")],
        [pay("P-1", amount="100.00"), pay("P-1", amount="100.00" if identical else "150.00")],
    )
    assert run.summary.eligible_payments == 0
    assert run.summary.quarantined_payment_rows == 2
    assert run.summary.eligible_payment_cents == 0
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.UNPAID
    (e,) = [e for e in run.exceptions if e.rule_code is RuleCode.DUPLICATE_PAYMENT_ID]
    assert ("identical" in e.explanation) is identical


def test_duplicate_with_otherwise_invalid_sibling_still_blocks_identity():
    # The sibling has a bad amount. It must not be filtered out first, which would
    # make the remaining valid row look unique and confidently matched.
    run = run_rows([inv(amount="100.00"), inv(amount="abc")], [pay(amount="100.00")])
    assert run.summary.eligible_invoices == 0
    assert payment_by_id(run, "P-1").status is PaymentStatus.AMBIGUOUS
    codes = {e.rule_code for e in run.exceptions}
    assert {RuleCode.DUPLICATE_INVOICE_IDENTITY, RuleCode.INVALID_AMOUNT} <= codes


def test_duplicate_payment_with_invalid_sibling():
    run = run_rows([inv()], [pay("P-1"), pay("P-1", payment_date="2026-13-01")])
    assert run.summary.eligible_payments == 0
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.UNPAID


def test_reference_to_invalid_invoice_is_blocked():
    run = run_rows([inv(currency="EUR")], [pay()])
    p = payment_by_id(run, "P-1")
    assert (p.status, p.rule_code) == (PaymentStatus.BLOCKED, RuleCode.REFERENCED_INVOICE_INVALID)
    assert "UNSUPPORTED_CURRENCY" in p.explanation
    assert run.summary.blocked_payment_cents == 10000


def test_currency_case_and_whitespace_normalized():
    run = run_rows([inv(currency=" usd ")], [pay(currency="Usd")])
    assert payment_by_id(run, "P-1").status is PaymentStatus.APPLIED


@pytest.mark.parametrize(
    ("field_kw", "value", "code"),
    [
        ("amount", "", RuleCode.MISSING_REQUIRED_VALUE),
        ("amount", "12.345", RuleCode.INVALID_AMOUNT),
        ("amount", "0.00", RuleCode.INVALID_AMOUNT),
        ("amount", "-1.00", RuleCode.INVALID_AMOUNT),
        ("invoice_date", "01/02/2026", RuleCode.INVALID_DATE),
        ("invoice_date", "2026-02-30", RuleCode.INVALID_DATE),
        ("due_date", "2025-12-31", RuleCode.DUE_DATE_BEFORE_INVOICE_DATE),
        ("currency", "CAD", RuleCode.UNSUPPORTED_CURRENCY),
        ("customer_id", "  ", RuleCode.MISSING_REQUIRED_VALUE),
    ],
)
def test_invalid_invoice_rows_quarantined(field_kw, value, code):
    run = run_rows([inv(**{field_kw: value}), inv("INV-2")], [])
    assert run.summary.quarantined_invoice_rows == 1
    assert run.summary.eligible_invoices == 1
    assert run.summary.eligible_invoice_cents == 10000
    assert not run.summary.is_complete
    assert code in {i.rule_code for i in run.validation_issues}


def test_missing_amount_is_never_zero():
    run = run_rows([inv(amount="")], [])
    assert run.summary.eligible_invoices == 0
    assert run.invoice_results == ()


def test_transactions_after_as_of_are_quarantined_future_due_dates_allowed():
    as_of = date(2026, 3, 31)
    run = run_rows(
        [
            inv("INV-1", invoice_date="2026-04-01", due_date="2026-04-30"),
            inv("INV-2", invoice_date="2026-03-31", due_date="2026-12-31"),
        ],
        [
            pay("P-1", "INV-2", payment_date="2026-04-01"),
            pay("P-2", "INV-2", payment_date="2026-03-31"),
        ],
        as_of,
    )
    after = {
        (i.kind, i.source_row)
        for i in run.validation_issues
        if i.rule_code is RuleCode.TRANSACTION_AFTER_AS_OF
    }
    assert after == {("invoices", 2), ("payments", 2)}
    assert invoice_by_id(run, "INV-2").status is InvoiceStatus.PAID


@pytest.mark.parametrize(
    ("due", "overdue", "days"),
    [("2026-03-30", True, 1), ("2026-03-31", False, 0), ("2026-04-01", False, 0)],
)
def test_overdue_boundaries(due, overdue, days):
    run = run_rows([inv(invoice_date="2026-03-01", due_date=due)], [])
    i = invoice_by_id(run, "INV-1")
    assert (i.is_overdue, i.days_overdue) == (overdue, days)


def test_paid_invoice_past_due_is_not_overdue():
    i = invoice_by_id(run_rows([inv(due_date="2026-01-31")], [pay()]), "INV-1")
    assert not i.is_overdue


def test_payment_before_invoice_date_is_informational_only():
    run = run_rows(
        [inv(invoice_date="2026-02-01", due_date="2026-03-01")], [pay(payment_date="2026-01-15")]
    )
    p = payment_by_id(run, "P-1")
    assert p.status is PaymentStatus.APPLIED
    assert RuleCode.PAYMENT_BEFORE_INVOICE_DATE in p.warnings
    assert invoice_by_id(run, "INV-1").status is InvoiceStatus.PAID


def test_as_of_is_explicit_not_machine_clock():
    a = run_rows([inv(due_date="2026-02-01")], [], date(2026, 1, 15))
    b = run_rows([inv(due_date="2026-02-01")], [], date(2026, 3, 15))
    assert not invoice_by_id(a, "INV-1").is_overdue
    assert invoice_by_id(b, "INV-1").is_overdue
    assert a.run_id != b.run_id


def test_deterministic_results_and_ids():
    rows = ([inv(), inv("INV-2", amount="abc")], [pay(), pay("P-2", invoice_id="")])
    a, b = run_rows(*rows), run_rows(*rows)
    assert a == b


def test_invariants_hold_on_mixed_run():
    run = run_rows(
        [
            inv("I1", amount="100.00"),
            inv("I2", amount="50.00"),
            inv("I3", amount="70.00"),
            inv("I3", amount="70.00"),
            inv("I4", currency="EUR"),
        ],
        [
            pay("P1", "I1", amount="60.00"),
            pay("P2", "I2", amount="80.00"),
            pay("P3", "", amount="5.00"),
            pay("P4", "I3", amount="7.00"),
            pay("P5", "I4", amount="9.00"),
        ],
    )
    s = run.summary
    i, a, o, x = (
        s.eligible_invoice_cents,
        s.applied_payment_cents,
        s.outstanding_cents,
        s.overpayment_cents,
    )
    p, u, m, b = (
        s.eligible_payment_cents,
        s.unmatched_payment_cents,
        s.ambiguous_payment_cents,
        s.blocked_payment_cents,
    )
    assert i - a == o - x
    assert p == a + u + m + b
    assert (u, m, b) == (500, 700, 900)
    assert o > 0 and x > 0  # offsetting balances do not imply settlement


def test_invariant_violation_is_raised():
    run = run_rows([inv()], [pay()])
    broken = run.invoice_results[0].__class__(
        **{**run.invoice_results[0].__dict__, "outstanding_cents": 1}
    )
    with pytest.raises(InvariantViolation):
        check_invariants((broken,), run.payment_results)
