-- Which payment exceptions require investigation, largest first?
SELECT
    payment_id,
    customer_id,
    invoice_id_raw AS invoice_reference_as_received,
    payment_amount_usd,
    status,
    rule_code,
    current_disposition
FROM rk_v1.payment_exceptions
WHERE run_id = $run_id AND NOT is_resolved
ORDER BY payment_amount_cents DESC, payment_id;
