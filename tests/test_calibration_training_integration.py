"""E4-S1: reviewer corrections -> the versioned calibration training table, end to end on mock data.

Synthetic prescriptions go through S4 -> S6; a test reviewer confirms, corrects and rejects facts
through the real S8 decision; the export builds the {proposed value, corrected value, doc_type,
fact_type, confidence} triples from clinical_fact + fact_provenance into a versioned, append-only
table. No production or clinician data is used."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.training import calibration
from cdi_adapter.training.__main__ import main as cli
from cdi_adapter.validate.service import validate_document
from cdi_adapter.webapp.review import ReviewDecision, decide
from tests.test_extract_integration import _doc_with_ocr, _write

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
REVIEWER = "dr-mock-e4s1"


def _mock_prescription(meds: list[str]) -> tuple[str, str]:
    name = "Cal" + uuid.uuid4().hex[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
    lines = ["CareFlow Polyclinic", f"Name: {name} Das   Age/Sex: 48 Y / F", "Date: 12/08/2026",
             "Diagnosis: Hypertension", "Rx", *meds]  # fmt: skip
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    return doc, name


def _facts(doc: str) -> dict[str, dict]:
    with session_scope() as c:
        return {f["local_text"]: f for f in repo.get_facts_for_document(c, doc)}


def _mine(rows: list[dict], doc: str) -> dict[str, dict]:
    return {r["proposed"]["local_text"]: r for r in rows if str(r["document_id"]) == doc}


def _adjudicated_mock_document() -> tuple[str, str, dict[str, str]]:
    """Four decisions on one mock prescription; one auto-accepted fact left undecided."""
    doc, name = _mock_prescription(["Tab Metformin 5000 mg 1-1-1",  # held: over the ceiling
                                    "Tab Amlodipine 5 mg OD x 30 days",
                                    "Tab Atorvastatin 10 mg 0-0-1",
                                    "Tab Telmisartan 40 mg OD"])  # fmt: skip
    f = _facts(doc)
    metformin = next(v for k, v in f.items() if "Metformin" in k)
    amlodipine = next(v for k, v in f.items() if "Amlodipine" in k)
    atorvastatin = next(v for k, v in f.items() if "Atorvastatin" in k)
    hypertension = next(v for v in f.values() if v["fact_type"] == "condition")
    with session_scope() as c:
        # corrected twice: one example, from the machine's proposal to the final value
        fix = ReviewDecision(action="correct", corrections={"local_text": "Tab Metformin 50"})
        first = decide(c, metformin["id"], fix, REVIEWER)
        fix = ReviewDecision(action="correct", corrections={"local_text": "Tab Metformin 500"})
        decide(c, first["result_fact_id"], fix, REVIEWER)
        decide(c, amlodipine["id"], ReviewDecision(action="accept"), REVIEWER)
        decide(c, atorvastatin["id"], ReviewDecision(action="reject"), REVIEWER)
        decide(
            c,
            hypertension["id"],
            ReviewDecision(  # an edit that changes nothing
                action="correct", corrections={"local_text": hypertension["local_text"]}
            ),
            REVIEWER,
        )
    return doc, name, {"metformin": str(metformin["id"]), "amlodipine": str(amlodipine["id"]),
                       "atorvastatin": str(atorvastatin["id"]),
                       "hypertension": str(hypertension["id"])}  # fmt: skip


def test_the_export_builds_every_triple_from_the_reviewers_decisions(db_env):
    doc, name, ids = _adjudicated_mock_document()
    with session_scope() as c:
        out = calibration.export(c, actor="test-export")
        rows = calibration.load(c, out["version"])
    assert out["unchanged"] is False and out["rows"] == len(rows)
    mine = _mine(rows, doc)
    assert len(mine) == 4  # Telmisartan was auto-accepted, never adjudicated: not an example
    met = next(v for k, v in mine.items() if "Metformin" in k)
    assert met["proposed_fact_id"] == uuid.UUID(ids["metformin"])
    assert met["decision"] == "corrected" and met["label"] == 0
    assert met["corrected"]["local_text"] == "Tab Metformin 500"  # the final value, not the 50
    assert met["proposed"]["strength_num"] == 5000 and met["machine_state"] == "held"
    aml = next(v for k, v in mine.items() if "Amlodipine" in k)
    assert (aml["decision"], aml["label"], aml["corrected"]) == ("confirmed", 1, aml["proposed"])
    assert aml["machine_state"] == "auto_accepted"
    ator = next(v for k, v in mine.items() if "Atorvastatin" in k)
    assert (ator["decision"], ator["label"], ator["corrected"]) == ("rejected", 0, None)
    htn = next(r for r in mine.values() if r["fact_type"] == "condition")
    assert htn["decision"] == "corrected" and htn["label"] == 1  # corrected to the same value
    for r in mine.values():  # the triple, and where it came from
        assert r["doc_type"] == "prescription" and r["fact_type"] in ("medication", "condition")
        assert 0 <= r["confidence"] <= 1 and r["reviewer"] == REVIEWER and r["decided_at"]
        assert r["model_stack"] == {"extractor": "stub"}
    assert name not in json.dumps([dict(r) for r in rows], default=str)  # no patient identifier


def test_versions_are_snapshots_written_only_when_the_data_changed(db_env):
    doc, _, _ = _adjudicated_mock_document()
    with session_scope() as c:
        v1 = calibration.export(c)
    with session_scope() as c:
        again = calibration.export(c)
    assert again["unchanged"] is True and again["version"] == v1["version"]
    doc2, _ = _mock_prescription(["Tab Metformin 5000 mg 1-1-1"])
    held = next(f for f in _facts(doc2).values() if f["review_state"] == "in_review")
    with session_scope() as c:
        decide(c, held["id"], ReviewDecision(action="reject"), REVIEWER)
        v2 = calibration.export(c)
        old, new = calibration.load(c, v1["version"]), calibration.load(c, v2["version"])
        forced = calibration.export(c, force=True)
    assert v2["version"] == v1["version"] + 1 and v2["rows"] == v1["rows"] + 1
    assert len(old) == v1["rows"]  # the earlier version is unchanged
    assert str(held["id"]) in {str(r["proposed_fact_id"]) for r in new}
    assert forced["version"] == v2["version"] + 1 and forced["sha256"] == v2["sha256"]


def test_a_published_version_can_never_be_changed(db_env):
    _adjudicated_mock_document()
    with session_scope() as c:
        out = calibration.export(c)
    for sql in ("UPDATE calibration_training_example SET label = 1 - label",
                "DELETE FROM calibration_training_example",
                "UPDATE calibration_dataset SET row_count = 0", "DELETE FROM calibration_dataset",
                "TRUNCATE calibration_training_example"):  # fmt: skip
        with pytest.raises(IntegrityError, match="published training dataset"):
            with get_engine().begin() as c:
                c.execute(text(sql))
    with session_scope() as c:
        assert len(calibration.load(c, out["version"])) == out["rows"]


def test_re_extraction_does_not_change_a_published_version(db_env):
    doc, _, _ = _adjudicated_mock_document()
    with session_scope() as c:
        out = calibration.export(c)
        before = calibration.load(c, out["version"])
        blocks = repo.get_ocr_blocks(c, doc)
    _write(doc, blocks)  # the document extracted again: its facts are rewritten
    with session_scope() as c:
        after = calibration.load(c, out["version"])
    assert after == before


def test_the_scheduled_job_and_its_cli(db_env, capsys):
    _adjudicated_mock_document()
    capsys.readouterr()  # the pipeline's own log lines

    def last_json() -> dict:  # the CLI prints one JSON line, after any log line
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert cli(["export", "--actor", "cron-test"]) == 0
    out = last_json()
    assert out["rows"] >= 4 and "sha256" in out
    assert cli(["versions"]) == 0
    versions = last_json()["versions"]
    assert versions[-1]["version"] == out["version"]
    assert versions[-1]["schema_version"] == "calibration_training.v1"
    # the nightly cron installed by the pod bootstrap runs the export
    boot = (ROOT / "infra" / "runpod" / "bootstrap_pod.sh").read_text(encoding="utf-8")
    assert "-m cdi_adapter.training export" in boot and "-e 'cdi_adapter.training export'" in boot
