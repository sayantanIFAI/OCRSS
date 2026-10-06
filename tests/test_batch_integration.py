"""E2-S8 integration on real PostgreSQL: a batched extraction writes exactly what one call per
document writes (facts, values, evidence blocks), for every document of the batch.

The page images come from an in-memory store (the service takes ``store``), so no MinIO is
needed; the model is the stub, whose batch answers each item as its own request."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.extract.service import extract_document, extract_documents
from cdi_adapter.ml.client import StubMLClient
from tests.test_extract_integration import RX_LINES, _doc_with_ocr

# a patient of this test's own: the shared test database is not reset between tests, and
# other tests expect to mint "Anjali Das" themselves
NAME = "Kavya " + "".join(chr(97 + int(c, 16) % 26) for c in uuid.uuid4().hex[:8]).title()
RX = [ln.replace("Anjali Das", NAME) for ln in RX_LINES]

pytestmark = pytest.mark.integration

VITALS = ["CareFlow Polyclinic - VITAL SIGNS CHART", f"Name: {NAME}   Age: 48 Y",
          "Recorded on: 12/08/2026", "Pulse: 78 /min", "BP: 128/82 mmHg", "SpO2: 98 %",
          "Allergies: Penicillin"]  # fmt: skip


class MemoryStore:
    def get_bytes(self, key: str) -> bytes:
        return b"\x89PNG-" + key.encode()


class CountingStub(StubMLClient):
    def __init__(self) -> None:
        super().__init__()
        self.batches = self.singles = 0

    def generate_batch(self, items):
        self.batches += 1
        return super().generate_batch(items)

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        self.singles += 1
        return super().generate(prompt, image_png, max_tokens=max_tokens, json_schema=json_schema)


def _facts(doc: str) -> list[tuple]:
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        blocks = {str(b["id"]): b["text"] for b in repo.get_ocr_blocks(c, doc)}
        out = []
        for f in facts:
            prov = c.execute(
                text("SELECT ocr_block_ids FROM fact_provenance WHERE fact_id = :f"),
                {"f": f["id"]},
            ).scalar_one()
            out.append((f["fact_type"], f["local_text"], str(f["value_num"]), f["value_text"],
                        f["value_unit_ucum"], str(f["confidence_overall"]), f["review_state"],
                        tuple(sorted(blocks[str(b)] for b in prov))))  # fmt: skip
    return sorted(out)


def _docs() -> list[str]:
    return [_doc_with_ocr(RX)[0], _doc_with_ocr(VITALS, "vitals_sheet")[0],
            _doc_with_ocr(RX)[0]]  # fmt: skip


def test_batched_extraction_writes_what_single_calls_write(db_env):
    single_docs, batch_docs = _docs(), _docs()
    one = CountingStub()
    for d in single_docs:
        extract_document(d, patient_id=None, client=one, store=MemoryStore())
    many = CountingStub()
    results = extract_documents(batch_docs, client=many, store=MemoryStore())
    assert many.batches == 1 and many.singles == 0  # one model call for three documents
    assert one.singles == 3
    assert all(isinstance(r, dict) and r["facts"] > 0 for r in results)
    for s, b in zip(single_docs, batch_docs, strict=True):
        assert _facts(b) == _facts(s)  # same facts, values and evidence, document by document
    with session_scope() as c:
        for d in batch_docs:
            run = [r for r in repo.get_runs(c, d) if r["stage"] == "extract"][-1]
            assert run["status"] == "ok"  # every document has its own extract run
            assert repo.get_document(c, d)["status"] == "extracted"
