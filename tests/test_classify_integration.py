"""E0-S6 integration.

* Heuristic path: real PostgreSQL only (it reads persisted OCR blocks; no object store touched).
* VLM-fallback and full ingest -> OCR -> classify chain: need real MinIO (page-1 image).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from cdi_adapter import repo
from cdi_adapter.classify.service import classify_document
from cdi_adapter.db import session_scope
from cdi_adapter.ingest.service import ingest_bytes
from cdi_adapter.ocr.service import ocr_document
from tests.helpers import make_pdf
from tests.test_classify_unit import CARDIO_RX

pytestmark = pytest.mark.integration


def _doc_with_ocr(lines: list[str], conf: float = 0.98) -> str:
    with session_scope() as c:
        sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        doc = repo.insert_source_document(c, sha256=sha, filename="rx.pdf",
                                          mime_type="application/pdf", byte_size=1,
                                          storage_key="unused")  # fmt: skip
        page = repo.insert_document_page(c, document_id=doc, page_no=1, storage_key="unused",
                                         thumb_key="unused", width=1654, height=2339, dpi=200,
                                         preproc={})  # fmt: skip
        repo.set_document_status(c, doc, "ocr_done")
        repo.insert_ocr_blocks(c, [
            {"document_id": doc, "page_id": page, "block_type": "line", "text": t,
             "bbox": [10, 10 + 30 * i, 900, 35 + 30 * i], "polygon": None, "ocr_conf": conf,
             "reading_order": i, "engine": "rapidocr", "pipeline_run_id": None}
            for i, t in enumerate(lines)
        ])  # fmt: skip
    return str(doc)


def test_heuristic_path_classifies_without_any_vlm_call(db_env):
    class NoVLM:
        def vlm_json(self, *a, **k):
            raise AssertionError("heuristic page must not call the VLM")

    doc = _doc_with_ocr(CARDIO_RX.splitlines())
    res = classify_document(doc, client=NoVLM())
    assert (res["doc_type"], res["method"], res["confidence"]) == ("prescription", "heuristic",
                                                                   0.95)  # fmt: skip
    with session_scope() as c:
        row = repo.get_latest_classification(c, doc)
        d = repo.get_document(c, doc)
        runs = [r for r in repo.get_runs(c, doc) if r["stage"] == "classify"]
    assert row["doc_type"] == "prescription" and row["method"] == "heuristic"
    assert row["is_handwritten"] is False and row["raw"]["score"] >= 9
    assert d["status"] == "classified"
    assert [r["status"] for r in runs] == ["ok"]


def test_full_chain_ingest_ocr_classify(app_env):
    pdf = make_pdf([CARDIO_RX.splitlines()])
    res = ingest_bytes(pdf, "cardio-rx.pdf")
    ocr_document(res.document_id, force_engine="rapidocr")
    out = classify_document(res.document_id)
    assert out["doc_type"] == "prescription"


def test_vlm_fallback_for_ambiguous_page(app_env):
    res = ingest_bytes(make_pdf([["short ambiguous note"]]), "ambiguous.pdf")
    ocr_document(res.document_id, force_engine="rapidocr")
    out = classify_document(res.document_id)  # stub VLM
    assert out["method"] == "vlm"


def test_an_unsure_ocr_page_skips_the_fast_path_and_the_vlm_can_call_it_handwritten(db_env):
    """2026-09-26: a printed letterhead + handwritten Rx (mean OCR 0.80) was taken by the rule-based
    fast path, which never says "handwritten", so the drugs were extracted from print-OCR garble."""
    calls = []

    class HandwritingVLM:
        model = "vlm-under-test"

        def vlm_json(self, png, prompt, schema, **k):
            calls.append(schema)
            return {"doc_type": "prescription", "is_handwritten": True, "languages": ["en"],
                    "confidence": 0.9}  # fmt: skip

    class Store:
        def get_bytes(self, key):
            return b"page-1-png"

    doc = _doc_with_ocr(CARDIO_RX.splitlines(), conf=0.80)  # the same text, unsure OCR
    res = classify_document(doc, client=HandwritingVLM(), store=Store())
    assert calls == ["classification.v1"] and res["method"] == "vlm"
    with session_scope() as c:
        row = repo.get_latest_classification(c, doc)
    assert row["is_handwritten"] is True and row["method"] == "vlm"  # -> the VLM handwriting pass


# ---- story E02-04 (Sprint Backlog, 2026-09-29): "ambiguous pages go to AI" and "never a guessed
# type", proven on real PostgreSQL. The model and the page image are passed in (as the handwriting
# test above does), so the AI decision is verified without an object store; the MinIO-backed
# full-chain tests above still cover the stored page image.


class _PageStore:
    def __init__(self):
        self.keys = []

    def get_bytes(self, key):
        self.keys.append(key)
        return b"page-1-png"


class _AnswerVLM:
    model = "vlm-under-test"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def vlm_json(self, png, prompt, schema, **k):
        self.calls.append((png, prompt, schema))
        return dict(self.answer)


def test_e02_04_an_ambiguous_page_is_decided_by_the_ai_and_recorded(db_env):
    lines = ["CareFlow Polyclinic", "Patient seen today, notes attached", "Review next week"]
    doc = _doc_with_ocr(lines)  # legible, but too short and too weak for the rules
    vlm = _AnswerVLM(
        {
            "doc_type": "opd_note",
            "specialty": "general medicine",
            "is_handwritten": False,
            "languages": ["en"],
            "confidence": 0.82,
        }
    )
    store = _PageStore()
    res = classify_document(doc, client=vlm, store=store)

    assert (res["doc_type"], res["method"], res["model"]) == ("opd_note", "vlm", "vlm-under-test")
    assert res["confidence"] == 0.82
    assert len(vlm.calls) == 1
    png, prompt, schema = vlm.calls[0]
    assert (png, schema) == (b"page-1-png", "classification.v1")  # the page image goes to the AI
    assert "Patient seen today, notes attached" in prompt  # with the page's OCR text as a hint
    assert store.keys == ["unused"]  # page 1's stored image was read once
    with session_scope() as c:
        row = repo.get_latest_classification(c, doc)
        d = repo.get_document(c, doc)
        runs = [r for r in repo.get_runs(c, doc) if r["stage"] == "classify"]
    assert (row["doc_type"], row["method"]) == ("opd_note", "vlm")
    assert row["raw"]["model"] == "vlm-under-test"  # the model is kept with the raw answer
    assert row["specialty"] == "general medicine" and row["is_handwritten"] is False
    assert d["status"] == "classified" and [r["status"] for r in runs] == ["ok"]


@pytest.mark.parametrize(
    "answer",
    [{"doc_type": "invoice", "confidence": 0.93},  # not one of the 11 types
     {"doc_type": "prescription", "confidence": 0.9, "_partial": True},  # malformed answer
     {"confidence": 0.9}],  # no type at all
    ids=["unknown-type", "partial-answer", "no-type"],
)  # fmt: skip
def test_e02_04_an_invalid_ai_answer_is_other_never_a_guess(db_env, answer):
    doc = _doc_with_ocr(["Scanned page", "illegible", "stamp"])
    res = classify_document(doc, client=_AnswerVLM(answer), store=_PageStore())
    assert res["doc_type"] == "other" and res["confidence"] <= 0.5 and res["method"] == "vlm"
    with session_scope() as c:
        row = repo.get_latest_classification(c, doc)
    assert row["doc_type"] == "other" and float(row["confidence"]) <= 0.5
