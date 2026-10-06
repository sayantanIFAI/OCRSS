"""S3 OCR service: ``ocr_document`` (ARCHITECTURE §5 S3).

Runs **before** classify: the orchestrator first calls ``ocr_document(doc, force_engine=
"rapidocr")`` so the S2 heuristic reads real OCR text; if S2 then marks the page handwritten it
calls ``ocr_document(doc, force_engine="vlm")``, which deletes and re-inserts the blocks so the
VLM transcription cleanly supersedes the RapidOCR pass. Without ``force_engine`` the engine is
chosen from the classification (``is_handwritten AND CDI_HANDWRITTEN_USES_VLM`` -> VLM).
Idempotent: every run deletes the document's previous blocks first.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from cdi_adapter import repo
from cdi_adapter.config import Settings, get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.logging import get_logger
from cdi_adapter.ml.client import MLClient, get_client
from cdi_adapter.ocr import rapid, vlm_ocr
from cdi_adapter.storage import ObjectStore, get_store

log = get_logger(__name__)

ENGINES = ("rapidocr", "vlm")
_EARLY = ("received", "pages_rendered")  # statuses OCR may advance to 'ocr_done'


def choose_engine(
    force_engine: str | None, classification: dict[str, Any] | None, settings: Settings
) -> str:
    if force_engine is not None:
        if force_engine not in ENGINES:
            raise ValueError(f"force_engine must be one of {ENGINES}")
        return force_engine
    if classification and classification.get("is_handwritten") and settings.handwritten_uses_vlm:
        return "vlm"
    return settings.ocr_engine


def ocr_document(
    document_id: UUID | str,
    *,
    force_engine: str | None = None,
    client: MLClient | None = None,
    store: ObjectStore | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    settings = get_settings()
    store = store or get_store()
    with session_scope() as conn:
        doc = repo.get_document(conn, document_id)
        if doc is None:
            raise LookupError(f"document {document_id} not found")
        pages = repo.get_pages(conn, document_id)
        engine = choose_engine(force_engine, repo.get_latest_classification(conn, document_id),
                               settings)  # fmt: skip
        model = settings.vlm_model_id if engine == "vlm" else "rapidocr-onnxruntime"
        run_id = repo.start_run(conn, document_id=document_id, stage="ocr", model_name=model)

    try:
        per_page: list[tuple[dict[str, Any], list[rapid.OcrLine]]] = []
        vlm = (client or get_client()) if engine == "vlm" else None
        for page in pages:
            png = store.get_bytes(page["storage_key"])
            if vlm is not None:
                lines = vlm_ocr.run_vlm_transcription(png, page["width"], page["height"], vlm)
            else:
                lines = rapid.run_rapidocr(png, min_conf=settings.ocr_min_conf)
            per_page.append((page, lines))

        with session_scope() as conn:
            repo.delete_ocr_blocks_for_document(conn, document_id)
            rows = [
                {
                    "document_id": document_id,
                    "page_id": page["id"],
                    "block_type": ln.block_type,
                    "text": ln.text,
                    "bbox": ln.bbox,
                    "polygon": ln.polygon,
                    "ocr_conf": ln.conf,
                    "reading_order": ln.reading_order,
                    "engine": engine,
                    "pipeline_run_id": run_id,
                }
                for page, lines in per_page
                for ln in lines
            ]
            repo.insert_ocr_blocks(conn, rows)
            confs = [r["ocr_conf"] for r in rows]
            metrics = {
                "engine": engine,
                "pages": len(per_page),
                "blocks": len(rows),
                "mean_conf": round(sum(confs) / len(confs), 4) if confs else None,
            }
            if doc["status"] in _EARLY:
                repo.set_document_status(conn, document_id, "ocr_done")
            repo.finish_run(conn, run_id, metrics=metrics)
            repo.write_audit(conn, actor=actor, action="update", entity="source_document",
                             entity_id=document_id, detail={"stage": "ocr", **metrics})  # fmt: skip
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            repo.set_document_status(conn, document_id, "error",
                                     error={"stage": "ocr", "message": str(exc)})  # fmt: skip
        log.error("ocr_failed", document_id=str(document_id), error=str(exc))
        raise
    log.info("ocr_done", document_id=str(document_id), **metrics)
    return metrics
