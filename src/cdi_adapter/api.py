"""Original ingest-only API (ARCHITECTURE §3.1; TECH_STACK §2: ``uvicorn cdi_adapter.api:app`` on
:8080 in the compose stack). ``POST /ingest`` stores one document and queues the rest of the
pipeline on Celery; the web app (:8888) is the primary path."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from cdi_adapter.ingest.pages import UnsupportedDocument

app = FastAPI(title="cdi-adapter ingest API")


@app.post("/ingest", status_code=202)
async def ingest(file: UploadFile = File(...), legacy_ref: str | None = Form(None),
                 legacy_patient_ref: str | None = Form(None)) -> dict[str, Any]:  # fmt: skip
    from cdi_adapter.ingest.service import ingest_bytes
    from cdi_adapter.worker import submit_document

    raw = await file.read()
    if not raw:
        raise HTTPException(422, "empty file")
    try:
        res = ingest_bytes(raw, file.filename or "upload", file.content_type,
                           source_channel="api", legacy_ref=legacy_ref,
                           legacy_patient_ref=legacy_patient_ref)  # fmt: skip
    except UnsupportedDocument as exc:
        raise HTTPException(415, str(exc)) from exc
    if not res.deduplicated:
        submit_document(str(res.document_id))
    return {"document_id": str(res.document_id), "deduplicated": res.deduplicated,
            "pages": res.page_count}  # fmt: skip


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
