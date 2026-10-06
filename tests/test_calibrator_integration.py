"""E4-S2: the isotonic calibrator end to end on MOCK training data (real PostgreSQL).

A synthetic, published training version whose scores over-state correctness (true rate = score
squared) is fitted per doc_type x fact_type x field; the held-out reliability shows the error
falling inside the (placeholder) tolerance; the gate uses the calibrator only when
``CDI_CALIBRATOR_VERSION`` names it. No production or clinician data is used."""

from __future__ import annotations

import json
import random
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.config import get_settings
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.training import calibrator
from cdi_adapter.training.__main__ import main as cli
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

MED = {"local_text": "Tab Amlodipine 5 mg", "drug_text": "Tab Amlodipine 5 mg",
       "code_system": "http://snomed.info/sct", "code": "386864001", "strength_num": 5.0,
       "strength_unit": "mg", "dose_num": 1.0, "frequency_code": "OD", "duration_days": 30.0,
       "value_num": None, "value_unit_ucum": None, "value_text": None,
       "code_display": "Amlodipine"}  # fmt: skip


def _mock_dataset(examples: list[tuple[str, str, float, dict, dict | None, str]]) -> int:
    """Publish a MOCK training version: (doc_type, fact_type, score, proposed, corrected,
    decision) rows written straight into the E4-S1 tables."""
    with get_engine().begin() as c:
        version = (c.execute(text("SELECT max(version) FROM calibration_dataset")).scalar_one()
                   or 0) + 1  # fmt: skip
        ds = c.execute(
            text("INSERT INTO calibration_dataset (version, schema_version, content_sha256, "
                 "  row_count, source, created_by) VALUES (:v, 'calibration_training.v1', "
                 "  :h, :n, '{\"mock\": true}', 'test-mock') RETURNING id"),
            {"v": version, "h": uuid.uuid4().hex * 2, "n": len(examples)},
        ).scalar_one()  # fmt: skip
        c.execute(
            text("INSERT INTO calibration_training_example (dataset_id, proposed_fact_id, "
                 "  final_fact_id, doc_type, fact_type, decision, proposed, corrected, "
                 "  confidence, machine_state, label) VALUES (:ds, :p, :p, :dt, :ft, :d, "
                 "  CAST(:pr AS jsonb), CAST(:co AS jsonb), :x, 'held', :lb)"),
            [{"ds": str(ds), "p": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock:{version}:{i}")),
              "dt": dt, "ft": ft, "d": dec,
              "pr": json.dumps(pr), "co": json.dumps(co) if co is not None else None,
              "x": x, "lb": int(dec == "confirmed")}
             for i, (dt, ft, x, pr, co, dec) in enumerate(examples)],
        )  # fmt: skip
    return version


def _overconfident(n: int, seed: int = 11) -> list[tuple]:
    """Prescription medications: the strength is right with probability score**2."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        x = round(rnd.random(), 4)
        right = rnd.random() < x**2
        corrected = MED if right else {**MED, "strength_num": 10.0}
        out.append(("prescription", "medication", x, MED, corrected,
                    "confirmed" if right else "corrected"))  # fmt: skip
    return out


def test_fit_on_mock_data_calibrates_per_field_within_the_tolerance(db_env):
    rows = _overconfident(6000)
    rows += [
        (
            "lab_report",
            "lab_result",
            0.9,
            {"local_text": "HbA1c", "value_num": 7.8},
            {"local_text": "HbA1c", "value_num": 7.8},
            "confirmed",
        )
    ] * 12  # too few: 12
    version = _mock_dataset(rows)
    with session_scope() as c:
        out = calibrator.fit_calibrator(c, version, actor="test-fit")
        slices = {(s["fact_type"], s["field"]): dict(s) for s in c.execute(
            text("SELECT s.* FROM calibration_slice s JOIN calibration_model m "
                 "ON m.id = s.model_id WHERE m.version = :v"), {"v": out["version"]},
        ).mappings()}  # fmt: skip
    assert out["dataset_version"] == version and out["fitted"] >= 1
    strength = slices[("medication", "strength")]
    assert strength["status"] == "fitted" and strength["n_test"] > 900  # held out, ~1 in 5
    assert strength["ece_before"] > 0.1  # the raw scores over-state correctness
    assert strength["ece_after"] < strength["ece_before"] / 3
    assert strength["within_tolerance"] is True  # placeholder tolerance 0.05
    rel = strength["reliability"]["after"]
    assert rel["bins"] and rel["ece"] == strength["ece_after"]
    assert strength["reliability"]["evaluated_on"] == "held_out"
    curve = strength["curve"]
    assert all(a <= b for a, b in zip(curve["y"], curve["y"][1:], strict=False))
    # the medicine itself was always right: a curve near 1 for that field
    assert slices[("medication", "medicine")]["curve"]["y"][-1] == 1.0
    lab = slices[("lab_result", "value")]
    assert lab["status"] == "insufficient_data" and lab["curve"] is None  # never guessed
    with session_scope() as c:
        model = c.execute(text("SELECT tolerance, tolerance_basis FROM calibration_model "
                               "WHERE version = :v"), {"v": out["version"]}).one()  # fmt: skip
    assert model.tolerance == pytest.approx(0.05) and "placeholder" in model.tolerance_basis


def test_a_fitted_calibrator_can_never_be_changed(db_env):
    version = _mock_dataset(_overconfident(300))
    with session_scope() as c:
        calibrator.fit_calibrator(c, version)
    for sql in ("UPDATE calibration_slice SET ece_after = 0", "DELETE FROM calibration_slice",
                "UPDATE calibration_model SET tolerance = 0.5", "DELETE FROM calibration_model",
                "TRUNCATE calibration_slice"):  # fmt: skip
        with pytest.raises(IntegrityError, match="fitted calibrator"):
            with get_engine().begin() as c:
                c.execute(text(sql))


@pytest.fixture
def calibrator_on(monkeypatch):
    def use(version: int | None) -> None:
        if version is None:
            monkeypatch.delenv("CDI_CALIBRATOR_VERSION", raising=False)
        else:
            monkeypatch.setenv("CDI_CALIBRATOR_VERSION", str(version))
        get_settings.cache_clear()
        calibrator.clear_cache()

    yield use
    monkeypatch.delenv("CDI_CALIBRATOR_VERSION", raising=False)
    get_settings.cache_clear()
    calibrator.clear_cache()


def _gated(doc: str) -> dict[str, dict]:
    with session_scope() as c:
        return {f["fact_type"] + ":" + f["local_text"]: f
                for f in repo.get_facts_for_document(c, doc)}  # fmt: skip


def test_the_gate_uses_the_calibrator_only_when_named(db_env, calibrator_on):
    # mock data: prescription medications right exactly 3 times in 10 in every score band
    # (deterministic, so the fit and its held-out split never depend on chance)
    rows = [
        (
            "prescription",
            "medication",
            round(0.5 + (i % 500) / 1000, 4),
            MED,
            MED if i % 10 < 3 else {**MED, "dose_num": 2.0},
            "confirmed" if i % 10 < 3 else "corrected",
        )
        for i in range(2000)
    ]
    with session_scope() as c:
        model = calibrator.fit_calibrator(c, _mock_dataset(rows))["version"]

    calibrator_on(None)  # the default: exactly as before
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    assert validate_document(doc)["auto_accepted"] == 7

    calibrator_on(model)
    out = validate_document(doc)
    facts = _gated(doc)
    meds = [f for k, f in facts.items() if k.startswith("medication:")]
    others = [f for k, f in facts.items() if not k.startswith("medication:")]
    assert meds and all(f["review_state"] == "in_review" for f in meds)  # calibrated ~0.3
    assert all(f["review_state"] == "auto_accepted" for f in others)  # no slice: unchanged
    note = meds[0]["review_note"]
    assert note["calibrator"]["version"] == model and note["calibrated"] < 0.85
    assert set(note["calibrator"]["fields"]) >= {"medicine", "dose"}
    raw = note["calibrator"]["raw"]  # the fixed score, still what confidence_overall holds
    assert raw == pytest.approx(meds[0]["confidence_overall"], abs=1e-4)
    assert out["in_review"] == len(meds)


def test_a_missing_calibrator_version_is_an_error_not_a_silent_fallback(db_env, calibrator_on):
    calibrator_on(9999)
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    with pytest.raises(LookupError, match="no such fitted calibrator"):
        validate_document(doc)


def test_the_cli_fits_and_writes_the_reliability_diagrams(db_env, capsys, tmp_path):
    version = _mock_dataset(_overconfident(1500, seed=5))
    capsys.readouterr()
    report = tmp_path / "reliability.html"
    assert cli(["fit", "--dataset-version", str(version), "--report", str(report)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["dataset_version"] == version and out["fitted"] >= 1
    page = report.read_text(encoding="utf-8")
    assert "<svg" in page and "medication / strength" in page and "placeholder" in page
    assert cli(["calibrators"]) == 0
    listed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["calibrators"]
    assert listed[-1]["version"] == out["version"]
