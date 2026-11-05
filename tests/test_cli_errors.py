"""CLI behaviour on unexpected internal errors (exit code 1)."""

from __future__ import annotations

import pytest

from reconcilekit import cli


@pytest.fixture
def broken_engine(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated engine fault")

    monkeypatch.setattr(cli, "reconcile_files", boom)


def test_internal_error_exits_one_with_hint_and_no_traceback(
    broken_engine, tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv(cli.DEBUG_ENV, raising=False)
    code = cli.main(["demo", "--db", str(tmp_path / "x.duckdb")])
    err = capsys.readouterr().err
    assert code == cli.EXIT_INTERNAL
    assert "Internal error: RuntimeError: simulated engine fault" in err
    assert f"Set {cli.DEBUG_ENV}=1" in err
    assert "Traceback" not in err
    assert not (tmp_path / "x.duckdb").exists()


@pytest.mark.parametrize("value", ["1", "true"])
def test_debug_env_prints_traceback(broken_engine, tmp_path, capsys, monkeypatch, value):
    monkeypatch.setenv(cli.DEBUG_ENV, value)
    code = cli.main(["demo", "--db", str(tmp_path / "x.duckdb")])
    err = capsys.readouterr().err
    assert code == cli.EXIT_INTERNAL
    assert "Traceback (most recent call last)" in err
    assert "simulated engine fault" in err


def test_debug_env_zero_means_off(broken_engine, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(cli.DEBUG_ENV, "0")
    assert cli.main(["demo", "--db", str(tmp_path / "x.duckdb")]) == cli.EXIT_INTERNAL
    assert "Traceback" not in capsys.readouterr().err
