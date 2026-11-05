-- How many unresolved exceptions remain, by category and severity?
-- "Unresolved" means the latest review disposition is anything but RESOLVED_EXTERNALLY
-- (an exception with no review events is OPEN). Amounts are deliberately not summed here:
-- an exception's amount means different things per category (outstanding balance or
-- overpayment for invoices, payment amount for payments, none for quarantined rows).
SELECT
    category,
    severity,
    count(*) AS unresolved_exceptions,
    count(*) FILTER (WHERE review_event_count = 0) AS never_reviewed
FROM rk_v1.exception_status
WHERE run_id = $run_id AND NOT is_resolved
GROUP BY category, severity
ORDER BY category, severity;
