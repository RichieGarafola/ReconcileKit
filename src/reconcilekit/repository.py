"""Local DuckDB persistence for runs, immutable findings, and append-only reviews.

Writes are limited to two operations:

* ``save_run`` inserts a complete run atomically, or reuses the existing run when
  the deterministic fingerprint is already stored (byte-identical reimport).
* ``add_review_event`` appends one review event. Nothing updates or deletes
  findings or review events.

All SQL is parameterized.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from reconcilekit.models import Disposition
from reconcilekit.reconciliation import ReconciliationRun, canonical_json
from reconcilekit.reporting_views import install_reporting_views

SCHEMA_VERSION = 1
MAX_REVIEWER_LENGTH = 80
MAX_NOTE_LENGTH = 2000

_DDL = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY,
    fingerprint VARCHAR NOT NULL UNIQUE,
    created_at_utc TIMESTAMP NOT NULL,
    as_of_date DATE NOT NULL,
    engine_version VARCHAR NOT NULL,
    rule_version VARCHAR NOT NULL,
    config_json VARCHAR NOT NULL,
    invoice_label VARCHAR NOT NULL,
    invoice_sha256 VARCHAR NOT NULL,
    invoice_rows INTEGER NOT NULL,
    invoice_extra_columns VARCHAR[] NOT NULL,
    payment_label VARCHAR NOT NULL,
    payment_sha256 VARCHAR NOT NULL,
    payment_rows INTEGER NOT NULL,
    payment_extra_columns VARCHAR[] NOT NULL,
    eligible_invoices INTEGER NOT NULL,
    eligible_payments INTEGER NOT NULL,
    quarantined_invoice_rows INTEGER NOT NULL,
    quarantined_payment_rows INTEGER NOT NULL,
    is_complete BOOLEAN NOT NULL,
    exception_count INTEGER NOT NULL,
    overdue_invoices INTEGER NOT NULL,
    eligible_invoice_cents BIGINT NOT NULL,
    applied_payment_cents BIGINT NOT NULL,
    outstanding_cents BIGINT NOT NULL,
    overpayment_cents BIGINT NOT NULL,
    eligible_payment_cents BIGINT NOT NULL,
    unmatched_payment_cents BIGINT NOT NULL,
    ambiguous_payment_cents BIGINT NOT NULL,
    blocked_payment_cents BIGINT NOT NULL,
    overdue_cents BIGINT NOT NULL,
    invoice_status_counts_json VARCHAR NOT NULL,
    payment_status_counts_json VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS source_rows (
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    record_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    source_row INTEGER NOT NULL,
    raw_json VARCHAR NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS validation_issues (
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    issue_seq INTEGER NOT NULL,
    record_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    source_row INTEGER NOT NULL,
    rule_code VARCHAR NOT NULL,
    field VARCHAR,
    raw_value VARCHAR,
    message VARCHAR NOT NULL,
    PRIMARY KEY (run_id, issue_seq)
);

CREATE TABLE IF NOT EXISTS invoice_results (
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    record_id VARCHAR NOT NULL,
    source_row INTEGER NOT NULL,
    invoice_id_raw VARCHAR NOT NULL,
    invoice_id VARCHAR NOT NULL,
    customer_id_raw VARCHAR NOT NULL,
    customer_id VARCHAR NOT NULL,
    invoice_date DATE NOT NULL,
    due_date DATE NOT NULL,
    invoice_amount_cents BIGINT NOT NULL,
    applied_payment_cents BIGINT NOT NULL,
    outstanding_cents BIGINT NOT NULL,
    overpayment_cents BIGINT NOT NULL,
    status VARCHAR NOT NULL,
    rule_code VARCHAR NOT NULL,
    is_overdue BOOLEAN NOT NULL,
    days_overdue INTEGER NOT NULL,
    payment_record_ids VARCHAR[] NOT NULL,
    normalization_applied BOOLEAN NOT NULL,
    explanation VARCHAR NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS payment_results (
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    record_id VARCHAR NOT NULL,
    source_row INTEGER NOT NULL,
    payment_id_raw VARCHAR NOT NULL,
    payment_id VARCHAR NOT NULL,
    invoice_id_raw VARCHAR NOT NULL,
    invoice_id VARCHAR,
    customer_id_raw VARCHAR NOT NULL,
    customer_id VARCHAR NOT NULL,
    payment_date DATE NOT NULL,
    payment_amount_cents BIGINT NOT NULL,
    status VARCHAR NOT NULL,
    rule_code VARCHAR NOT NULL,
    matched_invoice_record_id VARCHAR,
    related_invoice_record_ids VARCHAR[] NOT NULL,
    warnings VARCHAR[] NOT NULL,
    normalization_applied BOOLEAN NOT NULL,
    explanation VARCHAR NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    exception_id VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    rule_code VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    classification VARCHAR NOT NULL,
    customer_id VARCHAR,
    invoice_id VARCHAR,
    payment_id VARCHAR,
    record_ids VARCHAR[] NOT NULL,
    source_refs VARCHAR NOT NULL,
    amount_cents BIGINT,
    explanation VARCHAR NOT NULL,
    PRIMARY KEY (run_id, exception_id)
);

CREATE SEQUENCE IF NOT EXISTS review_event_seq START 1;

CREATE TABLE IF NOT EXISTS review_events (
    event_id VARCHAR PRIMARY KEY,
    seq BIGINT NOT NULL DEFAULT nextval('review_event_seq'),
    run_id VARCHAR NOT NULL REFERENCES runs (run_id),
    exception_id VARCHAR NOT NULL,
    reviewer VARCHAR NOT NULL,
    disposition VARCHAR NOT NULL,
    note VARCHAR NOT NULL,
    created_at_utc TIMESTAMP NOT NULL
);
"""


class ReviewError(ValueError):
    """A review submission that cannot be accepted (user-facing message)."""


class ReviewConflictError(ReviewError):
    """An existing event_id was resubmitted with a different logical payload."""


@dataclass(frozen=True)
class SaveOutcome:
    run_id: str
    created: bool  # False when an identical logical run already existed


@dataclass(frozen=True)
class ReviewOutcome:
    event_id: str
    inserted: bool  # False when this event_id was already recorded (idempotent replay)


def utc_now() -> datetime:
    """Current UTC time as a naive datetime (stored in *_utc TIMESTAMP columns)."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


@contextmanager
def connect(db_path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the database, creating the schema and rk_v1 reporting views if needed."""
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(_DDL)
        if con.execute("SELECT count(*) FROM schema_info").fetchone()[0] == 0:
            con.execute("INSERT INTO schema_info VALUES (?)", [SCHEMA_VERSION])
        install_reporting_views(con)
        yield con
    finally:
        con.close()


def _insert_many(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    if rows:
        placeholders = ", ".join("?" for _ in rows[0])
        con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def save_run(db_path: str | Path, run: ReconciliationRun) -> SaveOutcome:
    """Persist a run atomically, or reuse the stored run with the same fingerprint."""
    with connect(db_path) as con:
        con.begin()
        try:
            existing = con.execute(
                "SELECT run_id FROM runs WHERE fingerprint = ?", [run.fingerprint]
            ).fetchone()
            if existing:
                con.rollback()
                return SaveOutcome(existing[0], created=False)
            _write_run(con, run)
            con.commit()
        except duckdb.ConstraintException:
            con.rollback()
            # Only a concurrent writer storing the same fingerprint is benign; any other
            # constraint failure is a real defect and must surface.
            stored = con.execute(
                "SELECT run_id FROM runs WHERE fingerprint = ?", [run.fingerprint]
            ).fetchone()
            if stored:
                return SaveOutcome(stored[0], created=False)
            raise
        except BaseException:
            con.rollback()
            raise
    return SaveOutcome(run.run_id, created=True)


def _write_run(con: duckdb.DuckDBPyConnection, run: ReconciliationRun) -> None:
    s = run.summary
    rid = run.run_id
    con.execute(
        "INSERT INTO runs VALUES (" + ", ".join("?" for _ in range(33)) + ")",
        [
            rid,
            run.fingerprint,
            utc_now(),
            run.as_of,
            run.engine_version,
            run.rule_version,
            canonical_json(run.config),
            run.invoice_source.label,
            run.invoice_source.sha256,
            run.invoice_source.row_count,
            list(run.invoice_source.extra_columns),
            run.payment_source.label,
            run.payment_source.sha256,
            run.payment_source.row_count,
            list(run.payment_source.extra_columns),
            s.eligible_invoices,
            s.eligible_payments,
            s.quarantined_invoice_rows,
            s.quarantined_payment_rows,
            s.is_complete,
            s.exception_count,
            s.overdue_invoices,
            s.eligible_invoice_cents,
            s.applied_payment_cents,
            s.outstanding_cents,
            s.overpayment_cents,
            s.eligible_payment_cents,
            s.unmatched_payment_cents,
            s.ambiguous_payment_cents,
            s.blocked_payment_cents,
            s.overdue_cents,
            canonical_json(s.invoice_status_counts),
            canonical_json(s.payment_status_counts),
        ],
    )
    _insert_many(
        con,
        "source_rows",
        [
            (rid, r.record_id, r.kind, r.source_row, json.dumps(r.raw, ensure_ascii=False))
            for r in run.source_rows
        ],
    )
    _insert_many(
        con,
        "validation_issues",
        [
            (
                rid,
                n,
                i.record_id,
                i.kind,
                i.source_row,
                i.rule_code.value,
                i.field,
                i.raw_value,
                i.message,
            )
            for n, i in enumerate(run.validation_issues, start=1)
        ],
    )
    _insert_many(
        con,
        "invoice_results",
        [
            (
                rid,
                r.record_id,
                r.source_row,
                r.invoice_id_raw,
                r.invoice_id,
                r.customer_id_raw,
                r.customer_id,
                r.invoice_date,
                r.due_date,
                r.invoice_amount_cents,
                r.applied_payment_cents,
                r.outstanding_cents,
                r.overpayment_cents,
                r.status.value,
                r.rule_code.value,
                r.is_overdue,
                r.days_overdue,
                list(r.payment_record_ids),
                r.normalization_applied,
                r.explanation,
            )
            for r in run.invoice_results
        ],
    )
    _insert_many(
        con,
        "payment_results",
        [
            (
                rid,
                r.record_id,
                r.source_row,
                r.payment_id_raw,
                r.payment_id,
                r.invoice_id_raw,
                r.invoice_id,
                r.customer_id_raw,
                r.customer_id,
                r.payment_date,
                r.payment_amount_cents,
                r.status.value,
                r.rule_code.value,
                r.matched_invoice_record_id,
                list(r.related_invoice_record_ids),
                [w.value for w in r.warnings],
                r.normalization_applied,
                r.explanation,
            )
            for r in run.payment_results
        ],
    )
    _insert_many(
        con,
        "exceptions",
        [
            (
                rid,
                e.exception_id,
                e.category.value,
                e.rule_code.value,
                e.severity.value,
                e.classification,
                e.customer_id,
                e.invoice_id,
                e.payment_id,
                list(e.record_ids),
                e.source_refs,
                e.amount_cents,
                e.explanation,
            )
            for e in run.exceptions
        ],
    )


def add_review_event(
    db_path: str | Path,
    run_id: str,
    exception_id: str,
    reviewer: str,
    disposition: Disposition | str,
    note: str = "",
    event_id: str | None = None,
    created_at_utc: datetime | None = None,
) -> ReviewOutcome:
    """Append one review event, idempotently by ``event_id``.

    * New ``event_id``: the event is appended (even if its content repeats an earlier one).
    * Known ``event_id`` with the same logical payload: no-op, ``inserted=False``.
    * Known ``event_id`` with a different payload: ``ReviewConflictError``; nothing changes.

    The reviewer label is self-reported text, not an authenticated identity.
    """
    reviewer = reviewer.strip()
    note = note.strip()
    try:
        disposition = Disposition(disposition)
    except ValueError as exc:
        raise ReviewError(f"Unknown disposition {disposition!r}.") from exc
    if not reviewer:
        raise ReviewError("A reviewer label is required.")
    if len(reviewer) > MAX_REVIEWER_LENGTH:
        raise ReviewError(f"Reviewer label must be at most {MAX_REVIEWER_LENGTH} characters.")
    if len(note) > MAX_NOTE_LENGTH:
        raise ReviewError(f"Note must be at most {MAX_NOTE_LENGTH} characters.")
    if disposition is Disposition.RESOLVED_EXTERNALLY and not note:
        raise ReviewError("A note is required when marking an exception RESOLVED_EXTERNALLY.")
    event_id = event_id or str(uuid.uuid4())

    with connect(db_path) as con:
        con.begin()
        try:
            stored = con.execute(
                "SELECT run_id, exception_id, reviewer, disposition, note "
                "FROM review_events WHERE event_id = ?",
                [event_id],
            ).fetchone()
            if stored:
                # Logical payload = (run, exception, reviewer, disposition, note), compared
                # after the normalization above. created_at_utc is deliberately excluded:
                # a retry naturally carries a newer timestamp.
                payload = (run_id, exception_id, reviewer, disposition.value, note)
                if tuple(stored) != payload:
                    raise ReviewConflictError(
                        f"Review event {event_id!r} was already recorded with different "
                        "content; the retry was rejected and stored history is unchanged."
                    )  # the outer handler rolls back
                con.rollback()
                return ReviewOutcome(event_id, inserted=False)
            if not con.execute(
                "SELECT 1 FROM exceptions WHERE run_id = ? AND exception_id = ?",
                [run_id, exception_id],
            ).fetchone():
                raise ReviewError(f"Exception {exception_id!r} does not exist in run {run_id!r}.")
            con.execute(
                "INSERT INTO review_events "
                "(event_id, run_id, exception_id, reviewer, disposition, note, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    event_id,
                    run_id,
                    exception_id,
                    reviewer,
                    disposition.value,
                    note,
                    created_at_utc or utc_now(),
                ],
            )
            con.commit()
        except BaseException:
            con.rollback()
            raise
    return ReviewOutcome(event_id, inserted=True)
