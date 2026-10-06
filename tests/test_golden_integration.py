"""E1-S6: the golden-path test itself — §8.2 through S2 -> S9 on PostgreSQL with the stub model."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from cdi_adapter.db import get_engine

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def _load():
    spec = importlib.util.spec_from_file_location(
        "golden_path", ROOT / "scripts" / "golden_path.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden_path"] = mod
    spec.loader.exec_module(mod)
    return mod


gp = _load()


GOLDEN_DB = "cdi_golden_test"


@pytest.fixture
def fresh(db_env, monkeypatch):
    """A dedicated database reset to head: the golden counts are defined on a fresh database."""
    monkeypatch.setenv("CDI_DATABASE_URL", db_env)  # restored after the test
    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "stub")
    return gp.prepare_fresh_db(GOLDEN_DB)


def test_golden_path_seeded_matches_the_stub_baseline(fresh):
    docs = gp.run_seeded()
    with get_engine().connect() as conn:
        measured = gp.measure(conn, docs)
    assert gp.compare(measured, gp.load_golden()["stub"]) == []
    assert measured["totals"] == {
        "facts": 21, "provenance": 21, "resources": 37, "bundles": 3, "structural_errors": 0
    }  # fmt: skip


def test_golden_run_detects_drift_on_a_real_run(fresh):
    """Same real run, a baseline off by one fact: the comparison must fail, not pass."""
    docs = gp.run_seeded()
    with get_engine().connect() as conn:
        measured = gp.measure(conn, docs)
    wrong = gp.load_golden()["stub"]
    wrong["documents"]["prescription_00"]["facts"] = 10
    assert gp.compare(measured, wrong) == ["prescription_00.facts: expected 10, got 9"]


def test_cli_seeded_exits_zero_and_writes_a_report(db_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CDI_DATABASE_URL", db_env)
    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "stub")
    report = tmp_path / "golden.json"
    alerts = tmp_path / "alerts.log"
    argv = ["--mode", "seeded", "--fresh-db", GOLDEN_DB, "--report", str(report)]
    rc = gp.main([*argv, "--alert-log", str(alerts)])
    assert rc == 0 and not alerts.exists()
    assert '"drift": []' in report.read_text(encoding="utf-8")


def test_fresh_db_refuses_the_configured_database(db_env, monkeypatch):
    monkeypatch.setenv("CDI_DATABASE_URL", db_env)
    with pytest.raises(SystemExit, match="refusing to reset"):
        gp.prepare_fresh_db("cdi_test")


def test_repeat_run_without_a_fresh_db_is_reported_as_drift(fresh):
    """Why --fresh-db exists: a second run finds Patient/Organization already stored."""
    gp.run_seeded()
    docs = gp.run_seeded()
    with get_engine().connect() as conn:
        drift = gp.compare(gp.measure(conn, docs), gp.load_golden()["stub"])
    assert "prescription_00.resources: expected 14, got 12" in drift
