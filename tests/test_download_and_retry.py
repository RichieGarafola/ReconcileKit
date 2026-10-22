"""Table-download escaping in the UI and conflicting review-event retries."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from conftest import inv, pay, run_rows
from reconcilekit import reporting
from reconcilekit.exports import escape_frame
from reconcilekit.repository import (
    ReviewConflictError,
    add_review_event,
    connect,
    save_run,
)

APP = Path(__file__).resolve().parents[1] / "app.py"


# ------------------------------------------------------------------ table downloads


def test_every_app_dataframe_receives_an_escaped_copy():
    """st.dataframe always offers a CSV download, so its data must be escaped.

    Static st.table elements have no download control and may show raw values.
    """
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dataframe"
    ]
    assert calls, "expected interactive tables in app.py"
    # st.table renders cells as Markdown, so it is reserved for app-generated content.
    tables = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "table"
    ]
    assert {getattr(t.args[0], "id", None) for t in tables} <= {"bridge", "counts"}
    for call in calls:
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "escape_frame", (
            f"st.dataframe at line {call.lineno} must wrap its data in escape_frame(...)"
        )


def test_escape_frame_escapes_text_only_and_leaves_input_unchanged():
    raw = pd.DataFrame(
        {
            "payment_id": ['=HYPERLINK("http://x")', "PAY-1", "\t@cmd"],
            "note": ["+1+1", "-2", "fine"],
            "source_row": [2, 3, 4],
            "amount_cents": pd.array([100, None, 5], dtype="Int64"),
        }
    )
    before = raw.copy()
    shown = escape_frame(raw)
    assert list(shown["payment_id"]) == ['\'=HYPERLINK("http://x")', "PAY-1", "'\t@cmd"]
    assert list(shown["note"]) == ["'+1+1", "'-2", "fine"]
    assert shown["source_row"].tolist() == [2, 3, 4]
    assert str(shown["amount_cents"].dtype) == "Int64"
    pd.testing.assert_frame_equal(raw, before)


# ------------------------------------------------------------------ review retries


def _setup(db_path):
    run = run_rows([inv("=1+1")], [pay("@P1", invoice_id="")])
    run_id = save_run(db_path, run).run_id
    return run_id, run.exceptions[0].exception_id


def _history(db_path, run_id):
    with connect(db_path) as con:
        return reporting.review_history(con, run_id)


def test_identical_retry_is_a_noop_even_with_whitespace_differences(db_path):
    run_id, exc = _setup(db_path)
    first = add_review_event(db_path, run_id, exc, "ana", "INVESTIGATING", "call", event_id="e1")
    retry = add_review_event(
        db_path, run_id, exc, "  ana ", "INVESTIGATING", " call ", event_id="e1"
    )
    assert first.inserted and not retry.inserted
    assert len(_history(db_path, run_id)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"reviewer": "ben"},
        {"disposition": "ACKNOWLEDGED"},
        {"note": "different"},
    ],
)
def test_conflicting_retry_is_rejected_and_history_unchanged(db_path, change):
    run_id, exc = _setup(db_path)
    args = {"reviewer": "ana", "disposition": "INVESTIGATING", "note": "call"}
    add_review_event(db_path, run_id, exc, **args, event_id="e1")
    before = _history(db_path, run_id)
    with pytest.raises(ReviewConflictError, match="different content"):
        add_review_event(db_path, run_id, exc, **{**args, **change}, event_id="e1")
    after = _history(db_path, run_id)
    pd.testing.assert_frame_equal(before, after)


def test_conflicting_retry_against_other_exception_is_rejected(db_path):
    run = run_rows([inv()], [pay("P1", invoice_id=""), pay("P2", invoice_id="")])
    run_id = save_run(db_path, run).run_id
    a, b = (e.exception_id for e in run.exceptions[:2])
    add_review_event(db_path, run_id, a, "ana", "ACKNOWLEDGED", event_id="e1")
    with pytest.raises(ReviewConflictError):
        add_review_event(db_path, run_id, b, "ana", "ACKNOWLEDGED", event_id="e1")
    assert len(_history(db_path, run_id)) == 1


def test_new_event_id_with_identical_payload_is_a_legitimate_later_event(db_path):
    run_id, exc = _setup(db_path)
    add_review_event(db_path, run_id, exc, "ana", "ACKNOWLEDGED", "same", event_id="e1")
    later = add_review_event(db_path, run_id, exc, "ana", "ACKNOWLEDGED", "same", event_id="e2")
    assert later.inserted
    assert list(_history(db_path, run_id)["event_id"]) == ["e1", "e2"]
