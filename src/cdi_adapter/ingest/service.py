"""S1 ingest & normalize service: ``ingest_bytes`` (ARCHITECTURE §5 S1, steps 1–8).

1. SHA-256 -> if a ``source_document`` with that hash exists, return it (idempotent dedupe) and
   audit a read.
2. Store the original bytes -> ``documents/<sha[:2]>/<sha>/original.<ext>``.
3. Insert ``source_document`` (status ``received``), audit ``create``. Race-safe: a concurrent
   duplicate reuses the existing row.
4. Open ``pipeline_run(stage='ingest')``.
5. ``render_pages`` (PyMuPDF / Pillow / multi-frame TIFF).
6. ``normalize_image`` per page (deskew, denoise, CLAHE); ``preproc`` records what was done.
7. Store ``pages/NNNN.png`` + ``pages/NNNN.thumb.png``; insert ``document_page``.
8. ``status='pages_rendered'``, finish the run, audit. (Queueing the next stage is the caller's job:
   the web-app job model or the Celery chain.)

Failure -> ``pipeline_run`` failed, ``source_document.status='error'`` with the error detail
persisted, and the exception re-raised.

Safe to repeat (backlog E10-S9: a job resumed after its runner died ingests its files again).
One ingest per SHA-256 at a time (a transaction-scoped advisory lock, released by PostgreSQL
if the process dies). So a document found **without pages** was left by an ingest that died
or failed while rendering: its pages are rendered again instead of reusing a document that
has none.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.config import get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.ingest import pages
from cdi_adapter.logging import get_logger
from cdi_adapter.storage import ObjectStore, get_store, original_key, page_key, thumb_key

log = get_logger(__name__)


@dataclass(frozen=True)
class IngestResult:
    document_id: UUID
    sha256: str
    deduplicated: bool
    page_count: int | None


def ingest_bytes(
    raw: bytes,
    filename: str,
    mime_type: str | None = None,
    *,
    source_channel: str = "upload",
    legacy_ref: str | None = None,
    legacy_patient_ref: str | None = None,
    patient_id: UUID | str | None = None,
    actor: str = "system",
    store: ObjectStore | None = None,
) -> IngestResult:
    if not raw:
        raise pages.UnsupportedDocument("empty upload")
    store = store or get_store()
    sha = hashlib.sha256(raw).hexdigest()
    mime = mime_type or pages.sniff_mime(raw, filename)
    ext = pages.MIME_EXT.get(mime) or filename.rsplit(".", 1)[-1].lower()
    with _one_ingest_per_sha(sha):
        return _ingest(raw, filename, mime, ext, sha, store, source_channel=source_channel,
                       legacy_ref=legacy_ref, legacy_patient_ref=legacy_patient_ref,
                       patient_id=patient_id, actor=actor)  # fmt: skip


@contextmanager
def _one_ingest_per_sha(sha: str) -> Iterator[None]:
    with session_scope() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:s, 0))"), {"s": sha})
        yield


def _ingest(raw: bytes, filename: str, mime: str, ext: str, sha: str, store: ObjectStore, *,
            source_channel: str, legacy_ref: str | None, legacy_patient_ref: str | None,
            patient_id: UUID | str | None, actor: str) -> IngestResult:  # fmt: skip
    # 1. dedupe
    with session_scope() as conn:
        existing = repo.find_document_by_sha(conn, sha)
        if existing and existing["page_count"] is not None:
            return _reuse(conn, existing, actor)
    if existing:  # its earlier ingest stopped before the pages were stored: render them now
        return _rerender(existing, raw, mime, store, actor)

    # 2. store the original (idempotent: same key for the same bytes)
    key = original_key(sha, ext)
    store.put_bytes(key, raw, mime)

    # 3. source_document row, race-safe
    with session_scope() as conn:
        doc_id = repo.insert_source_document(
            conn,
            sha256=sha,
            filename=filename,
            mime_type=mime,
            byte_size=len(raw),
            storage_key=key,
            source_channel=source_channel,
            legacy_ref=legacy_ref,
            legacy_patient_ref=legacy_patient_ref,
            patient_id=patient_id,
        )
        if doc_id is None:  # a concurrent upload of the same bytes won the insert
            winner = repo.find_document_by_sha(conn, sha)
            if winner is None:  # pragma: no cover - the conflicting row cannot vanish mid-txn
                raise RuntimeError(f"dedupe conflict on {sha} but no row found")
            return _reuse(conn, winner, actor)
        repo.write_audit(
            conn,
            actor=actor,
            action="create",
            entity="source_document",
            entity_id=doc_id,
            detail={"filename": filename, "sha256": sha, "channel": source_channel},
        )
        # 4. open the run in the same transaction as the row it belongs to
        run_id = repo.start_run(conn, document_id=doc_id, stage="ingest", model_name="pymupdf")

    return _render_finish(doc_id, sha, raw, mime, store, run_id, actor, len(raw))


def _render_finish(doc_id: UUID, sha: str, raw: bytes, mime: str, store: ObjectStore,
                   run_id: UUID, actor: str, size: int, *,
                   reused: bool = False) -> IngestResult:  # fmt: skip
    try:
        page_count = _render_and_store(doc_id, sha, raw, mime, store)
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            repo.set_document_status(
                conn, doc_id, "error", error={"stage": "ingest", "message": str(exc)}
            )
            repo.write_audit(
                conn,
                actor=actor,
                action="update",
                entity="source_document",
                entity_id=doc_id,
                detail={"status": "error", "stage": "ingest"},
            )
        log.error("ingest_failed", document_id=str(doc_id), error=str(exc))
        raise

    # 8. done
    with session_scope() as conn:
        repo.set_page_count(conn, doc_id, page_count)
        repo.set_document_status(conn, doc_id, "pages_rendered")
        repo.finish_run(conn, run_id, metrics={"pages": page_count, "bytes": size})
        repo.write_audit(
            conn,
            actor=actor,
            action="update",
            entity="source_document",
            entity_id=doc_id,
            detail={"status": "pages_rendered", "pages": page_count},
        )
    log.info("ingested", document_id=str(doc_id), pages=page_count)
    return IngestResult(doc_id, sha, reused, page_count)


def _rerender(existing: dict, raw: bytes, mime: str, store: ObjectStore,
              actor: str) -> IngestResult:  # fmt: skip
    doc_id, sha = existing["id"], existing["sha256"]
    log.warning("ingest_rerender", document_id=str(doc_id), status=existing["status"])
    store.put_bytes(existing["storage_key"], raw, mime)
    with session_scope() as conn:
        conn.execute(text("DELETE FROM document_page WHERE document_id = :d"), {"d": doc_id})
        repo.set_document_status(conn, doc_id, "received")
        run_id = repo.start_run(conn, document_id=doc_id, stage="ingest", model_name="pymupdf")
    return _render_finish(doc_id, sha, raw, mime, store, run_id, actor, len(raw), reused=True)


def _reuse(conn, existing: dict, actor: str) -> IngestResult:
    repo.write_audit(
        conn,
        actor=actor,
        action="read",
        entity="source_document",
        entity_id=existing["id"],
        detail={"reason": "dedupe", "sha256": existing["sha256"]},
    )
    log.info("ingest_deduplicated", document_id=str(existing["id"]))
    return IngestResult(existing["id"], existing["sha256"], True, existing["page_count"])


def _render_and_store(doc_id: UUID, sha: str, raw: bytes, mime: str, store: ObjectStore) -> int:
    dpi = get_settings().page_dpi
    rendered = pages.render_pages(raw, mime, dpi)  # 5
    if not rendered:
        raise pages.UnsupportedDocument("document has no pages")
    with session_scope() as conn:
        for n, png in enumerate(rendered, start=1):
            norm, preproc = pages.normalize_image(png)  # 6
            pk, tk = page_key(sha, n), thumb_key(sha, n)
            store.put_bytes(pk, norm, "image/png")  # 7
            store.put_bytes(tk, pages.make_thumbnail(norm), "image/png")
            w, h = pages.image_size(norm)
            repo.insert_document_page(
                conn,
                document_id=doc_id,
                page_no=n,
                storage_key=pk,
                thumb_key=tk,
                width=w,
                height=h,
                dpi=dpi,
                preproc=preproc,
            )
    return len(rendered)
