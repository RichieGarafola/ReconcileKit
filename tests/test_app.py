"""Headless smoke test of the Streamlit UI using Streamlit's AppTest harness."""

from __future__ import annotations

from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_app_demo_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONCILEKIT_DB", str(tmp_path / "ui.duckdb"))
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert any("Load bundled demo" in i.value for i in at.info)

    at.sidebar.button[0].click().run()
    assert not at.exception
    assert any("Incomplete run" in w.value for w in at.warning)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Eligible invoices (I)"] == "$7,585.75"
    assert metrics["Eligible payments (P)"] == "$8,920.75"
    assert metrics["Blocked payments (B)"] == "$200.00"

    # Rerunning (as Streamlit does on any interaction) must not create another run.
    at.run()
    assert not at.exception
    from reconcilekit.repository import connect

    with connect(tmp_path / "ui.duckdb") as con:
        assert con.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM review_events").fetchone()[0] == 0


def test_grid_data_offered_for_download_is_formula_escaped(tmp_path, monkeypatch):
    """The CSV download of st.dataframe serializes the data sent to the browser."""
    from conftest import inv, pay, run_rows
    from reconcilekit.exports import FORMULA_PREFIXES
    from reconcilekit.repository import add_review_event, save_run

    db = tmp_path / "mal.duckdb"
    run = run_rows(
        [inv('=HYPERLINK("http://example.invalid")', "+CUST"), inv("INV-2", "@C", amount="x")],
        [pay("@P1", "", "-CUST"), pay("P2", '=HYPERLINK("http://example.invalid")', "+CUST")],
    )
    run_id = save_run(db, run).run_id
    add_review_event(db, run_id, run.exceptions[-1].exception_id, "+r", "INVESTIGATING", "=1+1")
    monkeypatch.setenv("RECONCILEKIT_DB", str(db))

    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["run_id"] = run_id
    at.run()
    assert not at.exception
    assert len(at.dataframe) >= 4  # row counts, quarantined rows, results, exceptions
    seen = 0
    for grid in at.dataframe:
        for value in grid.value.astype(object).to_numpy().ravel():
            if isinstance(value, str) and value:
                seen += 1
                assert not value.lstrip().startswith(FORMULA_PREFIXES), value
    assert seen
    flat = " ".join(str(v) for g in at.dataframe for v in g.value.astype(object).to_numpy().ravel())
    assert "'=HYPERLINK" in flat and "'@P1" in flat and "'+CUST" in flat
