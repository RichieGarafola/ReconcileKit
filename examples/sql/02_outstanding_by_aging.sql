-- What is the outstanding invoice balance, and how old is it?
SELECT
    aging_bucket,
    count(*) AS open_invoices,
    rk_v1.cents_to_usd(sum(outstanding_cents)) AS outstanding_usd
FROM rk_v1.open_invoices
WHERE run_id = $run_id
GROUP BY aging_bucket
ORDER BY
    CASE aging_bucket
        WHEN 'NOT_OVERDUE' THEN 0
        WHEN 'DAYS_1_30' THEN 1
        WHEN 'DAYS_31_60' THEN 2
        WHEN 'DAYS_61_90' THEN 3
        ELSE 4
    END;
