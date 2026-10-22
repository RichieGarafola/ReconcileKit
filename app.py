"""ReconcileKit Streamlit interface.

Presentation only: every calculation comes from ``reconcilekit.reconciliation`` and
every displayed fact is read back from the local DuckDB database, exactly as the CLI
does. Ingestion and review writes happen only inside explicit button/form
submissions, and both are idempotent, so Streamlit reruns cannot duplicate data.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

from reconcilekit import ENGINE_VERSION, RULE_VERSION, reporting
from reconcilekit.demo import DEMO_AS_OF, demo_invoices, demo_payments, load_demo
from reconcilekit.exports import (
    build_export_package,
    escape_frame,
    export_filename,
    validation_report_csv,
)
from reconcilekit.ingestion import (
    INVOICE_COLUMNS,
    MAX_DATA_ROWS,
    MAX_FILE_BYTES,
    PAYMENT_COLUMNS,
    FileValidationError,
)
from reconcilekit.models import Disposition
from reconcilekit.money import format_usd
from reconcilekit.reconciliation import InvariantViolation, reconcile_files
from reconcilekit.repository import (
    MAX_NOTE_LENGTH,
    MAX_REVIEWER_LENGTH,
    ReviewConflictError,
    ReviewError,
    add_review_event,
    connect,
    save_run,
)

log = logging.getLogger("reconcilekit.app")
DB_PATH = Path(os.environ.get("RECONCILEKIT_DB", "data/reconcilekit.duckdb"))

st.set_page_config(page_title="ReconcileKit", page_icon=":material/balance:", layout="wide")


# ----------------------------------------------------------------------------- helpers


def money(df: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    """Replace integer-cent columns with exact display strings (never floats)."""
    df = df.copy()
    for src, label in columns.items():
        df[label] = ["" if pd.isna(v) else format_usd(int(v)) for v in df[src]]
        if label != src:
            df = df.drop(columns=[src])
    return df


def run_pipeline(inv: bytes, pay: bytes, as_of, inv_label: str, pay_label: str) -> None:
    """Explicit submission action: reconcile, persist idempotently, select the run."""
    st.session_state.pop("file_errors", None)
    st.session_state.pop("export_zip", None)
    try:
        run = reconcile_files(inv, pay, as_of, inv_label, pay_label)
        outcome = save_run(DB_PATH, run)
    except FileValidationError as exc:
        st.session_state["file_errors"] = exc.problems
        return
    except InvariantViolation as exc:
        st.session_state["file_errors"] = {"engine": [f"Financial invariant failed: {exc}"]}
        return
    except Exception:  # never show a stack trace for an action the user triggered
        log.exception("Unexpected failure during reconciliation")
        st.session_state["file_errors"] = {
            "internal": ["Unexpected internal error. Details were written to the server log."]
        }
        return
    st.session_state["run_id"] = outcome.run_id
    st.session_state["run_notice"] = (
        "New run created."
        if outcome.created
        else "Identical inputs and settings were already reconciled; the existing run "
        "was reused (no facts or review events duplicated)."
    )


def template_csv(columns: tuple[str, ...]) -> bytes:
    return (",".join(columns) + "\n").encode("utf-8")


# ----------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Load data")
    if st.button("Load bundled demo", type="primary", width="stretch"):
        inv_b, pay_b = load_demo()
        run_pipeline(inv_b, pay_b, DEMO_AS_OF, "demo_invoices.csv", "demo_payments.csv")
    st.caption(f"Synthetic data, fixed as-of date {DEMO_AS_OF.isoformat()}.")

    with st.form("upload_form", border=True):
        st.subheader("Reconcile your files")
        inv_file = st.file_uploader("Invoice register (CSV)", type=["csv"])
        pay_file = st.file_uploader("Payment ledger (CSV)", type=["csv"])
        as_of = st.date_input(
            "As-of date",
            value=None,
            help="Snapshot date. Transactions after it are quarantined; overdue means "
            "due date before this date with a positive balance.",
            format="YYYY-MM-DD",
        )
        submitted = st.form_submit_button("Validate and reconcile", width="stretch")
    if submitted:
        if not inv_file or not pay_file:
            st.error("Upload both an invoice register and a payment ledger.")
        elif as_of is None:
            st.error("Choose an as-of date. ReconcileKit never assumes today's date.")
        else:
            run_pipeline(
                inv_file.getvalue(), pay_file.getvalue(), as_of, inv_file.name, pay_file.name
            )
    st.caption(
        f"Limits: {MAX_FILE_BYTES // (1024 * 1024)} MB and {MAX_DATA_ROWS:,} rows per file. "
        "UTF-8 (with or without BOM). Files are processed locally."
    )

    with st.expander("Templates and examples"):
        st.download_button(
            "Invoice template",
            template_csv(INVOICE_COLUMNS),
            "invoice_template.csv",
            "text/csv",
            width="stretch",
        )
        st.download_button(
            "Payment template",
            template_csv(PAYMENT_COLUMNS),
            "payment_template.csv",
            "text/csv",
            width="stretch",
        )
        st.download_button(
            "Demo invoices",
            demo_invoices(),
            "demo_invoices.csv",
            "text/csv",
            width="stretch",
        )
        st.download_button(
            "Demo payments",
            demo_payments(),
            "demo_payments.csv",
            "text/csv",
            width="stretch",
        )

    with connect(DB_PATH) as con:
        runs = reporting.list_runs(con)
    if not runs.empty:
        st.divider()
        ids = list(runs["run_id"])
        current = st.session_state.get("run_id")
        labels = {
            r.run_id: f"{r.run_id} | as of {pd.Timestamp(r.as_of_date).date()} | {r.invoice_label}"
            for r in runs.itertuples()
        }
        chosen = st.selectbox(
            "Stored runs",
            ids,
            index=ids.index(current) if current in ids else None,
            format_func=labels.get,
            placeholder="Open a previous run",
        )
        if chosen and chosen != current:
            st.session_state["run_id"] = chosen
            st.session_state.pop("export_zip", None)
            st.session_state.pop("run_notice", None)
            st.rerun()
    st.caption(f"Engine {ENGINE_VERSION} · rules {RULE_VERSION} · local database `{DB_PATH.name}`")


# ----------------------------------------------------------------------------- header

st.title("ReconcileKit")
st.markdown(
    "**Why don't the invoice register and the payment ledger agree, and which records need "
    "human review?** Exact-reference matching in integer cents, transparent quarantine of "
    "invalid rows, and an append-only review trail."
)

if errors := st.session_state.get("file_errors"):
    with st.container(border=True):
        st.error("The files could not be reconciled. No run was created.")
        for kind, messages in errors.items():
            for message in messages:
                st.text(f"[{kind}] {message}")  # may quote file content; no Markdown
        if previous := st.session_state.get("run_id"):
            st.caption(
                f"The dashboard below still shows the previously selected run `{previous}`, "
                "not the files you just submitted."
            )

run_id = st.session_state.get("run_id")
if not run_id:
    st.info(
        "Start with **Load bundled demo** in the sidebar, or upload an invoice register and "
        "a payment ledger and choose an as-of date."
    )
    col1, col2 = st.columns(2)
    with col1, st.container(border=True):
        st.subheader("Input contract")
        st.markdown(
            f"**Invoices:** `{', '.join(INVOICE_COLUMNS)}`  \n"
            f"**Payments:** `{', '.join(PAYMENT_COLUMNS)}`\n\n"
            "- IDs are text; leading zeros, case and punctuation are preserved. Only "
            "surrounding whitespace is trimmed.\n"
            "- Dates: `YYYY-MM-DD`. Currency: `USD` only.\n"
            "- Amounts: positive, plain decimals, at most two places (e.g. `1234.50`). "
            "Blank, zero, negative, exponent, or over-precise amounts are invalid.\n"
            "- Payment `invoice_id` may be blank (the payment is then unmatched).\n"
            "- Extra columns are ignored and listed in the validation report."
        )
    with col2, st.container(border=True):
        st.subheader("Deliberately out of scope (v1)")
        st.markdown(
            "Other currencies · refunds, credit notes and negative amounts · one payment "
            "split across invoices · fuzzy matching · matching by amount or date alone · "
            "PDFs/OCR · AI-generated decisions · authentication or multi-user workflow · "
            "cloud deployment and integrations."
        )
    st.stop()

with connect(DB_PATH) as con:
    run = reporting.get_run(con, run_id)
    if run is None:
        st.session_state.pop("run_id", None)
        st.warning("That run is no longer in the database.")
        st.stop()
    invoices = reporting.invoice_results(con, run_id)
    payments = reporting.payment_results(con, run_id)
    issues = reporting.validation_issues(con, run_id)
    exceptions = reporting.exceptions_with_disposition(con, run_id)

if notice := st.session_state.pop("run_notice", None):
    st.toast(notice)

complete = bool(run["is_complete"])
checks = reporting.invariant_checks(run)
invariants_ok = all(ok for _, ok, _ in checks)
open_count = int((exceptions["disposition"] != Disposition.RESOLVED_EXTERNALLY.value).sum())

st.caption(
    f"Run `{run_id}` · as of **{pd.Timestamp(run['as_of_date']).date()}** · "
    f"invoices `{run['invoice_label']}` (sha256 {run['invoice_sha256'][:12]}…) · "
    f"payments `{run['payment_label']}` (sha256 {run['payment_sha256'][:12]}…) · "
    f"created {pd.Timestamp(run['created_at_utc']).isoformat()} UTC"
)

ind1, ind2, ind3 = st.columns(3)
with ind1, st.container(border=True):
    st.markdown("**Arithmetic**")
    if invariants_ok:
        st.badge("Invariants verified", icon=":material/check:", color="green")
        st.caption("Totals reconcile exactly in cents. This says nothing about data quality.")
    else:
        st.badge("Invariant failure", icon=":material/error:", color="red")
with ind2, st.container(border=True):
    st.markdown("**Data completeness**")
    if complete:
        st.badge("Complete", icon=":material/check:", color="green")
        st.caption("Every source row was eligible.")
    else:
        q = run["quarantined_invoice_rows"] + run["quarantined_payment_rows"]
        st.badge(f"Incomplete: {q} rows quarantined", icon=":material/warning:", color="orange")
        st.caption("Totals cover eligible rows only.")
with ind3, st.container(border=True):
    st.markdown("**Exceptions**")
    color = "gray" if open_count == 0 else "orange"
    st.badge(f"{open_count} not resolved of {len(exceptions)}", color=color)
    st.caption("Review dispositions never change totals.")

if not complete:
    st.warning(
        f"**Incomplete run.** {run['quarantined_invoice_rows']} invoice row(s) and "
        f"{run['quarantined_payment_rows']} payment row(s) were quarantined. The totals below "
        "describe valid records only and do **not** represent the entire uploaded dataset. "
        "See the Validation tab. Correcting the source files and importing them again "
        "creates a new run."
    )

tab_overview, tab_validation, tab_results, tab_review, tab_export, tab_about = st.tabs(
    ["Overview", "Validation", "Results", "Review exceptions", "Export", "Rules & scope"]
)

# ----------------------------------------------------------------------------- overview

with tab_overview:
    m = st.columns(5)
    m[0].metric(
        "Eligible invoices (I)",
        format_usd(run["eligible_invoice_cents"]),
        help=f"{run['eligible_invoices']} invoices",
        border=True,
    )
    m[1].metric(
        "Eligible payments (P)",
        format_usd(run["eligible_payment_cents"]),
        help=f"{run['eligible_payments']} payments",
        border=True,
    )
    m[2].metric("Applied payments (A)", format_usd(run["applied_payment_cents"]), border=True)
    m[3].metric(
        "Outstanding (O)",
        format_usd(run["outstanding_cents"]),
        help=f"Overdue: {format_usd(run['overdue_cents'])} across "
        f"{run['overdue_invoices']} invoice(s)",
        border=True,
    )
    m[4].metric("Overpayments (X)", format_usd(run["overpayment_cents"]), border=True)
    m = st.columns(5)
    m[0].metric("Unmatched payments (U)", format_usd(run["unmatched_payment_cents"]), border=True)
    m[1].metric("Ambiguous payments (M)", format_usd(run["ambiguous_payment_cents"]), border=True)
    m[2].metric("Blocked payments (B)", format_usd(run["blocked_payment_cents"]), border=True)
    m[3].metric("Overdue outstanding", format_usd(run["overdue_cents"]), border=True)
    m[4].metric(
        "Excluded rows",
        f"{run['quarantined_invoice_rows']} inv · {run['quarantined_payment_rows']} pay",
        help="Quarantined rows are outside every monetary total. Unparseable amounts are "
        "never summed.",
        border=True,
    )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Reconciliation bridge")
        bridge = pd.DataFrame(
            [
                {
                    "": b.sign,
                    "Line": b.label,
                    "Symbol": b.symbol,
                    "Amount": format_usd(b.cents),
                    "Meaning": b.note,
                }
                for b in reporting.bridge(run)
            ]
        )
        st.table(bridge, hide_index=True)
        st.caption(
            "Outstanding balances and overpayments offset each other in the net difference. "
            "A small or zero net difference does **not** mean every invoice is settled."
        )
    with right:
        st.subheader("Invariant checks")
        for name, ok, detail in checks:
            st.markdown(f"{'✅' if ok else '❌'} **{name}**  \n`{detail}`")
        st.subheader("Status counts")
        counts = pd.DataFrame(
            [
                {"Record": "Invoice", "Status": k, "Count": v}
                for k, v in run["invoice_status_counts"].items()
            ]
            + [
                {"Record": "Payment", "Status": k, "Count": v}
                for k, v in run["payment_status_counts"].items()
            ]
        )
        st.table(counts, hide_index=True)

# ----------------------------------------------------------------------------- validation

with tab_validation:
    st.subheader("Row counts")
    rc = pd.DataFrame(
        [
            {
                "File": run["invoice_label"],
                "Kind": "Invoices",
                "Rows read": run["invoice_rows"],
                "Eligible": run["eligible_invoices"],
                "Quarantined": run["quarantined_invoice_rows"],
                "Ignored extra columns": ", ".join(run["invoice_extra_columns"]) or "none",
            },
            {
                "File": run["payment_label"],
                "Kind": "Payments",
                "Rows read": run["payment_rows"],
                "Eligible": run["eligible_payments"],
                "Quarantined": run["quarantined_payment_rows"],
                "Ignored extra columns": ", ".join(run["payment_extra_columns"]) or "none",
            },
        ]
    )
    st.dataframe(escape_frame(rc), hide_index=True, width="stretch")
    st.caption(
        "File-level problems (encoding, headers, malformed CSV) block a run entirely and are "
        "shown above the tabs when they occur. Row-level problems quarantine only the "
        "affected rows, listed here."
    )
    st.subheader("Quarantined rows")
    if issues.empty:
        st.success("No rows were quarantined.")
    else:
        st.dataframe(
            escape_frame(issues.rename(columns={"kind": "file", "source_row": "row"})),
            hide_index=True,
            width="stretch",
        )
    st.download_button(
        "Download validation report (CSV)",
        validation_report_csv(DB_PATH, run_id),
        f"validation_{run_id}.csv",
        "text/csv",
    )

# ----------------------------------------------------------------------------- results

with tab_results:
    st.subheader("Eligible invoices")
    st.dataframe(
        escape_frame(
            money(
                invoices[
                    [
                        "source_row",
                        "invoice_id",
                        "customer_id",
                        "invoice_date",
                        "due_date",
                        "invoice_amount_cents",
                        "applied_payment_cents",
                        "outstanding_cents",
                        "overpayment_cents",
                        "status",
                        "is_overdue",
                        "days_overdue",
                        "normalization_applied",
                        "explanation",
                    ]
                ],
                {
                    "invoice_amount_cents": "invoice",
                    "applied_payment_cents": "applied",
                    "outstanding_cents": "outstanding",
                    "overpayment_cents": "overpaid",
                },
            )
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "invoice_date": st.column_config.DateColumn(format="YYYY-MM-DD"),
            "due_date": st.column_config.DateColumn(format="YYYY-MM-DD"),
        },
    )
    st.subheader("Eligible payments")
    st.dataframe(
        escape_frame(
            money(
                payments[
                    [
                        "source_row",
                        "payment_id",
                        "invoice_id_raw",
                        "invoice_id",
                        "customer_id",
                        "payment_date",
                        "payment_amount_cents",
                        "status",
                        "rule_code",
                        "warnings",
                        "normalization_applied",
                        "explanation",
                    ]
                ],
                {"payment_amount_cents": "amount"},
            )
        ),
        hide_index=True,
        width="stretch",
        column_config={"payment_date": st.column_config.DateColumn(format="YYYY-MM-DD")},
    )

# ----------------------------------------------------------------------------- review

with tab_review:
    st.caption(
        "Automated findings are immutable. A review adds a separate, timestamped event; it "
        "never reallocates payments, edits source data, or changes totals. Reviewer labels "
        "are self-reported (this is a local single-user tool, not an authenticated system)."
    )
    st.caption(
        "In on-screen grids (and their CSV downloads), text starting with a formula character "
        "such as = + - @ is shown with a leading ' to stop spreadsheet formulas. Stored values "
        "are unchanged."
    )
    if exceptions.empty:
        st.success("No exceptions in this run.")
    else:
        f1, f2, f3 = st.columns(3)
        types = f1.multiselect("Exception type", sorted(exceptions["rule_code"].unique()))
        classes = f2.multiselect(
            "Automated classification", sorted(exceptions["classification"].unique())
        )
        dispositions = f3.multiselect("Review disposition", [d.value for d in Disposition])
        f4, f5 = st.columns(2)
        customers = sorted(c for c in exceptions["customer_id"].dropna().unique())
        customer = f4.selectbox("Customer", customers, index=None, placeholder="Any customer")
        id_query = f5.text_input("Invoice or payment ID contains").strip()

        view = exceptions
        if types:
            view = view[view["rule_code"].isin(types)]
        if classes:
            view = view[view["classification"].isin(classes)]
        if dispositions:
            view = view[view["disposition"].isin(dispositions)]
        if customer:
            view = view[view["customer_id"] == customer]
        if id_query:
            mask = view["invoice_id"].fillna("").str.contains(id_query, regex=False) | view[
                "payment_id"
            ].fillna("").str.contains(id_query, regex=False)
            view = view[mask]

        table = money(
            view[
                [
                    "exception_id",
                    "category",
                    "rule_code",
                    "classification",
                    "disposition",
                    "customer_id",
                    "invoice_id",
                    "payment_id",
                    "amount_cents",
                    "source_refs",
                ]
            ],
            {"amount_cents": "amount"},
        )
        st.caption(f"{len(view)} of {len(exceptions)} exceptions. Select a row for details.")
        event = st.dataframe(
            escape_frame(table),
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="exception_table",
        )
        selected = event.selection.rows
        if not selected:
            st.info("Select an exception above to see its evidence and review history.")
        else:
            exc = view.iloc[selected[0]]
            exc_id = exc["exception_id"]
            with st.container(border=True):
                st.subheader(f"{exc_id} · {exc['rule_code']}")
                st.text(exc["explanation"])  # embeds uploaded IDs; never render as Markdown
                st.markdown(
                    f"Automated classification **{exc['classification']}** · severity "
                    f"**{exc['severity']}** · current disposition **{exc['disposition']}**"
                    + (
                        f" · amount **{format_usd(int(exc['amount_cents']))}**"
                        if not pd.isna(exc["amount_cents"])
                        else ""
                    )
                )
                ev, hist = st.columns(2)
                with ev:
                    st.markdown("**Source evidence** (values as read)")
                    with connect(DB_PATH) as con:
                        evidence = reporting.source_rows(con, run_id, list(exc["record_ids"]))
                    if evidence.empty:
                        st.caption("No source rows linked.")
                    else:
                        raw = pd.DataFrame(list(evidence["raw"]))
                        raw.insert(0, "row", list(evidence["source_row"]))
                        raw.insert(0, "file", list(evidence["kind"]))
                        st.dataframe(escape_frame(raw), hide_index=True, width="stretch")
                with hist:
                    st.markdown("**Review history** (oldest first)")
                    with connect(DB_PATH) as con:
                        history = reporting.review_history(con, run_id, exc_id)
                    if history.empty:
                        st.caption("No review events yet. Current disposition: OPEN.")
                    else:
                        st.dataframe(
                            escape_frame(
                                history[["created_at_utc", "reviewer", "disposition", "note"]]
                            ),
                            hide_index=True,
                            width="stretch",
                        )

                # A fresh event id per form instance makes resubmission idempotent.
                key = f"event_id::{run_id}::{exc_id}"
                st.session_state.setdefault(key, str(uuid.uuid4()))
                with st.form(f"review::{exc_id}", clear_on_submit=True):
                    st.markdown("**Add review event**")
                    c1, c2 = st.columns([1, 1])
                    reviewer = c1.text_input("Reviewer label", max_chars=MAX_REVIEWER_LENGTH)
                    disposition = c2.selectbox(
                        "Disposition", [d.value for d in Disposition], index=1
                    )
                    note = st.text_area(
                        "Note (required for RESOLVED_EXTERNALLY)", max_chars=MAX_NOTE_LENGTH
                    )
                    if st.form_submit_button("Append review event"):
                        try:
                            outcome = add_review_event(
                                DB_PATH,
                                run_id,
                                exc_id,
                                reviewer,
                                disposition,
                                note,
                                event_id=st.session_state[key],
                            )
                        except ReviewConflictError as err:
                            # Nothing was saved. Issue a fresh event id so a deliberate
                            # resubmission is recorded as a new event.
                            st.session_state[key] = str(uuid.uuid4())
                            st.error(
                                f"{err} Nothing was saved; check the history, then submit again."
                            )
                        except ReviewError as err:
                            st.error(str(err))
                        else:
                            st.session_state[key] = str(uuid.uuid4())
                            st.session_state.pop("export_zip", None)
                            st.toast(
                                f"Review event recorded for {exc_id}."
                                if outcome.inserted
                                else f"This review event for {exc_id} was already recorded."
                            )
                            st.rerun()

# ----------------------------------------------------------------------------- export

with tab_export:
    st.markdown(
        "The package contains `summary.json`, `invoice_results.csv`, `payment_results.csv`, "
        "`exceptions.csv`, `validation_errors.csv`, `review_history.csv`, `manifest.json` and "
        "`README.txt`. It reflects the immutable findings plus the review history at the "
        "moment you prepare it. Source files are not included; their SHA-256 hashes are in "
        "the manifest. Text cells starting with `= + - @` are prefixed with `'` to prevent "
        "spreadsheet formula execution."
    )
    if st.button("Prepare reconciliation package"):
        st.session_state["export_zip"] = (run_id, build_export_package(DB_PATH, run_id))
    prepared = st.session_state.get("export_zip")
    if prepared and prepared[0] == run_id:
        st.download_button(
            "Download ZIP",
            prepared[1],
            export_filename(run_id),
            "application/zip",
            type="primary",
        )

# ----------------------------------------------------------------------------- about

with tab_about:
    st.markdown(
        """
**Matching.** A payment is applied only when its trimmed `customer_id` and `invoice_id`
exactly equal one eligible invoice's identity and the currency is USD. Amounts and dates are
never used to infer a match. Tolerance is zero cents.

**Payment decision order** (first rule wins):
1. Blank reference → `UNMATCHED` (`PAYMENT_REFERENCE_MISSING`)
2. Identity appears on more than one invoice row → `AMBIGUOUS` (`AMBIGUOUS_INVOICE_REFERENCE`)
3. Identity appears on one quarantined invoice row → `BLOCKED` (`REFERENCED_INVOICE_INVALID`)
4. Identity appears on one eligible invoice row → `APPLIED` (`EXACT_REFERENCE_MATCH`)
5. Invoice ID exists only for other customers → `UNMATCHED` (`CUSTOMER_REFERENCE_CONFLICT`)
6. Otherwise → `UNMATCHED` (`PAYMENT_REFERENCE_NOT_FOUND`)

**AMBIGUOUS vs BLOCKED.** Ambiguous: the reference cannot identify a single invoice because
the identity is duplicated. Blocked: the reference identifies exactly one invoice, but that
invoice is invalid. Both are held for review; neither is applied.

**Duplicates.** Invoice identity is `customer_id + invoice_id`; payment identity is
`payment_id`. Every row in a duplicate group is quarantined, including identical copies,
and groups are detected before other validation filters rows out.

**Snapshot.** Invoice and payment dates after the as-of date are quarantined. Future due
dates are allowed. An invoice is overdue only if its due date is before the as-of date and
it has a positive outstanding balance.

**Out of scope in v1:** other currencies, refunds/credits/negative amounts, splitting one
payment across invoices, fuzzy or amount/date-based matching, PDFs/OCR, AI decisions,
authentication or multi-user workflow, cloud deployment and integrations.
"""
    )
