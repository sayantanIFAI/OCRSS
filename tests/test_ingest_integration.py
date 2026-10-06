"""E0-S3 integration: ingest_bytes against real PostgreSQL 16 + real MinIO (no substitutes)."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.ingest.service import ingest_bytes
from cdi_adapter.storage import get_store, original_key
from tests.helpers import lined_page, make_pdf, png_bytes

pytestmark = pytest.mark.integration


def _unique_pdf(tag: str) -> bytes:
    return make_pdf([[f"CareFlow test document {tag}", "Rx Tab Metformin 500 mg 1-0-1"], ["p2"]])


def test_ingest_pdf_writes_rows_pages_and_objects(app_env):
    raw = _unique_pdf("happy-path")
    res = ingest_bytes(raw, "rx.pdf")
    assert not res.deduplicated and res.page_count == 2
    store = get_store()
    with session_scope() as c:
        doc = repo.get_document(c, res.document_id)
        pgs = repo.get_pages(c, res.document_id)
        runs = repo.get_runs(c, res.document_id)
        audits = c.execute(
            text("SELECT action FROM audit_log WHERE entity_id = :i ORDER BY id"),
            {"i": str(res.document_id)},
        ).scalars().all()  # fmt: skip
    assert doc["status"] == "pages_rendered" and doc["mime_type"] == "application/pdf"
    assert doc["storage_key"] == original_key(res.sha256, "pdf")
    assert store.get_bytes(doc["storage_key"]) == raw  # original never altered
    assert [p["page_no"] for p in pgs] == [1, 2]
    for p in pgs:
        assert store.exists(p["storage_key"]) and store.exists(p["thumb_key"])
        assert p["dpi"] == 200 and "steps" in p["preproc"]
    assert [(r["stage"], r["status"]) for r in runs] == [("ingest", "ok")]
    assert audits == ["create", "update"]


def test_same_bytes_twice_is_deduplicated(app_env):
    raw = _unique_pdf("dedupe")
    first = ingest_bytes(raw, "a.pdf")
    second = ingest_bytes(raw, "renamed.pdf")
    assert second.deduplicated and second.document_id == first.document_id
    with session_scope() as c:
        n = c.execute(
            text("SELECT count(*) FROM source_document WHERE sha256 = :s"), {"s": first.sha256}
        ).scalar_one()
    assert n == 1


def test_concurrent_duplicate_upload_is_race_safe(app_env):
    raw = png_bytes(lined_page((800, 1000), 12))
    results, errors = [], []

    def go():
        try:
            results.append(ingest_bytes(raw, "scan.png"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({r.document_id for r in results}) == 1
    assert sum(not r.deduplicated for r in results) == 1


def test_corrupt_pdf_marks_error_and_fails_run(app_env):
    raw = b"%PDF-1.4 corrupt " + b"x" * 64
    with pytest.raises(Exception):  # noqa: B017
        ingest_bytes(raw, "broken.pdf")
    with session_scope() as c:
        doc = c.execute(
            text("SELECT * FROM source_document WHERE filename = 'broken.pdf'")
        ).mappings().one()  # fmt: skip
        runs = repo.get_runs(c, doc["id"])
    assert doc["status"] == "error" and doc["error"]["stage"] == "ingest"
    assert runs[0]["status"] == "failed" and runs[0]["error"]
