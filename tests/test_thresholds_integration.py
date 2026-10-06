"""E4-S3: per-class gate thresholds derived from calibration data, on MOCK data (PostgreSQL).

A mock training version where each medication's strength is right only at scores >= 0.6 is
fitted (E4-S2), thresholds are derived per doc_type x fact_type x field x evidence state, measured
on held-out examples, written as the checked-in JSON config, and used by the gate only when
``CDI_GATE_THRESHOLDS`` names it. No production or clinician data is used."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.config import get_settings
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.training import calibrator, thresholds
from cdi_adapter.training.__main__ import main as cli
from cdi_adapter.validate.service import validate_document
from tests.test_calibrator_integration import MED, _mock_dataset
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


def _step_data(n: int = 8000) -> list[tuple]:
    """Mock: the strength is right exactly when the score is >= 0.6; other fields always."""
    rows = []
    for i in range(n):
        x = round((i % 1000) / 1000, 4)
        right = x >= 0.6
        rows.append(("prescription", "medication", x, MED,
                     MED if right else {**MED, "strength_num": 10.0},
                     "confirmed" if right else "corrected"))  # fmt: skip
    return rows


@pytest.fixture
def fitted_and_derived(db_env):
    with session_scope() as c:
        cal = calibrator.fit_calibrator(c, _mock_dataset(_step_data()))["version"]
        out = thresholds.derive_thresholds(c, cal, actor="test-derive")
    return cal, out


def _classes(version: int) -> dict[tuple[str, str], dict]:
    with session_scope() as c:
        return {(r["field"], r["evidence_state"]): dict(r) for r in c.execute(
            text("SELECT t.* FROM gate_threshold t JOIN gate_threshold_set s ON s.id = t.set_id "
                 "WHERE s.version = :v AND t.fact_type = 'medication'"), {"v": version},
        ).mappings()}  # fmt: skip


def test_thresholds_are_derived_per_class_and_meet_the_target_on_held_out_data(
        fitted_and_derived):  # fmt: skip
    cal, out = fitted_and_derived
    assert out["calibrator_version"] == cal and out["derived"] >= 1
    c = _classes(out["version"])
    strength = c[("strength", "single_engine")]
    assert strength["status"] == "derived" and strength["target"] == pytest.approx(0.005)
    assert strength["n_accepted_held_out"] >= 10
    assert strength["false_accept_held_out"] <= strength["target"]  # measured, not assumed
    assert strength["meets_target"] is True
    medicine = c[("medicine", "single_engine")]
    assert medicine["target"] == pytest.approx(0.02)  # a non-numeric field: the default target
    # evidence states the pipeline does not produce yet are never guessed
    assert c[("strength", "engines_disagree")]["status"] == "always_review"
    for state in ("engines_agree", "known_doctor_strong_exemplar", "unknown_doctor"):
        row = c[("strength", state)]
        assert row["status"] == "not_reachable" and row["threshold"] is None
    with session_scope() as s:
        basis = s.execute(text("SELECT target_basis, bound FROM gate_threshold_set "
                               "WHERE version = :v"), {"v": out["version"]}).one()  # fmt: skip
    assert "placeholder" in basis.target_basis and basis.bound.startswith("wilson-upper")


def test_a_derived_set_can_never_be_changed(fitted_and_derived):
    for sql in ("UPDATE gate_threshold SET threshold = 0.5 WHERE threshold IS NOT NULL",
                "DELETE FROM gate_threshold", "UPDATE gate_threshold_set SET target_default = 0.5",
                "DELETE FROM gate_threshold_set", "TRUNCATE gate_threshold"):  # fmt: skip
        with pytest.raises(IntegrityError, match="derived threshold set"):
            with get_engine().begin() as c:
                c.execute(text(sql))


@pytest.fixture
def gate_on(monkeypatch):
    def use(calibrator_version: int | None, path: str | None) -> None:
        for k, v in (("CDI_CALIBRATOR_VERSION", calibrator_version), ("CDI_GATE_THRESHOLDS", path)):
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
        calibrator.clear_cache()
        thresholds.clear_cache()

    yield use
    for k in ("CDI_CALIBRATOR_VERSION", "CDI_GATE_THRESHOLDS"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    calibrator.clear_cache()
    thresholds.clear_cache()


def test_the_derived_thresholds_replace_the_global_constants_at_the_gate(
        fitted_and_derived, gate_on, tmp_path):  # fmt: skip
    cal, out = fitted_and_derived
    with session_scope() as c:
        path = thresholds.write_config(c, out["version"], tmp_path / "gate_thresholds.json")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["schema"] == "gate_thresholds.v1" and cfg["calibrator_version"] == cal

    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    with session_scope() as c:  # scored 0.90: under today's global 0.95 audit line
        c.execute(text("UPDATE clinical_fact SET confidence_overall = 0.90 "
                       "WHERE :d = ANY(source_doc_ids) AND fact_type = 'medication'"),
                  {"d": doc})  # fmt: skip

    gate_on(None, None)  # the global constants: 0.90 is held
    validate_document(doc)
    with session_scope() as c:
        meds = [f for f in repo.get_facts_for_document(c, doc) if f["fact_type"] == "medication"]
    assert meds and {f["review_state"] for f in meds} == {"in_review"}

    gate_on(cal, str(path))  # the derived per-class cut-offs: 0.90 is reliably right here
    validate_document(doc)
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
    meds = [f for f in facts if f["fact_type"] == "medication"]
    assert {f["review_state"] for f in meds} == {"auto_accepted"}
    note = meds[0]["review_note"]["thresholds"]
    assert note["version"] == out["version"] and note["evidence_state"] == "single_engine"
    assert all(v["passed"] and v["status"] == "derived" for v in note["fields"].values())
    others = [f for f in facts if f["fact_type"] != "medication"]
    assert all(f["review_state"] == "in_review" for f in others)  # no validated class
    with session_scope() as c:
        policy = c.execute(text("SELECT policy_id FROM verified_fact WHERE fact_id = :f"),
                           {"f": str(meds[0]["id"])}).scalar_one()  # fmt: skip
    assert policy.endswith(f";calibrator=v{cal};thresholds=v{out['version']}")


def test_thresholds_without_their_calibrator_are_refused(fitted_and_derived, gate_on, tmp_path):
    _, out = fitted_and_derived
    with session_scope() as c:
        path = thresholds.write_config(c, out["version"], tmp_path / "t.json")
    gate_on(None, str(path))
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    with pytest.raises(ValueError, match="on the scale of calibrator"):
        validate_document(doc)


def test_the_cli_derives_and_writes_the_config(db_env, capsys, tmp_path):
    with session_scope() as c:
        cal = calibrator.fit_calibrator(c, _mock_dataset(_step_data(2000)))["version"]
    capsys.readouterr()
    path = tmp_path / "gate_thresholds.json"
    assert cli(["thresholds", "--calibrator-version", str(cal), "--write", str(path)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["calibrator_version"] == cal and out["config"] == str(path)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == out["version"]
    assert cli(["threshold-sets"]) == 0
    sets = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["threshold_sets"]
    assert sets[-1]["version"] == out["version"]
