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


def _doc_with_ocr(lines: list[str]) -> str:
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
             "bbox": [10, 10 + 30 * i, 900, 35 + 30 * i], "polygon": None, "ocr_conf": 0.98,
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
