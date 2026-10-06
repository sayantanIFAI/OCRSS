"""E0-S9 integration: S4 facts -> S5 bind_document on real PostgreSQL (no object store)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from tests.test_extract_integration import RX_LINES, _doc_with_ocr, _write

pytestmark = pytest.mark.integration


def test_bind_document_codes_the_worked_example(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    _write(doc, blocks)
    metrics = bind_document(doc)
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        status = repo.get_document(c, doc)["status"]
        runs = [r for r in repo.get_runs(c, doc) if r["stage"] == "terminology"]
        med_codes = dict(
            c.execute(
                text("SELECT m.drug_text, m.rxlike_code FROM medication_detail m "
                          "JOIN clinical_fact f ON f.id = m.fact_id "
                          "WHERE :d = ANY(f.source_doc_ids)"), {"d": doc}
            ).all()
        )  # fmt: skip
    codes = {f["local_text"]: (f["code"], f["code_status"]) for f in facts}
    assert codes["Type 2 diabetes mellitus"] == ("44054006", "bound")
    assert codes["Hypertension"] == ("59621000", "bound")
    assert codes["blood pressure"] == ("85354-9", "bound")  # components 8480-6 / 8462-4 in FHIR
    assert codes["body weight"] == ("29463-7", "bound")
    assert med_codes == {"Metformin": "372567009", "Amlodipine": "386864001",
                         "Atorvastatin": "373444002"}  # fmt: skip
    assert all(f["code_status"] in ("bound", "local_only") for f in facts)  # never unmapped
    assert metrics == {"facts": len(facts), "bound": len(facts), "local_only": 0}
    assert status == "normalized" and [r["status"] for r in runs] == ["ok"]


def test_unknown_terms_stay_local_only_with_text_preserved(db_env):
    doc, blocks = _doc_with_ocr(["Diagnosis: Zygomatic flurbosis", "Rx", "Tab Unknownium 5 mg OD"])
    _write(doc, blocks)
    metrics = bind_document(doc)
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
    assert metrics["local_only"] == len(facts) == 2 and metrics["bound"] == 0
    for f in facts:
        assert f["code"] is None and f["code_status"] == "local_only"
        assert f["local_text"] and f["confidence_terminology"] == pytest.approx(0.4)
