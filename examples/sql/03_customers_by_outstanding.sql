-- Which customers account for the largest outstanding balances?
-- Share denominator: the run's total outstanding balance (O), computed exactly in integer
-- cents and rounded half up to 0.1 percent.
WITH by_customer AS (
    SELECT customer_id, count(*) AS open_invoices, sum(outstanding_cents) AS outstanding_cents
    FROM rk_v1.open_invoices
    WHERE run_id = $run_id
    GROUP BY customer_id
),
total AS (
    SELECT outstanding_cents AS total_cents FROM rk_v1.run_summary WHERE run_id = $run_id
)
SELECT
    c.customer_id,
    c.open_invoices,
    rk_v1.cents_to_usd(c.outstanding_cents) AS outstanding_usd,
    CAST(
        CAST(
            (CAST(c.outstanding_cents AS HUGEINT) * 2000 + t.total_cents)
            // (CAST(t.total_cents AS HUGEINT) * 2)
            AS DECIMAL(38, 0)
        )
        * CAST(0.1 AS DECIMAL(2, 1))
        AS DECIMAL(38, 1)
    ) AS share_of_outstanding_pct
FROM by_customer AS c
CROSS JOIN total AS t
ORDER BY c.outstanding_cents DESC, c.customer_id;
