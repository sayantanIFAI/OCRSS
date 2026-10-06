"""E0-S5 integration: ingest -> OCR against real PostgreSQL 16 + real MinIO (no substitutes)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.ingest.service import ingest_bytes
from cdi_adapter.ocr.service import ocr_document
from tests.helpers import make_pdf

pytestmark = pytest.mark.integration


def test_ocr_writes_ordered_blocks_with_bboxes_and_is_idempotent(app_env):
    pdf = make_pdf([["LABORATORY REPORT", "HbA1c 7.8 % 4.0-5.6", "Creatinine 1.1 mg/dL"]])
    res = ingest_bytes(pdf, "lab-ocr.pdf")
    first = ocr_document(res.document_id, force_engine="rapidocr")
    again = ocr_document(res.document_id, force_engine="rapidocr")
    with session_scope() as c:
        blocks = repo.get_ocr_blocks(c, res.document_id)
        doc = repo.get_document(c, res.document_id)
        runs = [r for r in repo.get_runs(c, res.document_id) if r["stage"] == "ocr"]
    assert first["blocks"] == again["blocks"] == len(blocks) >= 3  # re-run replaced, not doubled
    assert "hba1c" in " ".join(b["text"] for b in blocks).lower().replace(" ", "")
    assert all(len(b["bbox"]) == 4 and b["engine"] == "rapidocr" for b in blocks)
    assert [b["reading_order"] for b in blocks] == list(range(len(blocks)))
    assert doc["status"] == "ocr_done"
    assert [r["status"] for r in runs] == ["ok", "ok"]


def test_vlm_pass_supersedes_rapidocr_blocks(app_env):
    res = ingest_bytes(make_pdf([["handwritten-ish note", "Rx Tab Dolo 650"]]), "hw.pdf")
    ocr_document(res.document_id, force_engine="rapidocr")

    class Transcriber:
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
            return "Rx\nTab Dolo 650 SOS"

    ocr_document(res.document_id, force_engine="vlm", client=Transcriber())
    with session_scope() as c:
        blocks = repo.get_ocr_blocks(c, res.document_id)
    assert {b["engine"] for b in blocks} == {"vlm"}
    assert [b["text"] for b in blocks] == ["Rx", "Tab Dolo 650 SOS"]
    assert all(b["ocr_conf"] == pytest.approx(0.55) for b in blocks)


def test_every_ocr_run_appends_raw_observations(app_env):
    """E4-S7: the RapidOCR observations are superseded by the VLM pass, never deleted, and each
    observation records the pixels it was read from and the engine that read them."""
    res = ingest_bytes(make_pdf([["handwritten-ish note", "Rx Tab Dolo 650"]]), "hw-e4s7.pdf")
    first = ocr_document(res.document_id, force_engine="rapidocr")

    class Transcriber:
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
            return "Rx\nTab Dolo 650 SOS"

    second = ocr_document(res.document_id, force_engine="vlm", client=Transcriber())
    with session_scope() as c:
        obs = c.execute(
            text("SELECT engine, engine_version, region_kind, crop_hash, prompt_hash, "
                 "  superseded_by FROM ocr_observation WHERE document_id = :d"),
            {"d": res.document_id},
        ).mappings().all()  # fmt: skip
    old = [o for o in obs if o["engine"] == "rapidocr"]
    new = [o for o in obs if o["engine"] == "vlm"]
    assert len(old) == first["blocks"] >= 2 and len(new) == second["blocks"] == 2
    assert second["superseded"] == len(old)
    assert all(o["superseded_by"] is not None for o in old)  # a pointer, not a delete
    assert all(o["superseded_by"] is None for o in new)
    assert all(o["region_kind"] == "printed" and o["prompt_hash"] is None for o in old)
    assert all(o["region_kind"] == "handwritten" and len(o["prompt_hash"]) == 64 for o in new)
    assert all(len(o["crop_hash"]) == 64 and o["engine_version"] for o in obs)
