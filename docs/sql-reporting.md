# SQL reporting contract (`rk_v1`)

ReconcileKit stores every run in a local DuckDB file. The `rk_v1` schema in that file holds
read-only views that present the results in a form analysts and BI tools can query
directly, without reassembling the export CSVs.

- **Read-only.** The views are definitions over the storage tables; DuckDB rejects
  `INSERT`, `UPDATE`, and `DELETE` against them. Nothing in `rk_v1` can change a finding.
- **Run-scoped.** Every view has `run_id`, and every join inside the views matches on it.
  Rows from different runs are never combined. Filter on `run_id` (see
  [Selecting a run](#selecting-a-run)).
- **Exact money.** `*_cents` columns are `BIGINT` integer cents, exactly as stored.
  `*_usd` columns are `DECIMAL(38,2)` computed from them with `rk_v1.cents_to_usd`, which
  uses decimal multiplication only. No view column is `DOUBLE` or `FLOAT`; a test enforces
  this.
- **Versioned.** Column names, types, and meanings in `rk_v1` will not change. A breaking
  change would be published as a new schema (`rk_v2`) next to this one, so existing
  queries and reports keep working. `SELECT contract_version FROM rk_v1.contract` tells a
  consumer which contract it is reading.
- **Deterministic.** The views contain no clock functions or random values. The same
  stored run always produces the same rows. Views have no guaranteed row order, so
  always `ORDER BY`.

## Installation

Nothing to install separately. `repository.connect()` creates or replaces the `rk_v1`
schema, macro, and views every time ReconcileKit opens a database (`CREATE OR REPLACE`,
idempotent). A database written by ReconcileKit 0.1 gains the views the next time any
ReconcileKit command opens it, including `reconcilekit query`. The definitions are in
[`src/reconcilekit/sql/rk_v1.sql`](../src/reconcilekit/sql/rk_v1.sql). Installing them
adds objects only in `rk_v1`; storage tables are untouched.

## Connecting

**ReconcileKit CLI (no extra tools):**

```bash
reconcilekit query --db data/reconcilekit.duckdb \
    --file examples/sql/01_reconciliation_overview.sql --param run_id=RUN-2e17cda32757cc29
reconcilekit query --db data/reconcilekit.duckdb --format markdown \
    --sql "SELECT run_id, arithmetic_status FROM rk_v1.run_summary"
```

`query` runs one statement on a connection that is **read-only** and has DuckDB
**external access disabled**, so it cannot modify the database, read or write other files,
or download extensions. `$name` placeholders are bound from `--param name=value` (values
are always data, never SQL text); parameters a query does not reference are ignored.
Output is CSV (text cells get the same spreadsheet formula escaping as the export package)
or a Markdown table. Errors exit with code 2.

**Python:**

```python
import duckdb

con = duckdb.connect("data/reconcilekit.duckdb", read_only=True)
con.execute(
    "SELECT customer_id, outstanding_usd FROM rk_v1.open_invoices WHERE run_id = ?",
    ["RUN-2e17cda32757cc29"],
).fetchall()
```

**DuckDB CLI or another tool:** open the file directly (`duckdb -readonly
data/reconcilekit.duckdb`), or attach it under an alias. Storage tables are referenced
as `main.*` inside the views, so they resolve under an alias too:

```sql
ATTACH 'data/reconcilekit.duckdb' AS rk (READ_ONLY);
SELECT * FROM rk.rk_v1.run_summary;
```

DuckDB allows one writing process per file. Close the Streamlit app or other writers
before opening the file from another process, or query a copy.

**Parquet, for BI tools:**

```bash
reconcilekit parquet --db data/reconcilekit.duckdb --run-id RUN-2e17cda32757cc29 --out-dir parquet
```

This writes `run_summary.parquet`, `open_invoices.parquet`, `payment_exceptions.parquet`,
and `exception_status.parquet`, each filtered to the one run and sorted (`run_id`,
`source_row`, `source_row`, `exception_id`). Columns and types are exactly the view
contract below: cents stay `INT64`, dollar amounts stay `DECIMAL(38,2)`, and identifiers
stay strings, so leading zeros such as `000123` survive (opening a CSV in a spreadsheet
often loses them). Parquet files are not formula-escaped, because they are not opened as
spreadsheets.

## Selecting a run

Each import of different input bytes, as-of date, or rules creates a distinct run;
re-importing identical files reuses the stored run. Choose the run explicitly:

```sql
-- every run, newest first (examples/sql/00_list_runs.sql)
SELECT run_id, created_at_utc, as_of_date, invoice_label, payment_label, data_completeness
FROM rk_v1.run_summary
ORDER BY created_at_utc DESC, run_id;

-- the newest run for a given as-of date
SELECT run_id FROM rk_v1.run_summary
WHERE as_of_date = DATE '2026-03-31'
ORDER BY created_at_utc DESC, run_id
LIMIT 1;
```

There is deliberately no "current run" view: which run is authoritative is a business
decision (two runs for the same date can come from different extracts).

## Financial definitions

All totals cover **eligible** records only. Quarantined rows (invalid values, duplicate
identities, dates after the as-of date) are excluded from every monetary figure. When
`data_completeness = 'INCOMPLETE'`, the totals describe the valid records, not the whole
upload.

| Symbol | Column | Meaning |
|---|---|---|
| I | `eligible_invoice_cents` | Sum of eligible invoice amounts |
| A | `applied_payment_cents` | Sum of payments applied to invoices |
| O | `outstanding_cents` | Σ max(invoice − applied, 0) |
| X | `overpayment_cents` | Σ max(applied − invoice, 0) |
| P | `eligible_payment_cents` | Sum of eligible payment amounts |
| U / M / B | `unmatched_` / `ambiguous_` / `blocked_payment_cents` | Eligible payments not applied, by reason |

Derived metrics, with their denominators:

- **Settled invoice value** `settled_invoice_cents` = I − O: invoice value covered by
  applied payments, excluding overpayment. Equals A − X.
- **Settled share** `settled_invoice_pct` = (I − O) / I × 100, rounded half up to 0.01
  percentage points using integer arithmetic. Denominator: eligible invoice total I.
  `NULL` when I = 0.
- **Net difference** `net_difference_cents` = I − P. **Explained difference**
  `explained_difference_cents` = O − X − U − M − B. They are always equal (the engine
  refuses to store a run otherwise); outstanding balances and overpayments offset, so a
  small net difference never means the ledgers agree.
- **Unresolved exception**: latest disposition is not `RESOLVED_EXTERNALLY`. An exception
  with no review events is `OPEN`. `ACKNOWLEDGED` and `INVESTIGATING` are unresolved.
- **Aging bucket**: from `days_overdue` (as-of date − due date) for invoices with
  `is_overdue`; `NOT_OVERDUE` when the due date is on or after the as-of date.

## Column reference

"Stored" columns are copied from the storage tables written by the engine. "Derived"
columns are computed by the view from stored values.

### `rk_v1.run_summary`

One row per run.

| Column | Type | Source | Description |
|---|---|---|---|
| `run_id` | VARCHAR | stored | Run identifier (`RUN-` + 16 hex digits) |
| `created_at_utc` | TIMESTAMP | stored | When the run was first stored (UTC) |
| `as_of_date` | DATE | stored | Snapshot date used for validation and aging |
| `engine_version` | VARCHAR | stored | ReconcileKit version that produced the run |
| `rule_version` | VARCHAR | stored | Rule set version |
| `invoice_label` | VARCHAR | stored | Safe display name of the invoice file |
| `invoice_sha256` | VARCHAR | stored | SHA-256 of the invoice file bytes |
| `invoice_rows` | INTEGER | stored | Data rows read from the invoice file |
| `eligible_invoices` | INTEGER | stored | Invoice rows included in totals |
| `quarantined_invoice_rows` | INTEGER | stored | Invoice rows excluded by validation |
| `payment_label` | VARCHAR | stored | Safe display name of the payment file |
| `payment_sha256` | VARCHAR | stored | SHA-256 of the payment file bytes |
| `payment_rows` | INTEGER | stored | Data rows read from the payment file |
| `eligible_payments` | INTEGER | stored | Payment rows included in totals |
| `quarantined_payment_rows` | INTEGER | stored | Payment rows excluded by validation |
| `is_complete` | BOOLEAN | stored | True when no row was quarantined |
| `data_completeness` | VARCHAR | derived | `COMPLETE` or `INCOMPLETE` |
| `eligible_invoice_cents` | BIGINT | stored | I |
| `applied_payment_cents` | BIGINT | stored | A |
| `outstanding_cents` | BIGINT | stored | O |
| `overpayment_cents` | BIGINT | stored | X |
| `eligible_payment_cents` | BIGINT | stored | P |
| `unmatched_payment_cents` | BIGINT | stored | U |
| `ambiguous_payment_cents` | BIGINT | stored | M |
| `blocked_payment_cents` | BIGINT | stored | B |
| `overdue_cents` | BIGINT | stored | Outstanding on invoices past due |
| `overdue_invoices` | INTEGER | stored | Number of overdue invoices |
| `settled_invoice_cents` | BIGINT | derived | I − O |
| `net_difference_cents` | BIGINT | derived | I − P |
| `explained_difference_cents` | BIGINT | derived | O − X − U − M − B |
| `eligible_invoice_usd` | DECIMAL(38,2) | derived | I in dollars |
| `applied_payment_usd` | DECIMAL(38,2) | derived | A in dollars |
| `outstanding_usd` | DECIMAL(38,2) | derived | O in dollars |
| `overpayment_usd` | DECIMAL(38,2) | derived | X in dollars |
| `eligible_payment_usd` | DECIMAL(38,2) | derived | P in dollars |
| `unmatched_payment_usd` | DECIMAL(38,2) | derived | U in dollars |
| `ambiguous_payment_usd` | DECIMAL(38,2) | derived | M in dollars |
| `blocked_payment_usd` | DECIMAL(38,2) | derived | B in dollars |
| `overdue_usd` | DECIMAL(38,2) | derived | Overdue outstanding in dollars |
| `settled_invoice_usd` | DECIMAL(38,2) | derived | I − O in dollars |
| `net_difference_usd` | DECIMAL(38,2) | derived | I − P in dollars |
| `explained_difference_usd` | DECIMAL(38,2) | derived | O − X − U − M − B in dollars |
| `settled_invoice_pct` | DECIMAL(38,2) | derived | (I − O) / I × 100; `NULL` when I = 0 |
| `invariant_invoice_side_ok` | BOOLEAN | derived | I − A = O − X |
| `invariant_payment_side_ok` | BOOLEAN | derived | P = A + U + M + B |
| `invariant_bridge_ok` | BOOLEAN | derived | I − P = O − X − U − M − B |
| `arithmetic_status` | VARCHAR | derived | `PASS` when all three invariants hold, else `FAIL` |
| `exception_count` | INTEGER | stored | Exceptions created by the run |
| `unresolved_exception_count` | BIGINT | derived | Exceptions whose latest disposition is not `RESOLVED_EXTERNALLY` |

Completeness, arithmetic, and open exceptions are reported separately on purpose; there is
no single "balanced" flag that would merge them.

### `rk_v1.open_invoices`

One row per eligible invoice with an outstanding balance (status `UNPAID` or
`PARTIALLY_PAID`). Fully paid and overpaid invoices are not open; overpayments appear in
`run_summary.overpayment_cents`. Quarantined invoice rows are not listed (see
`exception_status`, category `VALIDATION`).

| Column | Type | Source | Description |
|---|---|---|---|
| `run_id` | VARCHAR | stored | Run identifier |
| `as_of_date` | DATE | stored | Run snapshot date |
| `invoice_record_id` | VARCHAR | stored | Stable row ID within the run (e.g. `INV-R00004`) |
| `source_row` | INTEGER | stored | Row number in the invoice file (header = 1) |
| `customer_id` | VARCHAR | stored | Customer identifier (surrounding whitespace trimmed) |
| `invoice_id` | VARCHAR | stored | Invoice identifier (surrounding whitespace trimmed) |
| `invoice_date` | DATE | stored | Invoice date |
| `due_date` | DATE | stored | Due date |
| `invoice_amount_cents` | BIGINT | stored | Invoice amount |
| `applied_payment_cents` | BIGINT | stored | Payments applied to this invoice |
| `outstanding_cents` | BIGINT | stored | Amount still owed (> 0) |
| `invoice_amount_usd` | DECIMAL(38,2) | derived | Invoice amount in dollars |
| `applied_payment_usd` | DECIMAL(38,2) | derived | Applied payments in dollars |
| `outstanding_usd` | DECIMAL(38,2) | derived | Outstanding in dollars |
| `status` | VARCHAR | stored | `UNPAID` or `PARTIALLY_PAID` |
| `rule_code` | VARCHAR | stored | `INVOICE_UNPAID` or `INVOICE_PARTIALLY_PAID` |
| `applied_payment_count` | BIGINT | derived | Number of payments applied |
| `is_overdue` | BOOLEAN | stored | Due date is before the as-of date |
| `days_overdue` | INTEGER | stored | Days past due (0 when not overdue) |
| `aging_bucket` | VARCHAR | derived | `NOT_OVERDUE`, `DAYS_1_30`, `DAYS_31_60`, `DAYS_61_90`, `DAYS_OVER_90` |
| `exception_id` | VARCHAR | stored | The invoice's review exception; `NULL` when none was opened (an unpaid invoice that is not yet due) |
| `current_disposition` | VARCHAR | derived | Latest review disposition of that exception; `NULL` when there is no exception |

### `rk_v1.payment_exceptions`

One row per eligible payment that was **not** applied: `UNMATCHED`, `AMBIGUOUS`, or
`BLOCKED`. Quarantined payment rows have no trusted amount and appear only in
`exception_status` (category `VALIDATION`).

| Column | Type | Source | Description |
|---|---|---|---|
| `run_id` | VARCHAR | stored | Run identifier |
| `as_of_date` | DATE | stored | Run snapshot date |
| `payment_record_id` | VARCHAR | stored | Stable row ID within the run |
| `source_row` | INTEGER | stored | Row number in the payment file (header = 1) |
| `payment_id` | VARCHAR | stored | Payment identifier |
| `customer_id` | VARCHAR | stored | Customer on the payment |
| `invoice_id` | VARCHAR | stored | Invoice reference after trimming; `NULL` when blank |
| `invoice_id_raw` | VARCHAR | stored | Invoice reference exactly as received |
| `payment_date` | DATE | stored | Payment date |
| `payment_amount_cents` | BIGINT | stored | Payment amount |
| `payment_amount_usd` | DECIMAL(38,2) | derived | Payment amount in dollars |
| `status` | VARCHAR | stored | `UNMATCHED`, `AMBIGUOUS`, or `BLOCKED` |
| `rule_code` | VARCHAR | stored | Why it was not applied (e.g. `PAYMENT_REFERENCE_NOT_FOUND`) |
| `explanation` | VARCHAR | stored | Plain-language reason, including near-miss hints |
| `related_invoice_record_ids` | VARCHAR[] | stored | Invoice rows involved (duplicates, quarantined invoice, other customers' invoice) |
| `exception_id` | VARCHAR | stored | The payment's review exception |
| `current_disposition` | VARCHAR | derived | Latest review disposition |
| `is_resolved` | BOOLEAN | derived | Latest disposition is `RESOLVED_EXTERNALLY` |
| `latest_reviewed_at_utc` | TIMESTAMP | derived | Time of the latest review event; `NULL` if never reviewed |

### `rk_v1.exception_status`

One row per exception in the review queue, with its latest review state. The latest event
is chosen by insertion order (`review_events.seq`), so events with identical timestamps
are still ordered correctly.

| Column | Type | Source | Description |
|---|---|---|---|
| `run_id` | VARCHAR | stored | Run identifier |
| `exception_id` | VARCHAR | stored | Exception identifier, unique within a run (`EX-0001`, …) |
| `category` | VARCHAR | stored | `VALIDATION`, `INVOICE`, or `PAYMENT` |
| `rule_code` | VARCHAR | stored | Rule that raised the exception |
| `severity` | VARCHAR | stored | `QUARANTINE` (excluded from totals), `REVIEW`, or `INFO` |
| `classification` | VARCHAR | stored | Automated outcome, e.g. `UNMATCHED`, `OVERPAID`, `QUARANTINED` |
| `customer_id` | VARCHAR | stored | Customer, when known |
| `invoice_id` | VARCHAR | stored | Invoice reference, when relevant |
| `payment_id` | VARCHAR | stored | Payment identifier, when relevant |
| `source_refs` | VARCHAR | stored | Human-readable source rows, e.g. `invoices rows 10, 11` |
| `record_ids` | VARCHAR[] | stored | Record IDs involved; the first is the subject record |
| `amount_cents` | BIGINT | stored | Outstanding (unpaid/partial), overpayment (overpaid), or payment amount; `NULL` for quarantined rows |
| `amount_usd` | DECIMAL(38,2) | derived | `amount_cents` in dollars |
| `explanation` | VARCHAR | stored | Plain-language explanation |
| `current_disposition` | VARCHAR | derived | Latest disposition: `OPEN` (default), `ACKNOWLEDGED`, `INVESTIGATING`, `RESOLVED_EXTERNALLY` |
| `is_resolved` | BOOLEAN | derived | `current_disposition = 'RESOLVED_EXTERNALLY'` |
| `review_event_count` | BIGINT | derived | Number of review events |
| `first_reviewed_at_utc` | TIMESTAMP | derived | Time of the first review event |
| `latest_reviewed_at_utc` | TIMESTAMP | derived | Time of the latest review event |
| `latest_reviewer` | VARCHAR | derived | Reviewer label on the latest event (self-reported free text) |
| `latest_note` | VARCHAR | derived | Note on the latest event; required for `RESOLVED_EXTERNALLY` |

Because `amount_cents` means different things per category, do not sum it across
categories.

### `rk_v1.contract`

| Column | Type | Description |
|---|---|---|
| `contract_version` | VARCHAR | Always `rk_v1` for these views |
| `storage_schema_version` | INTEGER | Version of the underlying storage tables |

`rk_v1.cents_to_usd(cents)` is also available to queries: it converts integer cents to
`DECIMAL(38,2)` exactly (`NULL` stays `NULL`).

## Example queries

[`examples/sql/`](../examples/sql/) answers common reporting questions; each takes
`--param run_id=...`. Their output for the bundled demo is committed in
[`docs/sample-output/sql/`](sample-output/sql/) and checked in CI.

| File | Question |
|---|---|
| `00_list_runs.sql` | Which runs are stored? |
| `01_reconciliation_overview.sql` | What share of invoice value is settled, and does the bridge tie out? |
| `02_outstanding_by_aging.sql` | What is outstanding, and how old is it? |
| `03_customers_by_outstanding.sql` | Which customers hold the largest outstanding balances? |
| `04_payment_exceptions.sql` | Which unresolved payment exceptions need investigation? |
| `05_unresolved_exceptions.sql` | How many unresolved exceptions remain, by category and severity? |

## Limitations

- USD only, positive amounts only, exact references only: the views report what the v0.1
  engine produces and add no new financial logic.
- Quarantined rows are counted and listed as exceptions, but their amounts are never
  presented as money.
- Reviewer labels are self-reported; there is no authentication.
- The database file is not tamper-proof. The views reflect whatever is in the file.
- `query` disables external file access for its own connection only. It is a convenience
  for a local, single-user tool, not a security boundary for untrusted users.
