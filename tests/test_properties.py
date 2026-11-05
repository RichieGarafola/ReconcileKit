"""Property-based tests (Hypothesis) for money handling and reconciliation.

The engine already asserts its own bridge invariants via ``check_invariants``. These
tests deliberately do NOT rely on that function: every expected total is recomputed
from the generated input rows with ``Decimal`` and a deliberately simple model of the
documented matching contract (exact ``customer_id + invoice_id`` after trimming only;
duplicate identities quarantined, including when a sibling row is invalid; a reference to a
lone quarantined invoice is BLOCKED). A disagreement between the model and the engine fails
here even if the engine's internal checks are self-consistent.

Example counts come from a Hypothesis profile: ``ci`` (200 examples, used when the ``CI``
environment variable is set, as on GitHub Actions) or ``dev`` (50 examples, the local
default). Override with ``HYPOTHESIS_PROFILE=ci pytest``.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import AS_OF, inv, pay, run_rows
from reconcilekit.models import InvoiceStatus, PaymentStatus
from reconcilekit.money import (
    MAX_AMOUNT_CENTS,
    AmountError,
    format_cents,
    format_usd,
    parse_amount_cents,
)

_COMMON = {
    "deadline": None,  # pandas/pandera start-up makes per-example timing noisy on CI
    "suppress_health_check": [HealthCheck.too_slow],
}
settings.register_profile("ci", max_examples=200, **_COMMON)
settings.register_profile("dev", max_examples=50, **_COMMON)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)

PROFILE = settings()  # inherits the loaded profile; kept as a single place to tune all tests

# --------------------------------------------------------------------------- money

cents = st.integers(min_value=1, max_value=MAX_AMOUNT_CENTS)


@PROFILE
@given(cents)
def test_format_then_parse_round_trips_exactly(c):
    assert parse_amount_cents(format_cents(c)) == c


@PROFILE
@given(
    st.integers(min_value=0, max_value=999_999_999_999),
    st.sampled_from(["", ".0", ".00", ".5", ".05", ".50", ".99", ".1", ".10"]),
    st.sampled_from(["", " ", "\t"]),
)
def test_parse_agrees_with_decimal_oracle(whole, frac, pad):
    text = f"{pad}{whole}{frac}{pad}"
    expected = int(Decimal(f"{whole}{frac or ''}") * 100)
    if expected <= 0:
        with pytest.raises(AmountError):
            parse_amount_cents(text)
    else:
        assert parse_amount_cents(text) == expected


@PROFILE
@given(
    st.integers(min_value=0, max_value=10**9),
    st.text(alphabet="0123456789", min_size=3, max_size=6),
)
def test_more_than_two_decimals_is_rejected_never_rounded(whole, frac):
    with pytest.raises(AmountError):
        parse_amount_cents(f"{whole}.{frac}")


@PROFILE
@given(cents)
def test_presentation_forms_are_rejected(c):
    plain = format_cents(c)
    for bad in (f"${plain}", f"-{plain}", f"+{plain}", f"{plain} USD", f"({plain})"):
        with pytest.raises(AmountError):
            parse_amount_cents(bad)
    if c >= 100_000:  # has a thousands group to separate
        with pytest.raises(AmountError):
            parse_amount_cents(format_usd(c).lstrip("$"))


@PROFILE
@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_format_usd_matches_decimal_formatting(c):
    expected = f"{Decimal(abs(c)) / 100:,.2f}"
    assert format_usd(c) == ("-" if c < 0 else "") + "$" + expected
    assert Decimal(format_cents(c)) * 100 == c


def test_amount_above_maximum_is_rejected():
    with pytest.raises(AmountError):
        parse_amount_cents(format_cents(MAX_AMOUNT_CENTS + 1))


# --------------------------------------------------------------------------- ledgers

CUSTOMERS = ["C1", "C2", "C3"]
INVOICE_IDS = ["INV-1", "INV-2", "INV-3", "INV-4"]
small_amount = st.integers(min_value=1, max_value=500_000).map(format_cents)
INVALID_AMOUNTS = ["", "1.234", "-5.00", "0.00"]


@st.composite
def ledgers(draw):
    """Invoice and payment rows with deliberate collisions.

    Small identifier pools force duplicate invoice identities, cross-customer
    references, unknown references, blank references, and duplicate payment ids. Some
    invoice amounts are invalid (so the row is quarantined), and some payment references
    carry surrounding whitespace (trimmed before matching). Payment rows are always valid.
    """
    invoices = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        inv_date = AS_OF - timedelta(days=draw(st.integers(min_value=0, max_value=120)))
        due = inv_date + timedelta(days=draw(st.integers(min_value=0, max_value=60)))
        invoices.append(
            inv(
                invoice_id=draw(st.sampled_from(INVOICE_IDS)),
                customer_id=draw(st.sampled_from(CUSTOMERS)),
                invoice_date=inv_date.isoformat(),
                due_date=due.isoformat(),
                amount=draw(
                    st.one_of(small_amount, small_amount, st.sampled_from(INVALID_AMOUNTS))
                ),
            )
        )
    payments = []
    for _ in range(draw(st.integers(min_value=0, max_value=10))):
        pay_date = AS_OF - timedelta(days=draw(st.integers(min_value=0, max_value=120)))
        payments.append(
            pay(
                payment_id=draw(st.sampled_from([f"P-{n}" for n in range(1, 9)])),
                invoice_id=draw(st.sampled_from(["", " "]))
                + draw(st.sampled_from([*INVOICE_IDS, "INV-404", "inv-1", ""]))
                + draw(st.sampled_from(["", " "])),
                customer_id=draw(st.sampled_from(CUSTOMERS)),
                payment_date=pay_date.isoformat(),
                amount=draw(small_amount),
            )
        )
    return invoices, payments


def to_cents(text: str) -> int:
    return int(Decimal(text) * 100)


def valid_amount(text: str) -> bool:
    return text not in INVALID_AMOUNTS


def model(invoices, payments):
    """Independent expectation for generated rows (see module docstring)."""
    # Duplicate identities are counted over ALL rows, valid or not (documented rule).
    inv_identity = Counter((r[1], r[0]) for r in invoices)  # (customer_id, invoice_id)
    invalid_keys = {(r[1], r[0]) for r in invoices if not valid_amount(r[5])}
    eligible_inv = {
        (r[1], r[0]): to_cents(r[5])
        for r in invoices
        if inv_identity[(r[1], r[0])] == 1 and valid_amount(r[5])
    }
    pay_identity = Counter(r[0] for r in payments)
    eligible_pay = [r for r in payments if pay_identity[r[0]] == 1]

    status = {}
    applied = Counter()
    for r in eligible_pay:
        pid, inv_id, cust, amount = r[0], r[1].strip(), r[2], to_cents(r[5])
        key = (cust, inv_id)
        if inv_id == "":
            status[pid] = PaymentStatus.UNMATCHED
        elif inv_identity[key] > 1:
            status[pid] = PaymentStatus.AMBIGUOUS
        elif key in invalid_keys:
            status[pid] = PaymentStatus.BLOCKED
        elif key in eligible_inv:
            status[pid] = PaymentStatus.APPLIED
            applied[key] += amount
        else:
            status[pid] = PaymentStatus.UNMATCHED
    totals = {
        "I": sum(eligible_inv.values()),
        "P": sum(to_cents(r[5]) for r in eligible_pay),
        "A": sum(applied.values()),
    }
    by_status = Counter()
    for r in eligible_pay:
        by_status[status[r[0]]] += to_cents(r[5])
    return eligible_inv, status, applied, totals, by_status


def invoice_key(result):
    return (result.customer_id, result.invoice_id)


@PROFILE
@given(ledgers())
def test_engine_agrees_with_independent_model(data):
    invoices, payments = data
    run = run_rows(invoices, payments)  # raises InvariantViolation if the bridge fails
    eligible_inv, status, applied, totals, by_status = model(invoices, payments)
    s = run.summary

    assert s.eligible_invoice_cents == totals["I"]
    assert s.eligible_payment_cents == totals["P"]
    assert s.applied_payment_cents == totals["A"]
    assert s.unmatched_payment_cents == by_status[PaymentStatus.UNMATCHED]
    assert s.ambiguous_payment_cents == by_status[PaymentStatus.AMBIGUOUS]
    assert s.blocked_payment_cents == by_status[PaymentStatus.BLOCKED]
    assert s.quarantined_invoice_rows == len(invoices) - len(eligible_inv)

    assert {p.payment_id: p.status for p in run.payment_results} == status
    assert {invoice_key(i) for i in run.invoice_results} == set(eligible_inv)
    for i in run.invoice_results:
        a = applied[invoice_key(i)]
        assert i.applied_payment_cents == a
        assert i.outstanding_cents == max(i.invoice_amount_cents - a, 0)
        assert i.overpayment_cents == max(a - i.invoice_amount_cents, 0)
        expected = (
            InvoiceStatus.UNPAID
            if a == 0
            else InvoiceStatus.PARTIALLY_PAID
            if a < i.invoice_amount_cents
            else InvoiceStatus.PAID
            if a == i.invoice_amount_cents
            else InvoiceStatus.OVERPAID
        )
        assert i.status is expected
        assert i.is_overdue == (i.outstanding_cents > 0 and i.due_date < AS_OF)


@PROFILE
@given(ledgers())
def test_applied_payments_match_only_on_exact_identity(data):
    run = run_rows(*data)
    invoices_by_record = {i.record_id: i for i in run.invoice_results}
    for p in run.payment_results:
        if p.status is PaymentStatus.APPLIED:
            target = invoices_by_record[p.matched_invoice_record_id]
            assert (p.customer_id, p.invoice_id) == (target.customer_id, target.invoice_id)
        else:
            assert p.matched_invoice_record_id is None
    # A case variant of an existing reference ("inv-1") is never applied.
    assert all(
        p.status is not PaymentStatus.APPLIED
        for p in run.payment_results
        if p.invoice_id == "inv-1"
    )


@PROFILE
@given(ledgers(), st.randoms(use_true_random=False))
def test_row_order_does_not_change_outcomes(data, rnd: random.Random):
    invoices, payments = data
    base = run_rows(invoices, payments)
    shuffled_inv, shuffled_pay = invoices[:], payments[:]
    rnd.shuffle(shuffled_inv)
    rnd.shuffle(shuffled_pay)
    other = run_rows(shuffled_inv, shuffled_pay)

    money = [
        "eligible_invoice_cents",
        "applied_payment_cents",
        "outstanding_cents",
        "overpayment_cents",
        "eligible_payment_cents",
        "unmatched_payment_cents",
        "ambiguous_payment_cents",
        "blocked_payment_cents",
        "overdue_cents",
    ]
    assert {k: asdict(base.summary)[k] for k in money} == {
        k: asdict(other.summary)[k] for k in money
    }

    def outcomes(run):
        return (
            {p.payment_id: (p.status, p.rule_code) for p in run.payment_results},
            {invoice_key(i): (i.status, i.applied_payment_cents) for i in run.invoice_results},
        )

    assert outcomes(base) == outcomes(other)


@PROFILE
@given(ledgers())
def test_same_input_is_deterministic(data):
    first, second = run_rows(*data), run_rows(*data)
    assert first.run_id == second.run_id
    assert first.fingerprint == second.fingerprint
    assert first.summary == second.summary
    assert first.payment_results == second.payment_results
    assert first.invoice_results == second.invoice_results
    assert [e.exception_id for e in first.exceptions] == [e.exception_id for e in second.exceptions]


@PROFILE
@given(ledgers(), st.data())
def test_invalid_amount_rows_are_quarantined_not_coerced(data, draw):
    invoices, payments = data
    bad = draw.draw(
        st.sampled_from(["", "abc", "1,000.00", "$5.00", "-1.00", "0.00", "1.234", "1e3"])
    )
    run = run_rows([*invoices, inv(invoice_id="INV-BAD", customer_id="C9", amount=bad)], payments)
    clean = run_rows(invoices, payments)
    assert all(i.invoice_id != "INV-BAD" for i in run.invoice_results)
    assert run.summary.quarantined_invoice_rows == clean.summary.quarantined_invoice_rows + 1
    assert run.summary.eligible_invoice_cents == clean.summary.eligible_invoice_cents
    bad_row = len(invoices) + 2  # header is row 1
    assert any(
        i.source_row == bad_row and i.field == "invoice_amount"
        for i in run.validation_issues
        if i.kind == "invoices"
    )
