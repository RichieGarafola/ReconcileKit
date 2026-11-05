-- Which reconciliation runs are stored, and which one should I report on?
-- Pick the run_id you need and pass it to the other queries as --param run_id=RUN-...
SELECT
    run_id,
    created_at_utc,
    as_of_date,
    invoice_label,
    payment_label,
    data_completeness,
    arithmetic_status,
    unresolved_exception_count
FROM rk_v1.run_summary
ORDER BY created_at_utc DESC, run_id;
