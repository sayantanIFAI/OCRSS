"""Celery pipeline for the folder-watch / ingest-API path (ARCHITECTURE §3.1, TECH_STACK §5).

Broker + result backend: Redis (``CDI_REDIS_URL``). Queue ``cdi``. Each stage is a retryable
task; the chain is the five tasks TECH_STACK §5 names: ``ingest_file -> classify_document ->
ocr_document -> extract_document -> bind_document``. This path classifies **before** OCR (the
classifier falls back to a one-off RapidOCR hint, §5 S2), then OCR runs with the engine the
classification picks.

    celery -A cdi_adapter.worker.celery_app worker -Q cdi
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from celery import Celery, chain

from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

_s = get_settings()
celery_app = Celery("cdi", broker=_s.redis_url, backend=_s.redis_url)
celery_app.conf.update(
    task_default_queue="cdi",
    task_acks_late=True,  # a crashed worker's task is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)

MAX_RETRIES = 3
RETRY_BACKOFF = 10  # seconds, doubled per attempt


def _retry(task, exc: Exception) -> Any:
    countdown = RETRY_BACKOFF * (2**task.request.retries)
    raise task.retry(exc=exc, countdown=countdown, max_retries=MAX_RETRIES)


@celery_app.task(bind=True, name="cdi.ingest_file", max_retries=MAX_RETRIES)
def ingest_file(self, path: str, source_channel: str = "folder") -> str:
    from cdi_adapter.ingest.service import ingest_bytes

    p = Path(path)
    try:
        res = ingest_bytes(p.read_bytes(), p.name, source_channel=source_channel)
    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    log.info("folder_ingested", path=path, document_id=str(res.document_id),
             deduplicated=res.deduplicated)  # fmt: skip
    return str(res.document_id)


@celery_app.task(bind=True, name="cdi.classify_document", max_retries=MAX_RETRIES)
def classify_document(self, document_id: str) -> str:
    from cdi_adapter.classify.service import classify_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.ocr_document", max_retries=MAX_RETRIES)
def ocr_document(self, document_id: str) -> str:
    from cdi_adapter.ocr.service import ocr_document as run

    try:
        run(document_id)  # engine chosen from the classification
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.extract_document", max_retries=MAX_RETRIES)
def extract_document(self, document_id: str) -> str:
    from cdi_adapter.extract.service import extract_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.bind_document", max_retries=MAX_RETRIES)
def bind_document(self, document_id: str) -> str:
    from cdi_adapter.terminology.service import bind_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


def pipeline_after_ingest():
    """The stages that follow ingest, as a Celery chain signature."""
    return chain(classify_document.s(), ocr_document.s(), extract_document.s(),
                 bind_document.s())  # fmt: skip


def submit_file(path: str | Path, source_channel: str = "folder"):
    """Queue one file through the whole pipeline."""
    return chain(ingest_file.s(str(path), source_channel), pipeline_after_ingest()).apply_async()


def submit_document(document_id: str):
    """Queue an already-ingested document (the ingest API) through the rest of the pipeline."""
    return chain(classify_document.s(document_id), ocr_document.s(), extract_document.s(),
                 bind_document.s()).apply_async()  # fmt: skip
