"""E4-S7: the immutable 3-table evidence schema (real PostgreSQL, migration 0013).

ocr_observation (raw, append-only) -> interpretation_candidate -> verified_fact. Direct SQL,
bypassing the application, cannot change or delete raw evidence; re-running OCR or extraction
never removes observations, verifications or reviewer decisions; ``ocr_block`` stays a working
compatibility view; every auto-accepted fact traces back to pixels and model versions."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import evidence, repo
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from cdi_adapter.webapp.review import ReviewDecision, decide
from tests.test_extract_integration import _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

IMMUTABLE = "is raw evidence"


def _doc_with_observations(lines: list[str]) -> tuple[str, str, list[dict]]:
    """A document read by RapidOCR, with the recognizer metadata S3 records (E4-S7)."""
    with session_scope() as c:
        sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        doc = repo.insert_source_document(c, sha256=sha, filename="rx.pdf",
                                          mime_type="application/pdf", byte_size=1_000_000,
                                          storage_key="unused")  # fmt: skip
        page = repo.insert_document_page(c, document_id=doc, page_no=1,
                                         storage_key=f"pages/{sha}/0001.png", thumb_key="t",
                                         width=1654, height=2339, dpi=200, preproc={})  # fmt: skip
        run = repo.start_run(c, document_id=doc, stage="ocr", model_name="rapidocr-onnxruntime")
        repo.insert_ocr_blocks(c, [
            {"document_id": doc, "page_id": page, "block_type": "line", "text": t,
             "bbox": [100, 100 + 40 * i, 1200, 130 + 40 * i], "polygon": None,
             "ocr_conf": 0.96, "reading_order": i, "engine": "rapidocr", "pipeline_run_id": run,
             "crop_hash": hashlib.sha256(f"{sha}:{i}".encode()).hexdigest(),
             "region_kind": "printed", "engine_version": "rapidocr-onnxruntime 1.4.4"}
            for i, t in enumerate(lines)
        ])  # fmt: skip
        repo.finish_run(c, run, metrics={})
        repo.insert_classification(c, document_id=doc, run_id=None, doc_type="prescription",
                                   specialty=None, languages=["en"], is_handwritten=False,
                                   page_spans=[], confidence=0.95, method="heuristic")  # fmt: skip
        blocks = repo.get_ocr_blocks(c, doc)
    return str(doc), str(run), blocks


def _governed_doc(extra: list[str] | None = None) -> tuple[str, str]:
    """The §8.2 prescription (every fact auto-accepted), or its header + ``extra`` Rx lines."""
    doc, run, blocks = _doc_with_observations(
        _rx() if extra is None else _rx()[:4] + ["Rx", *extra]
    )
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    return doc, run


def _one_observation(doc: str) -> str:
    with get_engine().connect() as c:
        return str(
            c.execute(
                text(
                    "SELECT id FROM ocr_observation WHERE document_id = :d "
                    "ORDER BY reading_order LIMIT 1"
                ),
                {"d": doc},
            ).scalar_one()
        )


# ------------------------------------------------------------------ AC: direct-SQL negative tests


@pytest.mark.parametrize("assignment", [
    "raw_text = 'tampered'", "bbox = ARRAY[0, 0, 1, 1]", "crop_hash = repeat('0', 64)",
    "engine = 'vlm'", "ocr_conf = 0.1", "engine_version = 'other'",
])  # fmt: skip
def test_direct_sql_cannot_update_raw_evidence(db_env, assignment):
    doc, _, _ = _doc_with_observations(["Tab Amlodipine 5 mg OD"])
    obs = _one_observation(doc)
    with pytest.raises(IntegrityError, match=IMMUTABLE), get_engine().begin() as c:
        c.execute(text(f"UPDATE ocr_observation SET {assignment} WHERE id = :o"), {"o": obs})
    with get_engine().connect() as c:
        row = c.execute(text("SELECT raw_text, engine FROM ocr_observation WHERE id = :o"),
                        {"o": obs}).one()  # fmt: skip
    assert tuple(row) == ("Tab Amlodipine 5 mg OD", "rapidocr")


def test_direct_sql_cannot_delete_or_truncate_raw_evidence(db_env):
    doc, _, _ = _doc_with_observations(["Tab Amlodipine 5 mg OD", "BP: 138/86 mmHg"])
    with pytest.raises(IntegrityError, match=IMMUTABLE), get_engine().begin() as c:
        c.execute(text("DELETE FROM ocr_observation WHERE document_id = :d"), {"d": doc})
    with pytest.raises(IntegrityError, match=IMMUTABLE), get_engine().begin() as c:
        c.execute(text("DELETE FROM ocr_block WHERE document_id = :d"), {"d": doc})  # the view
    with pytest.raises(IntegrityError, match="never truncated"), get_engine().begin() as c:
        c.execute(text("TRUNCATE ocr_observation CASCADE"))
    with pytest.raises(IntegrityError, match=IMMUTABLE), get_engine().begin() as c:
        c.execute(text("DELETE FROM source_document WHERE id = :d"), {"d": doc})  # cascade
    with get_engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM ocr_observation WHERE document_id = :d"),
                      {"d": doc}).scalar_one()  # fmt: skip
    assert n == 2


def test_supersession_is_a_pointer_set_once(db_env):
    doc, run, _ = _doc_with_observations(["Tab Amlodipine 5 mg OD"])
    obs = _one_observation(doc)
    with get_engine().begin() as c:
        run2 = repo.start_run(c, document_id=doc, stage="ocr", model_name="vlm")
        assert repo.supersede_ocr_observations(c, doc, run2) == 1
    with pytest.raises(IntegrityError, match="already superseded"), get_engine().begin() as c:
        c.execute(text("UPDATE ocr_observation SET superseded_by = :r WHERE id = :o"),
                  {"r": run, "o": obs})  # fmt: skip


def test_verified_facts_and_candidates_cannot_be_rewritten(db_env):
    doc, _ = _governed_doc()
    with get_engine().connect() as c:
        vf = c.execute(text("SELECT id, candidate_id FROM verified_fact WHERE document_id = :d "
                            "LIMIT 1"), {"d": doc}).one()  # fmt: skip
    for sql in (
        "UPDATE verified_fact SET review_state = 'rejected' WHERE id = :i",
        "DELETE FROM verified_fact WHERE id = :i",
    ):
        with pytest.raises(IntegrityError, match="never updated or deleted"), \
                get_engine().begin() as c:  # fmt: skip
            c.execute(text(sql), {"i": vf.id})
    for sql in (
        "UPDATE interpretation_candidate SET code = 'X' WHERE id = :i",
        "DELETE FROM interpretation_candidate WHERE id = :i",
    ):
        with pytest.raises(IntegrityError, match="interpretation_candidate"), \
                get_engine().begin() as c:  # fmt: skip
            c.execute(text(sql), {"i": vf.candidate_id})
    with get_engine().begin() as c:  # the one change a candidate allows: its elimination, once
        c.execute(text("UPDATE interpretation_candidate SET eliminated_by = 'rule:test' "
                       "WHERE id = :i"), {"i": vf.candidate_id})  # fmt: skip


def test_raw_observations_have_no_code_columns(db_env):
    with get_engine().connect() as c:
        cols = set(c.execute(text("SELECT column_name FROM information_schema.columns "
                                  "WHERE table_name = 'ocr_observation'")).scalars())  # fmt: skip
    assert {
        "raw_text",
        "bbox",
        "crop_hash",
        "engine",
        "engine_version",
        "region_kind",
        "field_domain",
        "prompt_hash",
        "token_confidences",
        "superseded_by",
    } <= cols
    assert not cols & {"code", "code_system", "concept", "code_display"}


# ------------------------------------------------------------------ AC: re-runs never remove


def test_a_new_ocr_run_appends_and_the_view_shows_the_current_run(db_env):
    """What S3's handwritten pass does now: supersede the RapidOCR observations, append VLM ones."""
    doc, _, blocks = _doc_with_observations(["Tab Amlodipine 5 mg OD", "BP: 138/86 mmHg"])
    with session_scope() as c:
        page = blocks[0]["page_id"]
        run2 = repo.start_run(c, document_id=doc, stage="ocr", model_name="Qwen2.5-VL")
        repo.supersede_ocr_observations(c, doc, run2)
        repo.insert_ocr_blocks(c, [{"document_id": doc, "page_id": page, "block_type": "page",
                                    "text": "Tab Amlodipine 5 mg OD", "bbox": [0, 0, 1654, 2339],
                                    "polygon": None, "ocr_conf": 0.55, "reading_order": 0,
                                    "engine": "vlm", "pipeline_run_id": run2,
                                    "region_kind": "handwritten"}])  # fmt: skip
        current = repo.get_ocr_blocks(c, doc)
        every = c.execute(text("SELECT engine, superseded_by FROM ocr_observation "
                               "WHERE document_id = :d ORDER BY created_at"),
                          {"d": doc}).all()  # fmt: skip
    assert [b["engine"] for b in current] == ["vlm"]  # the old delete-and-insert result
    assert sorted(e for e, _ in every) == ["rapidocr", "rapidocr", "vlm"]  # nothing removed
    assert {str(s) for e, s in every if e == "rapidocr"} == {str(run2)}


def test_re_extraction_keeps_verifications_and_reviewer_decisions(db_env):
    doc, _ = _governed_doc()
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        target = next(f for f in facts if f["fact_type"] == "medication")
        decide(c, target["id"], ReviewDecision(action="correct",
                                               corrections={"local_text": "Tab Amlodipine 10"}),
               "dr-e4s7")  # fmt: skip
        before = c.execute(text("SELECT id FROM verified_fact WHERE document_id = :d"),
                           {"d": doc}).scalars().all()  # fmt: skip
    assert before
    with session_scope() as c:  # extraction runs again: it rewrites clinical_fact
        blocks = repo.get_ocr_blocks(c, doc)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        after = set(c.execute(text("SELECT id FROM verified_fact WHERE document_id = :d"),
                              {"d": doc}).scalars())  # fmt: skip
        reviewer = c.execute(
            text("SELECT v.reviewer_id, v.value_text, c.source FROM verified_fact v "
                 "JOIN interpretation_candidate c ON c.id = v.candidate_id "
                 "WHERE v.document_id = :d AND v.verification_method = 'reviewer'"), {"d": doc},
        ).all()  # fmt: skip
    assert set(before) <= after  # nothing purged, the new run's verifications are added
    assert [tuple(r) for r in reviewer] == [("dr-e4s7", "Tab Amlodipine 10", "reviewer")]


def test_a_reviewer_correction_adds_a_reviewer_candidate_and_a_verified_fact(db_env):
    doc, _ = _governed_doc(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    with session_scope() as c:
        held = next(f for f in repo.get_facts_for_document(c, doc)
                    if f["review_state"] == "in_review")  # fmt: skip
        out = decide(c, held["id"], ReviewDecision(action="correct",
                                                   corrections={"local_text": "Tab Metformin 500"}),
                     "dr-e4s7")  # fmt: skip
        chain = evidence.trace(c, out["result_fact_id"])
    assert len(chain) == 1
    v, cand = chain[0]["verified_fact"], chain[0]["candidate"]
    assert v["verification_method"] == "reviewer" and v["reviewer_id"] == "dr-e4s7"
    assert v["review_state"] == "corrected" and v["value_text"] == "Tab Metformin 500"
    assert cand["source"] == "reviewer" and cand["created_by"] == "dr-e4s7"
    assert cand["evidence"] == {"action": "correct", "prior_fact_id": str(held["id"])}
    assert chain[0]["observations"]  # still pointing at the page it was read from


# ------------------------------------------------------------------ AC: traceability + view


def test_every_auto_accepted_fact_traces_back_to_pixels_and_model_versions(db_env):
    doc, run = _governed_doc()
    with session_scope() as c:
        accepted = [f for f in repo.get_facts_for_document(c, doc)
                    if f["review_state"] == "auto_accepted"]  # fmt: skip
        assert accepted
        for f in accepted:
            (link,) = evidence.trace(c, f["id"])
            v, cand, obs = link["verified_fact"], link["candidate"], link["observations"]
            assert v["verification_method"] == "auto_gate" and v["review_state"] == "auto_accepted"
            assert v["policy_id"].startswith("s6-gate:") and 0 <= v["confidence"] <= 1
            assert v["model_version"] and '"extractor": "stub"' in v["model_version"]
            assert cand["id"] == v["candidate_id"] and cand["source"] in ("terminology",
                                                                          "extractor")  # fmt: skip
            assert cand["code"] == f["code"] and cand["model_version"] == v["model_version"]
            assert obs and [str(o["id"]) for o in obs] == [str(i) for i in v["observation_ids"]]
            for o in obs:  # pixels + engine versions
                assert o["page_image"].startswith("pages/") and len(o["bbox"]) == 4
                assert len(o["crop_hash"]) == 64 and o["engine"] == "rapidocr"
                assert o["engine_version"] == "rapidocr-onnxruntime 1.4.4"
                assert o["run_model"] == "rapidocr-onnxruntime"
    validate_document(doc)  # a re-run of the gate records nothing twice
    with get_engine().connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM verified_fact WHERE document_id = :d "
                "AND verification_method = 'auto_gate'"
            ),
            {"d": doc},
        ).scalar_one()
    assert n == len(accepted)


def test_the_compatibility_view_keeps_provenance_and_the_review_consoles_working(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    doc, _ = _governed_doc(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    with session_scope() as c:
        held = next(f for f in repo.get_facts_for_document(c, doc)
                    if f["review_state"] == "in_review")  # fmt: skip
        ids = repo.get_provenance(c, held["id"])[0]["ocr_block_ids"]
        via_view = c.execute(text("SELECT count(*) FROM ocr_block WHERE id = ANY(:i)"),
                             {"i": ids}).scalar_one()  # fmt: skip
    assert ids and via_view == len(ids)  # fact_provenance.ocr_block_ids resolve through the view
    client = signed_in(app, "reviewer")
    detail = client.get(f"/api/review/facts/{held['id']}")
    assert detail.status_code == 200  # S8 /review: the evidence lines and their boxes
    assert [b["id"] for b in detail.json()["provenance"][0]["ocr_blocks"]] == [str(i) for i in ids]
    ev = client.get(f"/api/documents/{doc}/evidence").json()
    assert ev["ocr_blocks"] == 7  # 4 header lines + "Rx" + 2 medicine lines
