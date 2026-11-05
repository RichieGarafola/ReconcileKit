-- ReconcileKit reporting contract rk_v1: read-only views over the DuckDB storage tables.
--
-- * Every view carries run_id; filter on it. Rows from different runs are never combined.
-- * Money is exact: *_cents columns are BIGINT integer cents (the stored values), and
--   *_usd columns are DECIMAL(38,2) computed from them by rk_v1.cents_to_usd. No column
--   is ever derived through floating point.
-- * Views have no guaranteed row order; consumers should ORDER BY.
-- * Storage tables are schema-qualified (main.*) so the views also resolve when the
--   database is ATTACHed under an alias (e.g. ATTACH 'recon.duckdb' AS rk).
-- * Column names, types, and meanings in rk_v1 do not change. A breaking change would be
--   published as a new schema (rk_v2) alongside this one.
-- See docs/sql-reporting.md for the column reference.

CREATE SCHEMA IF NOT EXISTS rk_v1;

-- Exact integer cents -> DECIMAL(38,2). Multiplication keeps DuckDB in DECIMAL arithmetic;
-- division would produce DOUBLE. NULL in, NULL out.
CREATE OR REPLACE MACRO rk_v1.cents_to_usd(cents) AS
    CAST(CAST(cents AS DECIMAL(38, 0)) * CAST(0.01 AS DECIMAL(3, 2)) AS DECIMAL(38, 2));

CREATE OR REPLACE VIEW rk_v1.contract AS
SELECT
    'rk_v1' AS contract_version,
    (SELECT max(version) FROM main.schema_info) AS storage_schema_version;

-- One row per exception, with the latest review state (by insertion sequence).
CREATE OR REPLACE VIEW rk_v1.exception_status AS
WITH review_summary AS (
    SELECT
        run_id,
        exception_id,
        count(*) AS review_event_count,
        arg_max(disposition, seq) AS latest_disposition,
        arg_max(reviewer, seq) AS latest_reviewer,
        arg_max(note, seq) AS latest_note,
        arg_min(created_at_utc, seq) AS first_reviewed_at_utc,
        arg_max(created_at_utc, seq) AS latest_reviewed_at_utc
    FROM main.review_events
    GROUP BY run_id, exception_id
)
SELECT
    e.run_id,
    e.exception_id,
    e.category,
    e.rule_code,
    e.severity,
    e.classification,
    e.customer_id,
    e.invoice_id,
    e.payment_id,
    e.source_refs,
    e.record_ids,
    e.amount_cents,
    rk_v1.cents_to_usd(e.amount_cents) AS amount_usd,
    e.explanation,
    coalesce(r.latest_disposition, 'OPEN') AS current_disposition,
    coalesce(r.latest_disposition, 'OPEN') = 'RESOLVED_EXTERNALLY' AS is_resolved,
    coalesce(r.review_event_count, 0) AS review_event_count,
    r.first_reviewed_at_utc,
    r.latest_reviewed_at_utc,
    r.latest_reviewer,
    r.latest_note
FROM main.exceptions AS e
LEFT JOIN review_summary AS r
    ON r.run_id = e.run_id AND r.exception_id = e.exception_id;

-- One row per run: metadata, the stored bridge totals, and metrics derived from them.
CREATE OR REPLACE VIEW rk_v1.run_summary AS
WITH exception_counts AS (
    SELECT
        run_id,
        count(*) FILTER (WHERE NOT is_resolved) AS unresolved_exception_count
    FROM rk_v1.exception_status
    GROUP BY run_id
),
base AS (
    SELECT
        r.run_id,
        r.created_at_utc,
        r.as_of_date,
        r.engine_version,
        r.rule_version,
        r.invoice_label,
        r.invoice_sha256,
        r.invoice_rows,
        r.eligible_invoices,
        r.quarantined_invoice_rows,
        r.payment_label,
        r.payment_sha256,
        r.payment_rows,
        r.eligible_payments,
        r.quarantined_payment_rows,
        r.is_complete,
        r.eligible_invoice_cents,
        r.applied_payment_cents,
        r.outstanding_cents,
        r.overpayment_cents,
        r.eligible_payment_cents,
        r.unmatched_payment_cents,
        r.ambiguous_payment_cents,
        r.blocked_payment_cents,
        r.overdue_cents,
        r.overdue_invoices,
        r.exception_count,
        r.eligible_invoice_cents - r.outstanding_cents AS settled_invoice_cents,
        r.eligible_invoice_cents - r.eligible_payment_cents AS net_difference_cents,
        r.outstanding_cents - r.overpayment_cents - r.unmatched_payment_cents
            - r.ambiguous_payment_cents - r.blocked_payment_cents AS explained_difference_cents,
        r.eligible_invoice_cents - r.applied_payment_cents
            = r.outstanding_cents - r.overpayment_cents AS invariant_invoice_side_ok,
        r.eligible_payment_cents = r.applied_payment_cents + r.unmatched_payment_cents
            + r.ambiguous_payment_cents + r.blocked_payment_cents AS invariant_payment_side_ok
    FROM main.runs AS r
)
SELECT
    b.run_id,
    b.created_at_utc,
    b.as_of_date,
    b.engine_version,
    b.rule_version,
    b.invoice_label,
    b.invoice_sha256,
    b.invoice_rows,
    b.eligible_invoices,
    b.quarantined_invoice_rows,
    b.payment_label,
    b.payment_sha256,
    b.payment_rows,
    b.eligible_payments,
    b.quarantined_payment_rows,
    b.is_complete,
    CASE WHEN b.is_complete THEN 'COMPLETE' ELSE 'INCOMPLETE' END AS data_completeness,
    b.eligible_invoice_cents,
    b.applied_payment_cents,
    b.outstanding_cents,
    b.overpayment_cents,
    b.eligible_payment_cents,
    b.unmatched_payment_cents,
    b.ambiguous_payment_cents,
    b.blocked_payment_cents,
    b.overdue_cents,
    b.overdue_invoices,
    b.settled_invoice_cents,
    b.net_difference_cents,
    b.explained_difference_cents,
    rk_v1.cents_to_usd(b.eligible_invoice_cents) AS eligible_invoice_usd,
    rk_v1.cents_to_usd(b.applied_payment_cents) AS applied_payment_usd,
    rk_v1.cents_to_usd(b.outstanding_cents) AS outstanding_usd,
    rk_v1.cents_to_usd(b.overpayment_cents) AS overpayment_usd,
    rk_v1.cents_to_usd(b.eligible_payment_cents) AS eligible_payment_usd,
    rk_v1.cents_to_usd(b.unmatched_payment_cents) AS unmatched_payment_usd,
    rk_v1.cents_to_usd(b.ambiguous_payment_cents) AS ambiguous_payment_usd,
    rk_v1.cents_to_usd(b.blocked_payment_cents) AS blocked_payment_usd,
    rk_v1.cents_to_usd(b.overdue_cents) AS overdue_usd,
    rk_v1.cents_to_usd(b.settled_invoice_cents) AS settled_invoice_usd,
    rk_v1.cents_to_usd(b.net_difference_cents) AS net_difference_usd,
    rk_v1.cents_to_usd(b.explained_difference_cents) AS explained_difference_usd,
    -- Share of eligible invoice value settled by applied payments, in percent, rounded half
    -- up to 0.01 with integer arithmetic. NULL when there are no eligible invoices.
    CASE
        WHEN b.eligible_invoice_cents = 0 THEN NULL
        ELSE CAST(
            CAST(
                (CAST(b.settled_invoice_cents AS HUGEINT) * 20000 + b.eligible_invoice_cents)
                // (CAST(b.eligible_invoice_cents AS HUGEINT) * 2)
                AS DECIMAL(38, 0)
            ) * CAST(0.01 AS DECIMAL(3, 2))
            AS DECIMAL(38, 2)
        )
    END AS settled_invoice_pct,
    b.invariant_invoice_side_ok,
    b.invariant_payment_side_ok,
    b.net_difference_cents = b.explained_difference_cents AS invariant_bridge_ok,
    CASE
        WHEN b.invariant_invoice_side_ok
            AND b.invariant_payment_side_ok
            AND b.net_difference_cents = b.explained_difference_cents
            THEN 'PASS'
        ELSE 'FAIL'
    END AS arithmetic_status,
    b.exception_count,
    coalesce(c.unresolved_exception_count, 0) AS unresolved_exception_count
FROM base AS b
LEFT JOIN exception_counts AS c ON c.run_id = b.run_id;

-- One row per eligible invoice with an outstanding balance (UNPAID or PARTIALLY_PAID).
CREATE OR REPLACE VIEW rk_v1.open_invoices AS
SELECT
    i.run_id,
    r.as_of_date,
    i.record_id AS invoice_record_id,
    i.source_row,
    i.customer_id,
    i.invoice_id,
    i.invoice_date,
    i.due_date,
    i.invoice_amount_cents,
    i.applied_payment_cents,
    i.outstanding_cents,
    rk_v1.cents_to_usd(i.invoice_amount_cents) AS invoice_amount_usd,
    rk_v1.cents_to_usd(i.applied_payment_cents) AS applied_payment_usd,
    rk_v1.cents_to_usd(i.outstanding_cents) AS outstanding_usd,
    i.status,
    i.rule_code,
    len(i.payment_record_ids) AS applied_payment_count,
    i.is_overdue,
    i.days_overdue,
    CASE
        WHEN NOT i.is_overdue THEN 'NOT_OVERDUE'
        WHEN i.days_overdue <= 30 THEN 'DAYS_1_30'
        WHEN i.days_overdue <= 60 THEN 'DAYS_31_60'
        WHEN i.days_overdue <= 90 THEN 'DAYS_61_90'
        ELSE 'DAYS_OVER_90'
    END AS aging_bucket,
    x.exception_id,
    x.current_disposition
FROM main.invoice_results AS i
JOIN main.runs AS r ON r.run_id = i.run_id
LEFT JOIN rk_v1.exception_status AS x
    ON x.run_id = i.run_id
    AND x.category = 'INVOICE'
    AND x.record_ids[1] = i.record_id
WHERE i.outstanding_cents > 0;

-- One row per eligible payment that was not applied (UNMATCHED, AMBIGUOUS, or BLOCKED).
-- Quarantined payment rows have no trusted amount; they appear in exception_status with
-- category VALIDATION.
CREATE OR REPLACE VIEW rk_v1.payment_exceptions AS
SELECT
    p.run_id,
    r.as_of_date,
    p.record_id AS payment_record_id,
    p.source_row,
    p.payment_id,
    p.customer_id,
    p.invoice_id,
    p.invoice_id_raw,
    p.payment_date,
    p.payment_amount_cents,
    rk_v1.cents_to_usd(p.payment_amount_cents) AS payment_amount_usd,
    p.status,
    p.rule_code,
    p.explanation,
    p.related_invoice_record_ids,
    x.exception_id,
    x.current_disposition,
    x.is_resolved,
    x.latest_reviewed_at_utc
FROM main.payment_results AS p
JOIN main.runs AS r ON r.run_id = p.run_id
LEFT JOIN rk_v1.exception_status AS x
    ON x.run_id = p.run_id
    AND x.category = 'PAYMENT'
    AND x.severity = 'REVIEW'
    AND x.record_ids[1] = p.record_id
WHERE p.status <> 'APPLIED';
