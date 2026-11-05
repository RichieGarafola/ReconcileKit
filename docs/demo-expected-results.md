# Demo: expected results

The bundled demo (`src/reconcilekit/demo_data/invoices.csv` and `payments.csv`) is small enough to check
by hand. Every expected value below was calculated manually from the CSV files. None was
copied from engine output. `tests/test_demo_expected.py` hard-codes these values as the test
oracle.

**As-of date:** 2026-03-31. Row numbers count the header as row 1, as a spreadsheet does.

> All identifiers, customers, and amounts are fictional and were made up for this demo.

## Invoices (`demo_data/invoices.csv`)

| Row | Invoice | Customer | Due | Amount | Planted scenario | Expected outcome |
|---|---|---|---|---:|---|---|
| 2 | INV-1001 | CUST-A | 2026-02-04 | 1200.00 | Exact full payment | PAID (applied 1200.00) |
| 3 | INV-1002 | CUST-A | 2026-02-09 | 2500.00 | Two payments add up to the invoice | PAID (1000.00 + 1500.00) |
| 4 | INV-1003 | CUST-B | 2026-02-14 | 800.00 | Partial payment, past due | PARTIALLY_PAID, outstanding 300.00, overdue 45 days |
| 5 | INV-1004 | CUST-B | 2026-03-03 | 450.00 | Overpayment | OVERPAID by 50.00 |
| 6 | INV-1005 | CUST-C | 2026-03-12 | 975.50 | Unpaid and overdue (its only payment uses `inv-1005`) | UNPAID, outstanding 975.50, overdue 19 days |
| 7 | 000123 | 00042 | 2026-03-17 | 640.25 | Leading-zero identifiers | PAID |
| 8 | INV-1007 | CUST-D | 2026-04-30 | 300.00 | Unpaid, due date after as-of (allowed) | UNPAID, outstanding 300.00, **not** overdue |
| 9 | INV-1008 | CUST-D | 2026-04-04 | 720.00 | Currency written `usd` (normalized); payment reference padded with whitespace | PAID |
| 10, 11 | INV-1009 | CUST-E | | 1000.00 ×2 | Identical duplicate invoice identity | Both rows quarantined (`DUPLICATE_INVOICE_IDENTITY`, identical) |
| 12, 13 | INV-1010 | CUST-E | | 350.00 / 3500.00 | Conflicting duplicate invoice identity | Both rows quarantined (conflict on `invoice_amount`) |
| 14 | INV-1011 | CUST-F | | *(blank)* | Missing required value | Quarantined (`MISSING_REQUIRED_VALUE`) |
| 15 | INV-1012 | CUST-F | | 99.999 | Three decimal places | Quarantined (`INVALID_AMOUNT`; not rounded) |
| 16 | INV-1013 | CUST-G | | 410.00 EUR | Unsupported currency | Quarantined (`UNSUPPORTED_CURRENCY`) |
| 17 | INV-1014 | CUST-G | | 150.00 | Invoice dated 2026-04-02, after the as-of date | Quarantined (`TRANSACTION_AFTER_AS_OF`) |

## Payments (`demo_data/payments.csv`)

| Row | Payment | References | Customer | Amount | Planted scenario | Expected outcome |
|---|---|---|---|---:|---|---|
| 2 | PAY-5001 | INV-1001 | CUST-A | 1200.00 | Exact match | APPLIED |
| 3 | PAY-5002 | INV-1002 | CUST-A | 1000.00 | First of two payments | APPLIED |
| 4 | PAY-5003 | INV-1002 | CUST-A | 1500.00 | Second of two payments | APPLIED |
| 5 | PAY-5004 | INV-1003 | CUST-B | 500.00 | Partial payment | APPLIED |
| 6 | PAY-5005 | INV-1004 | CUST-B | 500.00 | Overpays; dated 2026-01-28, **before** the invoice date | APPLIED + informational `PAYMENT_BEFORE_INVOICE_DATE` |
| 7 | PAY-5006 | 000123 | 00042 | 640.25 | Leading zeros preserved | APPLIED |
| 8 | PAY-5007 | `" INV-1008 "` | `"CUST-D "` | 720.00 | Whitespace-only difference | APPLIED, normalization disclosed |
| 9 | PAY-5008 | *(blank)* | CUST-C | 125.00 | No invoice reference | UNMATCHED (`PAYMENT_REFERENCE_MISSING`) |
| 10 | PAY-5009 | inv-1005 | CUST-C | 975.50 | Differs from INV-1005 only by case; same amount | UNMATCHED (`PAYMENT_REFERENCE_NOT_FOUND`) with near-miss note |
| 11 | PAY-5010 | INV-9999 | CUST-A | 310.00 | Nonexistent invoice | UNMATCHED (`PAYMENT_REFERENCE_NOT_FOUND`) |
| 12 | PAY-5011 | INV-1001 | CUST-B | 250.00 | INV-1001 belongs to CUST-A | UNMATCHED (`CUSTOMER_REFERENCE_CONFLICT`) |
| 13 | PAY-5012 | INV-1009 | CUST-E | 1000.00 | References a duplicated invoice | AMBIGUOUS |
| 14 | PAY-5013 | INV-1011 | CUST-F | 200.00 | References an invalid invoice (row 14) | BLOCKED |
| 15, 16 | PAY-5014 | INV-1007 | CUST-D | 100.00 / 150.00 | Conflicting duplicate payment ID | Both quarantined (`DUPLICATE_PAYMENT_ID`); INV-1007 stays UNPAID |
| 17 | PAY-5015 | INV-1004 | CUST-B | 60.00 | Payment dated 2026-04-05, after as-of | Quarantined (`TRANSACTION_AFTER_AS_OF`) |
| 18 | PAY-5016 | INV-1001 | CUST-A | `1.2e3` | Exponent notation | Quarantined (`INVALID_AMOUNT`) |

## Hand calculation of the totals

Eligible invoices (8): rows 2–9.

- **I** = 1200.00 + 2500.00 + 800.00 + 450.00 + 975.50 + 640.25 + 300.00 + 720.00 = **7,585.75**
- **A** (applied) = 1200.00 + 2500.00 + 500.00 + 500.00 + 0 + 640.25 + 0 + 720.00 = **6,060.25**
- **O** (outstanding) = 300.00 (INV-1003) + 975.50 (INV-1005) + 300.00 (INV-1007) = **1,575.50**
- **X** (overpayment) = 500.00 − 450.00 (INV-1004) = **50.00**

Eligible payments (13): rows 2–14.

- **A** (APPLIED, rows 2–8) = 1200 + 1000 + 1500 + 500 + 500 + 640.25 + 720 = **6,060.25** (agrees with the invoice side)
- **U** (UNMATCHED, rows 9–12) = 125.00 + 975.50 + 310.00 + 250.00 = **1,660.50**
- **M** (AMBIGUOUS, row 13) = **1,000.00**
- **B** (BLOCKED, row 14) = **200.00**
- **P** = 6,060.25 + 1,660.50 + 1,000.00 + 200.00 = **8,920.75**

Invariants:

- I − A = 7,585.75 − 6,060.25 = **1,525.50**; O − X = 1,575.50 − 50.00 = **1,525.50** ✔
- P = A + U + M + B = **8,920.75** ✔
- I − P = **−1,335.00**; O − X − U − M − B = 1,575.50 − 50.00 − 1,660.50 − 1,000.00 − 200.00 = **−1,335.00** ✔

Other expected figures:

| Measure | Expected |
|---|---|
| Invoice rows / eligible / quarantined | 16 / 8 / 8 |
| Payment rows / eligible / quarantined | 17 / 13 / 4 |
| Invoice statuses | PAID 4, PARTIALLY_PAID 1, OVERPAID 1, UNPAID 2 |
| Payment statuses | APPLIED 7, UNMATCHED 4, AMBIGUOUS 1, BLOCKED 1 |
| Overdue invoices / overdue outstanding | 2 / 1,275.50 (300.00 + 975.50) |
| Data completeness | INCOMPLETE (12 rows quarantined) |
| Exceptions | 19 = 9 validation (2 invoice duplicate groups, 4 invoice rows, 1 payment duplicate group, 2 payment rows) + 3 invoice (INV-1003 partial, INV-1004 overpaid, INV-1005 unpaid & overdue) + 7 payment (6 not applied + 1 informational payment-before-invoice) |

INV-1007 is unpaid but not yet due, so it is reported in the results without opening an
exception.

The net difference of −1,335.00 hides 1,575.50 that is still owed and 50.00 that was
overpaid. A small net difference never means the ledgers agree.

## Reporting views (`rk_v1`)

Derived by hand from the figures above. `tests/test_reporting_views.py` asserts each one.

- **Settled invoice value** = I − O = 7,585.75 − 1,575.50 = **6,010.25** (also A − X =
  6,060.25 − 50.00).
- **Settled share** = 6,010.25 / 7,585.75 = 79.2308…% → **79.23%** (rounded half up).
- **Open invoices** (3), as of 2026-03-31:

| Invoice | Customer | Outstanding | Days overdue | Aging bucket | Exception |
|---|---|---:|---:|---|---|
| INV-1003 | CUST-B | 300.00 | 45 (due 2026-02-14) | `DAYS_31_60` | yes (partially paid) |
| INV-1005 | CUST-C | 975.50 | 19 (due 2026-03-12) | `DAYS_1_30` | yes (unpaid, overdue) |
| INV-1007 | CUST-D | 300.00 | not overdue (due 2026-04-30) | `NOT_OVERDUE` | none |

- **Outstanding by customer** (share of O = 1,575.50, rounded half up to 0.1%): CUST-C
  975.50 (61.9%), CUST-B 300.00 (19.0%), CUST-D 300.00 (19.0%). The shares add to 99.9%
  because of rounding.
- **Payment exceptions** (6 eligible payments not applied): PAY-5008 125.00, PAY-5009
  975.50, PAY-5010 310.00, PAY-5011 250.00 (UNMATCHED); PAY-5012 1,000.00 (AMBIGUOUS);
  PAY-5013 200.00 (BLOCKED). Total **2,860.50** = U + M + B.
- **Unresolved exceptions**: all **19** are OPEN (no review events): INVOICE/REVIEW 3,
  PAYMENT/INFO 1, PAYMENT/REVIEW 6, VALIDATION/QUARANTINE 9.
