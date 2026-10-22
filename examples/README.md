# Synthetic demonstration data

The demo files [`src/reconcilekit/demo_data/invoices.csv`](../src/reconcilekit/demo_data/invoices.csv) and [`payments.csv`](../src/reconcilekit/demo_data/payments.csv) are **entirely synthetic**. The identifiers, customers,
dates, and amounts were invented to exercise specific reconciliation rules. They contain no
real personal, employer, client, or business information.

Each row is a deliberate scenario. The full scenario list, expected outcomes, and
hand-calculated totals are in [`docs/demo-expected-results.md`](../docs/demo-expected-results.md).

Fixed as-of date: **2026-03-31** (`reconcilekit.demo.DEMO_AS_OF`).

Run it:

```bash
reconcilekit demo --db data/reconcilekit.duckdb --export-dir exports
```
