"""E0-S10 integration: S4 -> S5 -> S6 on real PostgreSQL (no object store)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.extract.prompt import build_extraction_prompt
from cdi_adapter.extract.service import write_extraction
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import RX_LINES, _doc_with_ocr, _write


def _rx() -> list[str]:
    """The worked-example prescription for a patient no other test shares."""
    name = "Val" + uuid.uuid4().hex[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
    return [ln.replace("Anjali Das", f"{name} Das") for ln in RX_LINES]


pytestmark = pytest.mark.integration


def _facts(doc):
    with session_scope() as c:
        return repo.get_facts_for_document(c, doc)


def _tasks(doc):
    with session_scope() as c:
        # held-fact tasks only; a random ~5 % audit_sample task is covered by its own unit test
        return c.execute(text("SELECT * FROM review_task WHERE document_id = :d "
                              "AND kind <> 'audit_sample' ORDER BY kind"),
                         {"d": doc}).mappings().all()  # fmt: skip


def test_clean_extraction_is_governed_and_status_validated(db_env):
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    counts = validate_document(doc)
    facts = _facts(doc)
    assert counts["facts"] == len(facts) == 7 and counts["in_review"] == 0
    assert all(f["review_state"] == "auto_accepted" for f in facts)
    assert all(f["review_note"]["findings"] == [] for f in facts)
    with session_scope() as c:
        assert repo.get_document(c, doc)["status"] == "validated"
        runs = [r for r in repo.get_runs(c, doc) if r["stage"] == "validate"]
    assert runs[0]["status"] == "ok" and runs[0]["metrics"]["auto_accepted"] == 7


def test_partial_extraction_holds_every_fact(db_env):
    """ARCHITECTURE Appendix D: a _partial extraction -> 0 auto-accepted, all in_review."""
    doc, blocks = _doc_with_ocr(_rx())
    prompt, bmap = build_extraction_prompt("prescription", blocks)
    from cdi_adapter.ml.client import StubMLClient

    payload = StubMLClient().vlm_json(None, prompt, "prescription.v3", retries=0)
    payload["_partial"] = True
    with session_scope() as c:
        write_extraction(c, document_id=doc, doc_type="prescription", payload=payload,
                         block_id_map=bmap, blocks=blocks, run_ids=[], model_stack={})  # fmt: skip
    bind_document(doc)
    counts = validate_document(doc)
    assert counts["auto_accepted"] == 0 and counts["in_review"] == counts["facts"] == 7
    assert all(f["review_state"] == "in_review" for f in _facts(doc))
    (task,) = _tasks(doc)
    assert task["status"] == "open" and len(task["fact_ids"]) == 7


def test_blockers_warnings_and_task_kind(db_env):
    lines = ["Name: Dose Test", "Diagnosis: Zygomatic flurbosis", "Rx",
             "Tab Metformin 5000 mg 1-1-1", "BP: 380/86"]  # fmt: skip
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    by = {f["local_text"]: f for f in _facts(doc)}
    met = next(f for k, f in by.items() if "Metformin" in k)
    assert met["review_state"] == "in_review"
    assert "med-dose-ceiling" in [x["code"] for x in met["review_note"]["findings"]]
    assert by["Zygomatic flurbosis"]["review_state"] == "in_review"  # unmapped warn -> 0.9 cap
    assert by["blood pressure"]["review_state"] == "in_review"  # systolic 380 implausible
    (task,) = _tasks(doc)
    assert task["kind"] == "dose_check" and len(task["fact_ids"]) == 3


def test_missing_evidence_is_a_blocker(db_env):
    doc, blocks = _doc_with_ocr(_rx())
    _, bmap = build_extraction_prompt("prescription", blocks)
    payload = {"diagnoses": [{"text": "Asthma", "evidence": ["b404"], "confidence": 0.99}],
               "medications": [], "confidence": 0.99}  # fmt: skip
    with session_scope() as c:
        write_extraction(c, document_id=doc, doc_type="prescription", payload=payload,
                         block_id_map=bmap, blocks=blocks, run_ids=[], model_stack={})  # fmt: skip
    bind_document(doc)
    validate_document(doc)
    (f,) = _facts(doc)
    assert f["review_state"] == "in_review"
    assert [x["code"] for x in f["review_note"]["findings"]] == ["evidence-missing"]


def test_cross_document_contradiction_and_duplicate(db_env):
    lines_a = [
        "Name: Conflict Casex  Age/Sex: 60 Y / M",
        "Date: 01/09/2026",
        "LABORATORY REPORT",
        "HbA1c 7.8 % 4.0-5.6",
        "Serum Creatinine 1.1 mg/dL",
    ]
    lines_b = [ln.replace("HbA1c 7.8", "HbA1c 9.1") for ln in lines_a]
    doc_a, blocks_a = _doc_with_ocr(lines_a, doc_type="lab_report")
    pid = _write(doc_a, blocks_a, doc_type="lab_report")["patient_id"]
    bind_document(doc_a)
    validate_document(doc_a)
    assert all(f["review_state"] == "auto_accepted" for f in _facts(doc_a))

    doc_b, blocks_b = _doc_with_ocr(lines_b, doc_type="lab_report")
    _write(doc_b, blocks_b, doc_type="lab_report", patient_id=pid)
    bind_document(doc_b)
    counts = validate_document(doc_b)
    b = {f["code"]: f for f in _facts(doc_b)}
    a = {f["code"]: f for f in _facts(doc_a)}
    assert counts["conflicts"] == 1
    assert b["4548-4"]["review_state"] == "in_review"  # 9.1 vs 7.8 same day
    assert a["4548-4"]["review_state"] == "in_review"  # the earlier fact is pulled back too
    assert b["2160-0"]["review_state"] == "auto_accepted"  # same value -> duplicate, info only
    with session_scope() as c:
        ids = [str(f["id"]) for f in b.values()]
        kinds = sorted(c.execute(text(
            "SELECT conflict_type || ':' || evidence_state || ':' || severity FROM fact_conflict "
            "WHERE fact_a = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
        ).scalars())  # fmt: skip
    assert kinds == ["duplicate:SUPPORTED:info", "value_mismatch:CONTRADICTED:blocker"]


def test_human_decisions_are_never_regated(db_env):
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    first = _facts(doc)[0]
    with session_scope() as c:
        c.execute(text("UPDATE clinical_fact SET review_state = 'rejected' WHERE id = :i"),
                  {"i": first["id"]})  # fmt: skip
    validate_document(doc)
    validate_document(doc)  # idempotent re-run
    after = {f["id"]: f for f in _facts(doc)}
    assert after[first["id"]]["review_state"] == "rejected"
    assert len(_tasks(doc)) <= 1  # tasks recomputed, not duplicated
