# ReconcileKit

[![CI](https://github.com/RichieGarafola/ReconcileKit/actions/workflows/ci.yml/badge.svg)](https://github.com/RichieGarafola/ReconcileKit/actions/workflows/ci.yml)
[![Dependency audit](https://github.com/RichieGarafola/ReconcileKit/actions/workflows/audit.yml/badge.svg)](https://github.com/RichieGarafola/ReconcileKit/actions/workflows/audit.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Why don't the invoice register and the payment ledger agree, and which records need a
human to look at them?**

ReconcileKit reconciles an invoice register against a payment ledger with explicit,
explainable rules. It works in exact integer cents, quarantines invalid or duplicated rows
instead of guessing, ties the totals out through a bridge whose arithmetic is checked on
every run, and keeps an append-only review trail that never alters the automated findings.
It runs locally as a CLI and a Streamlit app that share one engine and one DuckDB database.

![Overview of the bundled demo run](docs/screenshots/overview.jpg)

<sub>Screenshot from the running app with the bundled synthetic demo.</sub>

## The problem

Month-end close and collections work often comes down to two exports that should agree and
don't. The questions are always the same: which invoices are still open, which payments
can't be tied to an invoice, whether a gap is real or caused by bad data, and who has looked
at each issue.

Spreadsheet lookups answer quickly, but they hide the failure modes that matter in finance:

| Spreadsheet habit | What goes wrong | ReconcileKit instead |
|---|---|---|
| `VLOOKUP` takes the first match | A duplicated invoice silently absorbs a payment | Every row of a duplicated identity is quarantined; payments to it are **AMBIGUOUS** |
| Floats and rounding | `99.999` becomes `100.00`; one-cent drift creates false exceptions | Strict text-to-integer-cents parsing; anything unparseable is rejected, never rounded |
| Bad rows are dropped or coerced | Totals look complete but aren't | Bad rows are listed with reasons and the run is labelled **incomplete** |
| Fuzzy or amount-based matching | Confident-looking wrong allocations | Exact `customer_id + invoice_id` only; near misses are explained, not matched |
| Edits overwrite the evidence | No record of what the data said | Findings are immutable; reviews are separate, append-only events |

## How it works

```mermaid
flowchart LR
    A[invoices.csv<br/>payments.csv] --> B[Ingestion<br/>encoding, structure,<br/>header contract]
    B -->|file-level error| R[Rejected<br/>nothing saved]
    B --> C[Validation<br/>Pandera rules,<br/>duplicate groups]
    C -->|row-level failure| Q[Quarantine<br/>with reasons]
    C --> D[Reconciliation<br/>exact matching,<br/>classification]
    D --> E{Invariants<br/>hold?}
    E -->|no| F[Run refused<br/>exit 1]
    E -->|yes| G[(DuckDB<br/>atomic, idempotent)]
    G --> H[Review events<br/>append-only]
    G --> I[Export ZIP<br/>CSV + JSON + hashes]
    Q --> G
```

Every payment lands in exactly one category, decided by the first rule that applies:

```mermaid
flowchart TD
    P[Eligible payment] --> R1{Invoice reference<br/>blank?}
    R1 -->|yes| U1[UNMATCHED<br/>PAYMENT_REFERENCE_MISSING]
    R1 -->|no| R2{customer + invoice identity<br/>on more than one row?}
    R2 -->|yes| M[AMBIGUOUS]
    R2 -->|no| R3{On exactly one row<br/>that was quarantined?}
    R3 -->|yes| B[BLOCKED]
    R3 -->|no| R4{On exactly one<br/>eligible row?}
    R4 -->|yes| A[APPLIED]
    R4 -->|no| R5{Invoice ID exists for<br/>another customer?}
    R5 -->|yes| U2[UNMATCHED<br/>CUSTOMER_REFERENCE_CONFLICT]
    R5 -->|no| U3[UNMATCHED<br/>PAYMENT_REFERENCE_NOT_FOUND]
```

Invoices are then classified **UNPAID**, **PARTIALLY_PAID**, **PAID**, or **OVERPAID** from
the payments applied to them, and overdue status is computed against an explicit as-of date
(never the machine clock).

## The reconciliation bridge

Each run explains the net difference between invoices and payments line by line, and the
engine refuses to save a run unless these identities hold exactly, in cents:

| Symbol | Meaning |
|---|---|
| I / P | eligible invoice total / eligible payment total |
| A | payments applied to invoices |
| O / X | outstanding balances / overpayments |
| U / M / B | unmatched / ambiguous / blocked payments |

**I − A = O − X**, **P = A + U + M + B**, and therefore **I − P = O − X − U − M − B**.

Output of `reconcilekit demo` (unedited; reproduced by
[`scripts/generate_sample_outputs.py`](scripts/generate_sample_outputs.py)):

```text
Run RUN-8a40f8d27cbbcb5f (new)
As-of date: 2026-03-31
Data completeness: INCOMPLETE - 8 invoice and 4 payment rows quarantined. Totals cover eligible records only.
    Eligible invoices                     $7,585.75
  - Eligible payments                     $8,920.75
  = Net difference                       -$1,335.00
  + Outstanding invoice balances          $1,575.50
  - Overpayments                             $50.00
  - Unmatched payments                    $1,660.50
  - Ambiguous payments                    $1,000.00
  - Blocked payments                        $200.00
  = Explained difference                 -$1,335.00
  Invariant I - A = O - X: PASS
  Invariant P = A + U + M + B: PASS
  Invariant I - P = O - X - U - M - B: PASS
Exceptions requiring review: 19 {'OPEN': 19}
Export written: exports/reconcilekit_RUN-8a40f8d27cbbcb5f.zip
```

The demo is *incomplete* on purpose: it plants duplicates, a blank amount, `99.999`, a EUR
row, and a future-dated invoice to show quarantine. Outstanding balances and overpayments
offset in the net difference, so a small net difference never means the books are settled.
That is why the bridge is shown in full.

## Features

- **Exact money.** Amounts are validated as text and converted to integer cents with
  `Decimal`; sums, comparisons, and storage (DuckDB `BIGINT`) never use floats.
- **Two-level validation.** File-level problems (encoding, headers, malformed CSV) stop the
  run. Row-level problems quarantine only the affected rows, each with a stable rule code.
- **Explainable outcomes.** Every result carries a rule code, a plain-language explanation,
  source row numbers, and raw plus normalized identifiers.
- **Review workflow.** Reviewers append dispositions (OPEN, ACKNOWLEDGED, INVESTIGATING,
  RESOLVED_EXTERNALLY) with notes; retries are idempotent and conflicting retries are rejected.
- **Reproducible runs.** The run ID is a fingerprint of the input bytes, rules, versions, and
  as-of date, so re-importing identical files reuses the stored run.
- **Portable evidence.** The export ZIP contains results, exceptions, validation errors,
  review history, and a manifest with SHA-256 hashes of every packaged file.
- **Spreadsheet-safe exports.** Cells that a spreadsheet could execute as a formula are
  escaped, in the export package and in the app's table downloads.

## Quick start

Requires Python 3.11–3.13. These steps were run on Windows 11 with CPython 3.11.9.

```bash
git clone https://github.com/RichieGarafola/ReconcileKit.git && cd ReconcileKit
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install -c requirements-lock.txt -e ".[dev]"

reconcilekit demo --export-dir exports   # CLI: prints the bridge, writes the ZIP (exit code 2, see below)
streamlit run app.py                     # UI: click "Load bundled demo"
```

No credentials, network services, or API keys are needed.

**Your own files:**

```bash
reconcilekit reconcile --invoices invoices.csv --payments payments.csv \
    --as-of 2026-03-31 --db data/reconcilekit.duckdb --export-dir exports [--json]

reconcilekit export --run-id RUN-xxxxxxxxxxxxxxxx --db data/reconcilekit.duckdb --export-dir exports
```

Required columns (any order, extra columns ignored):

- **Invoices:** `invoice_id, customer_id, invoice_date, due_date, currency, invoice_amount`
- **Payments:** `payment_id, invoice_id, customer_id, payment_date, currency, payment_amount`

Dates are `YYYY-MM-DD`, currency must be `USD`, and amounts are positive plain decimals with
at most two places. The full input contract is in the [reference](docs/reference.md#input-contract).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Run completed, every row was eligible, invariants passed. |
| `2` | Invalid input: a file-level error (nothing saved), **or** the run completed but some rows were quarantined (*incomplete*; still saved and exported). Usage errors also exit 2. |
| `1` | Internal failure, including a financial-invariant violation (nothing saved). Set `RECONCILEKIT_DEBUG=1` to print the traceback. |

Business findings such as unpaid invoices do **not** change the exit code, because finding
them is the tool working correctly. Scripts should read the `--json` output
(`status`, `exception_count`, `invoice_status_counts`, `payment_status_counts`,
`net_difference_cents`, …); see [`docs/sample-output/cli-demo.json`](docs/sample-output/cli-demo.json).

### Installation notes

- **Reproducible install:** `requirements-lock.txt` pins every package version used for
  testing and is applied as pip *constraints*. Lower bounds in `pyproject.toml` are the
  tested versions; newer versions within the upper bounds are allowed but untested.
- **Wheel vs. checkout:** an installed wheel provides the CLI and the bundled demo. The
  Streamlit UI (`app.py`) and the tests require a source checkout.
- **Windows:** keep the checkout and virtual environment in a short path. Streamlit ships
  deeply nested files, and a long path can fail with `WinError 206` unless long-path support
  is enabled.

## Sample outputs

[`docs/sample-output/`](docs/sample-output/) holds the demo's CLI output and the result files
from its export package: [`invoice_results.csv`](docs/sample-output/invoice_results.csv),
[`payment_results.csv`](docs/sample-output/payment_results.csv),
[`exceptions.csv`](docs/sample-output/exceptions.csv), and
[`validation_errors.csv`](docs/sample-output/validation_errors.csv). Files carrying export
timestamps are left out so the samples are byte-reproducible. A test fails if they drift from
what the code produces; regenerate them with `python scripts/generate_sample_outputs.py`.

Every expected value for the demo was also calculated by hand from the CSVs, independently
of the engine: see [`docs/demo-expected-results.md`](docs/demo-expected-results.md).

![Exception review with source evidence and history](docs/screenshots/review.jpg)

## Engineering tradeoffs

| Decision | Benefit | Cost accepted |
|---|---|---|
| Integer cents, strict parsing, no rounding | No float drift; output always agrees with the source | Inputs with `$`, thousands separators, or 3+ decimals must be cleaned upstream |
| Exact references only | No wrong-but-confident allocations | More payments land in UNMATCHED for human review |
| Quarantine whole duplicate groups | No arbitrary "first row wins" | One bad duplicate holds back its valid twin until the source is fixed |
| Immutable findings + append-only reviews | Evidence of what the data said is preserved | Fixing data means re-importing, which creates a new run |
| Embedded DuckDB | Real SQL and transactions; no server or credentials | One writer process at a time; not a multi-user service |
| Byte-level run identity | Simple, verifiable reproducibility | Reordering rows or changing line endings creates a new run |

Rationale for each is in [design decisions](docs/design-decisions.md).

## Testing and quality checks

```bash
pytest                    # full suite
ruff check . && ruff format --check .
python scripts/generate_sample_outputs.py --check
```

- **Example-based tests** cover every matching branch, duplicate groups, as-of and overdue
  boundaries, idempotent re-import, append-only reviews, export precision, formula escaping,
  a CLI end-to-end run from files to database to ZIP, and the Streamlit UI driven headlessly
  with `AppTest`.
- **Property-based tests** ([`tests/test_properties.py`](tests/test_properties.py), Hypothesis)
  generate random ledgers with deliberate collisions and compare the engine against an
  independent model of the matching contract. They check cents round-trips and rejection of
  rounding and presentation formats, agreement of every total and status with totals
  recomputed from the input via `Decimal`, exact-identity-only matching, row-order
  independence, determinism, and quarantine (never coercion) of invalid amounts.
- **CI** ([`ci.yml`](.github/workflows/ci.yml)) runs lint, format, tests, a demo smoke run,
  and a wheel install smoke test on Ubuntu (Python 3.11, 3.12, 3.13) and Windows (3.11).
- **Dependency audit** ([`audit.yml`](.github/workflows/audit.yml)) runs `pip-audit` against
  the lockfile on every change and weekly.

## Scope and limitations

Out of scope in v0.1 by design: currencies other than USD, refunds and credit notes, one
payment split across several invoices, fuzzy or amount/date matching, document or PDF
inputs, AI-generated decisions, authentication or multi-user use, cloud deployment, and
scheduled ingestion.

Known limitations:

- Single user, single writer: running the app and CLI at the same moment against the same
  database file can fail with a lock error. Use separate files (`--db`, `RECONCILEKIT_DB`).
- The database is not tamper-proof; anyone with file access can modify it. For tamper
  evidence, keep the exported package and its `manifest.json` hashes.
- Spreadsheet programs may drop leading zeros when opening exported CSVs unless identifier
  columns are imported as text. The files themselves preserve them.
- There is no hosted demo; run it locally with the steps above.

## Documentation

- [Reference](docs/reference.md): Streamlit UI, input contract, matching and duplicate rules,
  invariants, review semantics, persistence, export package, module map
- [Design decisions](docs/design-decisions.md)
- [Demo expected results](docs/demo-expected-results.md) (hand-calculated oracle)
- [Synthetic data notice](examples/README.md) · [Third-party notices](docs/third-party-notices.md)

## Data notice

All bundled data is synthetic: identifiers, customers, dates, and amounts were invented to
exercise specific rules. Do not commit real invoice or payment data. `data/`, `exports/`,
`*.duckdb`, and generated ZIP files are git-ignored.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Copyright 2026 Richie Garafola.
Third-party dependencies keep their own licenses (see
[third-party notices](docs/third-party-notices.md)).
