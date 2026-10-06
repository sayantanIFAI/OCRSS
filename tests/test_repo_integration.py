"""Stage DAL queries against real PostgreSQL 16 (no object storage involved)."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import create_engine, text

from cdi_adapter import repo

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine(migrated_db):
    eng = create_engine(migrated_db)
    yield eng
    eng.dispose()


def _doc_with_pages(c, n_pages=2) -> tuple[str, list[str]]:
    sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    doc = repo.insert_source_document(
        c, sha256=sha, filename="t.pdf", mime_type="application/pdf", byte_size=1,
        storage_key=f"documents/{sha[:2]}/{sha}/original.pdf",
    )  # fmt: skip
    pages = [
        repo.insert_document_page(
            c,
            document_id=doc,
            page_no=n,
            storage_key=f"k{n}",
            thumb_key=f"t{n}",
            width=100,
            height=200,
            dpi=200,
            preproc={"skew_deg": 0.0, "steps": ["grayscale"]},
        )  # fmt: skip
        for n in range(1, n_pages + 1)
    ]
    return str(doc), [str(p) for p in pages]


def test_source_document_insert_is_race_safe_on_sha(engine):
    with engine.begin() as c:
        sha = hashlib.sha256(b"same-bytes-" + uuid.uuid4().bytes).hexdigest()
        kw = dict(sha256=sha, filename="a", mime_type="image/png", byte_size=1, storage_key="k")
        first = repo.insert_source_document(c, **kw)
        second = repo.insert_source_document(c, **kw)  # conflict -> None, no exception
        assert first is not None and second is None
        assert repo.find_document_by_sha(c, sha)["id"] == first


def test_pages_round_trip_in_order(engine):
    with engine.begin() as c:
        doc, _ = _doc_with_pages(c, 3)
        repo.set_page_count(c, doc, 3)
        pages = repo.get_pages(c, doc)
        assert [p["page_no"] for p in pages] == [1, 2, 3]
        assert pages[0]["preproc"] == {"skew_deg": 0.0, "steps": ["grayscale"]}
        assert repo.get_document(c, doc)["page_count"] == 3


def test_ocr_blocks_bulk_insert_ordered_read_and_delete(engine):
    with engine.begin() as c:
        doc, (p1, p2) = _doc_with_pages(c, 2)
        run = repo.start_run(c, document_id=doc, stage="ocr", model_name="rapidocr-onnxruntime")
        rows = [
            {"document_id": doc, "page_id": p2, "block_type": "line", "text": "page2-first",
             "bbox": [1, 2, 3, 4], "polygon": None, "ocr_conf": 0.9, "reading_order": 0,
             "engine": "rapidocr", "pipeline_run_id": run},
            {"document_id": doc, "page_id": p1, "block_type": "line", "text": "page1-second",
             "bbox": [5, 60, 70, 80], "polygon": [[5, 60], [70, 60], [70, 80], [5, 80]],
             "ocr_conf": 0.95, "reading_order": 1, "engine": "rapidocr", "pipeline_run_id": run},
            {"document_id": doc, "page_id": p1, "block_type": "line", "text": "page1-first",
             "bbox": [5, 6, 70, 30], "polygon": None, "ocr_conf": 0.99, "reading_order": 0,
             "engine": "rapidocr", "pipeline_run_id": run},
        ]  # fmt: skip
        repo.insert_ocr_blocks(c, rows)
        got = repo.get_ocr_blocks(c, doc)
        assert [b["text"] for b in got] == ["page1-first", "page1-second", "page2-first"]
        assert [b["page_no"] for b in got] == [1, 1, 2]
        assert got[1]["bbox"] == [5, 60, 70, 80] and got[1]["polygon"][2] == [70, 80]
        hits = c.execute(
            text("SELECT count(*) FROM ocr_block WHERE document_id = :d AND "
                 "to_tsvector('simple', text) @@ to_tsquery('simple', 'page1')"), {"d": doc}
        ).scalar_one()  # fmt: skip
        assert hits == 2  # GIN full-text index path works
        assert repo.delete_ocr_blocks_for_document(c, doc) == 3
        assert repo.get_ocr_blocks(c, doc) == []


def test_ocr_block_bbox_must_have_four_ints(engine):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), engine.begin() as c:
        doc, (p1,) = _doc_with_pages(c, 1)
        repo.insert_ocr_blocks(c, [{"document_id": doc, "page_id": p1, "block_type": "line",
                                    "text": "x", "bbox": [1, 2, 3], "polygon": None,
                                    "ocr_conf": 0.9, "reading_order": 0, "engine": "rapidocr",
                                    "pipeline_run_id": None}])  # fmt: skip


def test_latest_classification(engine):
    with engine.begin() as c:
        doc, _ = _doc_with_pages(c, 1)
        assert repo.get_latest_classification(c, doc) is None
        c.execute(
            text("INSERT INTO doc_classification (document_id, doc_type, confidence, created_at) "
                 "VALUES (:d, 'lab_report', 0.6, now() - interval '1 minute'), "
                 "       (:d, 'prescription', 0.95, now())"), {"d": doc}
        )  # fmt: skip
        assert repo.get_latest_classification(c, doc)["doc_type"] == "prescription"
