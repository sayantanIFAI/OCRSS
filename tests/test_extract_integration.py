"""E0-S8 integration.

* ``write_extraction`` (all S4 database work) on real PostgreSQL with a real stub-model payload.
* ``extract_document`` skip path for unknown types (touches no object store).
* The full ``extract_document`` chain needs real MinIO (page-1 image).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.extract.prompt import build_extraction_prompt
from cdi_adapter.extract.service import IdentityMismatch, extract_document, write_extraction
from cdi_adapter.ingest.service import ingest_bytes
from cdi_adapter.ml.client import StubMLClient
from cdi_adapter.mpi import service as mpi
from tests.helpers import make_pdf

pytestmark = pytest.mark.integration

RX_LINES = [
    "CareFlow Polyclinic",
    "Name: Anjali Das   Age/Sex: 48 Y / F",
    "Date: 12/08/2026",
    "Diagnosis: Type 2 diabetes mellitus, Hypertension",
    "Rx",
    "Tab Metformin 500 mg 1-0-1 x 30 days",
    "Tab Amlodipine 5 mg OD x 30 days",
    "Tab Atorvastatin 10 mg 0-0-1",
    "BP: 138/86 mmHg   Weight: 72 kg",
]


def _doc_with_ocr(lines: list[str], doc_type: str = "prescription") -> tuple[str, list[dict]]:
    with session_scope() as c:
        sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        # > 700 KB: FHIR references the scan by URL (§7.2), so no object store is read here
        doc = repo.insert_source_document(c, sha256=sha, filename="rx.pdf",
                                          mime_type="application/pdf", byte_size=1_000_000,
                                          storage_key="unused")  # fmt: skip
        page = repo.insert_document_page(c, document_id=doc, page_no=1, storage_key="unused",
                                         thumb_key="unused", width=1654, height=2339, dpi=200,
                                         preproc={})  # fmt: skip
        repo.insert_ocr_blocks(c, [
            {"document_id": doc, "page_id": page, "block_type": "line", "text": t,
             "bbox": [100, 100 + 40 * i, 1200, 130 + 40 * i], "polygon": None,
             "ocr_conf": 0.96, "reading_order": i, "engine": "rapidocr", "pipeline_run_id": None}
            for i, t in enumerate(lines)
        ])  # fmt: skip
        repo.insert_classification(c, document_id=doc, run_id=None, doc_type=doc_type,
                                   specialty=None, languages=["en"], is_handwritten=False,
                                   page_spans=[], confidence=0.95, method="heuristic")  # fmt: skip
        blocks = repo.get_ocr_blocks(c, doc)
    return str(doc), blocks


def _write(doc, blocks, doc_type="prescription", **kw):
    prompt, bmap = build_extraction_prompt(doc_type, blocks)
    schema = {"prescription": "prescription.v3", "lab_report": "lab_report.v3"}[doc_type]
    payload = StubMLClient().vlm_json(None, prompt, schema, retries=0)
    with session_scope() as c:
        return write_extraction(c, document_id=doc, doc_type=doc_type, payload=payload,
                                block_id_map=bmap, blocks=blocks, run_ids=[],
                                model_stack={"extractor": "stub"}, **kw)  # fmt: skip


def test_write_extraction_facts_provenance_encounter_identity(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    out = _write(doc, blocks)
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        enc = c.execute(text("SELECT * FROM encounter WHERE id = :e"),
                        {"e": out["encounter_id"]}).mappings().one()  # fmt: skip
        patient = mpi.get_patient(c, out["patient_id"])
        prov = {f["id"]: repo.get_provenance(c, f["id"]) for f in facts}
        ext = c.execute(text("SELECT * FROM extraction WHERE id = :x"),
                        {"x": out["extraction_id"]}).mappings().one()  # fmt: skip
        doc_row = repo.get_document(c, doc)

    by_type = {}
    for f in facts:
        by_type.setdefault(f["fact_type"], []).append(f)
    assert len(by_type["condition"]) == 2 and len(by_type["medication"]) == 3
    assert {(f["local_text"], f["value_text"]) for f in by_type["vital_sign"]} == {
        ("blood pressure", "138/86"), ("body weight", None)}  # fmt: skip
    met = next(f for f in by_type["medication"] if "Metformin" in f["local_text"])
    assert (met["drug_text"], met["strength_num"], met["frequency_per_day"],
            met["duration_days"]) == ("Metformin", 500, 2, 30)  # fmt: skip

    # provenance: every fact is bound to its OCR block, with a bbox and the text it came from
    for f in facts:
        (p,) = prov[f["id"]]
        assert len(p["ocr_block_ids"]) >= 1 and len(p["bbox_union"]) == 4
        assert p["extracted_text"] and p["agent"] == "system"
    assert prov[met["id"]][0]["extracted_text"] == "Tab Metformin 500 mg 1-0-1 x 30 days"

    # fused confidence = 0.5 * (extract 0.97 + OCR 0.96)
    assert met["confidence_overall"] == pytest.approx(0.965, abs=1e-3)
    assert all(f["review_state"] == "pending" for f in facts)
    assert (enc["class"], str(enc["period_start"]), enc["date_precision"]) == (
        "AMB", "2026-08-12", "day")  # fmt: skip
    assert out["identity"] == "minted" and patient["name"] == "Anjali Das"
    assert doc_row["patient_id"] == out["patient_id"]
    assert ext["is_partial"] is False and ext["evidence_map"]["b1"] == str(blocks[0]["id"])


def test_re_extraction_supersedes_instead_of_doubling(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    pid = _write(doc, blocks)["patient_id"]
    first = len(repo_facts(doc))
    _write(doc, blocks, patient_id=pid)
    with session_scope() as c:
        n_ext = c.execute(text("SELECT count(*) FROM extraction WHERE document_id=:d"),
                          {"d": doc}).scalar_one()  # fmt: skip
        n_enc = c.execute(text("SELECT count(*) FROM encounter WHERE source_document_id=:d"),
                          {"d": doc}).scalar_one()  # fmt: skip
    assert len(repo_facts(doc)) == first and n_ext == 1 and n_enc == 1


def test_the_same_document_extracted_again_keeps_its_patient(db_env):
    # 2026-09-25: one image uploaded twice (same bytes = same document) got a second "Rahul":
    # a name-only reading never matches an existing record, so each extraction minted a new one
    lines = [ln.replace("Anjali Das", f"Test {uuid.uuid4().hex[:6]}") for ln in RX_LINES]
    doc, blocks = _doc_with_ocr(lines)
    first = _write(doc, blocks)
    with session_scope() as c:
        before = c.execute(text("SELECT count(*) FROM patient_identity")).scalar_one()
    again = _write(doc, blocks)  # no patient given: the upload again
    with session_scope() as c:
        after = c.execute(text("SELECT count(*) FROM patient_identity")).scalar_one()
        filed = c.execute(text("SELECT patient_id FROM source_document WHERE id = :d"),
                          {"d": doc}).scalar_one()  # fmt: skip
    assert again["patient_id"] == first["patient_id"] == filed and again["identity"] == "document"
    assert after == before  # no second record


def repo_facts(doc):
    with session_scope() as c:
        return repo.get_facts_for_document(c, doc)


def test_given_patient_is_used_and_document_reading_stored_as_alias(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    with session_scope() as c:
        pid, _ = mpi.resolve_identity(c, mpi.Candidate(name=f"Given {uuid.uuid4().hex[:6]}"))
    out = _write(doc, blocks, patient_id=pid,
                 expected_patient={"name": "Anjali Das", "dob": "1978-06-01"})  # fmt: skip
    assert out["patient_id"] == pid and out["identity"] == "given"
    with session_scope() as c:
        alias = c.execute(text("SELECT name FROM patient_identity_alias WHERE "
                               "source_document_id = :d"), {"d": doc}).scalar_one()  # fmt: skip
    assert alias == "Anjali Das"


def test_mismatch_guard_stops_and_leaves_no_facts(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    with pytest.raises(IdentityMismatch):
        _write(doc, blocks, patient_id=None,
               expected_patient={"name": "Ramesh Kumar", "dob": "1971-03-14"})  # fmt: skip
    assert repo_facts(doc) == []


def test_fact_without_ocr_support_gets_no_evidence(db_env):
    doc, blocks = _doc_with_ocr(RX_LINES)
    payload = {"medications": [{"drug_name": "Tab Invented 10 mg", "evidence": ["b99"],
                                "confidence": 0.99}], "confidence": 0.99}  # fmt: skip
    _, bmap = build_extraction_prompt("prescription", blocks)
    with session_scope() as c:
        write_extraction(c, document_id=doc, doc_type="prescription", payload=payload,
                         block_id_map=bmap, blocks=blocks, run_ids=[], model_stack={})  # fmt: skip
    (f,) = repo_facts(doc)
    with session_scope() as c:
        (p,) = repo.get_provenance(c, f["id"])
    assert p["ocr_block_ids"] == [] and p["extracted_text"] is None and p["bbox_union"] is None
    assert f["confidence_ocr"] is None and f["confidence_overall"] == pytest.approx(0.495)


def test_partial_payload_is_recorded(db_env):
    doc, blocks = _doc_with_ocr(["HbA1c 7.8 %"], doc_type="lab_report")
    _, bmap = build_extraction_prompt("lab_report", blocks)
    payload = {"tests": [{"value": 7.8}], "confidence": 0.9, "_partial": True, "_errors": ["x"]}
    with session_scope() as c:
        out = write_extraction(c, document_id=doc, doc_type="lab_report", payload=payload,
                               block_id_map=bmap, blocks=blocks, run_ids=[],
                               model_stack={})  # fmt: skip
        is_partial = c.execute(text("SELECT is_partial FROM extraction WHERE id=:x"),
                               {"x": out["extraction_id"]}).scalar_one()  # fmt: skip
    assert out["partial"] is True and is_partial is True


def test_unknown_doc_type_is_skipped_as_normalized(db_env):
    doc, _ = _doc_with_ocr(["misc admin form"], doc_type="administrative")
    assert extract_document(doc)["skipped"] is True
    with session_scope() as c:
        assert repo.get_document(c, doc)["status"] == "normalized"


def test_full_extract_document_chain(app_env):
    res = ingest_bytes(make_pdf([RX_LINES]), "anjali-rx.pdf")
    from cdi_adapter.classify.service import classify_document
    from cdi_adapter.ocr.service import ocr_document

    ocr_document(res.document_id, force_engine="rapidocr")
    classify_document(res.document_id)
    out = extract_document(res.document_id)
    # Two conditions, three medicines and two vitals; BP is one fact with two components.
    assert out["facts"] == 7
    facts = repo_facts(res.document_id)
    assert sum(f["fact_type"] == "medication" for f in facts) == 3
