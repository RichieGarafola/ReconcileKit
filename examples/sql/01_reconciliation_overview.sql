-- What share of invoice value has been settled, and does the bridge tie out?
-- Denominator: eligible invoice total (I). Quarantined invoice rows are excluded, so check
-- data_completeness before quoting the percentage as covering the whole register.
SELECT
    run_id,
    as_of_date,
    data_completeness,
    eligible_invoice_usd,
    settled_invoice_usd,
    settled_invoice_pct,
    outstanding_usd,
    overpayment_usd,
    net_difference_usd,
    explained_difference_usd,
    arithmetic_status
FROM rk_v1.run_summary
WHERE run_id = $run_id;
