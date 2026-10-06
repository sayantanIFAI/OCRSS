"""``/monitor`` — the Pipeline Monitor microsite: the processing log as a flow (owner request
2026-09-26, "like an n8n workflow", "a microsite from the main site"). Its own full-screen app
(``monitor_page``), opened from the main site's header, under the same sign-in and roles.

Every upload's documents, newest first, each as the chain of stages it goes through::

    ingest -> ocr -> classify -> extract -> terminology -> validate -> review -> fhir

A stage's state comes from the pipeline's own records: ``pipeline_run`` (status, model, timing,
metrics; the latest run of the stage when a document was processed more than once) and, while a job
is still running, the live per-document state in ``web_job``. ``review`` is the document's review
item, ``fhir`` the persisted bundle when there is one (the live projection is
``GET /api/documents/{id}/bundle``). Read-only; clinical content, so clinicians and admins only.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.catalogue.service import matches_for_facts
from cdi_adapter.db import session_scope
from cdi_adapter.webapp.monitor_page import monitor_page

router = APIRouter()

# "agent": the advisory medication agent (E12-S4), queued once the document is filed for review
RUN_STAGES = ("ingest", "ocr", "classify", "extract", "terminology", "validate", "agent")
FLOW = (*RUN_STAGES, "review", "fhir")
ITEMS_MAX = 100
OCR_BLOCKS_SHOWN = 80
LOW_OCR_CONF = 0.6


def _docs(raw: Any) -> list[dict[str, Any]]:
    return json.loads(raw) if isinstance(raw, str) else (raw or [])


def _live_state(doc: dict[str, Any], stage: str) -> str | None:
    """A stage's state from the running job itself (before or without a ``pipeline_run`` row)."""
    if stage in (doc.get("steps_done") or []):
        return "ok"
    if doc.get("step") == stage:
        return "failed" if doc.get("status") == "error" else "running"
    return None


def _run_states(conn: Connection, document_ids: list[str]) -> dict[str, dict[str, str]]:
    """document id -> {stage: status of its latest run}."""
    if not document_ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (document_id, stage) CAST(document_id AS text) AS d, stage, status "
            "FROM pipeline_run WHERE CAST(document_id AS text) = ANY(:ids) "
            "ORDER BY document_id, stage, started_at DESC"
        ),
        {"ids": document_ids},
    ).mappings()  # fmt: skip
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["d"], {})[r["stage"]] = r["status"]
    return out


def list_items(conn: Connection, limit: int = 40) -> list[dict[str, Any]]:
    """The newest uploads' documents with a state per flow stage."""
    jobs = conn.execute(
        text(
            "SELECT j.id, j.state, j.created_at, j.finished_at, j.created_by, j.docs, "
            "  p.mpi_id, p.name FROM web_job j "
            "LEFT JOIN patient_identity p ON p.id = j.patient_id "
            "ORDER BY j.created_at DESC LIMIT :n"
        ),
        {"n": max(1, min(limit, ITEMS_MAX))},
    ).mappings().all()  # fmt: skip
    ids = [d["document_id"] for j in jobs for d in _docs(j["docs"]) if d.get("document_id")]
    runs = _run_states(conn, ids)
    review = {
        r["d"]: r["status"]
        for r in conn.execute(
            text(
                "SELECT DISTINCT ON (source_document_id) CAST(source_document_id AS text) AS d, "
                "  status FROM rv_item WHERE CAST(source_document_id AS text) = ANY(:ids) "
                "ORDER BY source_document_id, created_at DESC"
            ),
            {"ids": ids},
        ).mappings()
    }  # fmt: skip
    items = []
    for j in jobs:
        for i, d in enumerate(_docs(j["docs"])):
            did = d.get("document_id")
            stages = {}
            for s in RUN_STAGES:
                stages[s] = runs.get(did or "", {}).get(s) or _live_state(d, s) or "pending"
            stages["review"] = "ok" if did in review else "pending"
            items.append({
                "job_id": j["id"], "index": i, "job_state": j["state"],
                "created_at": j["created_at"].isoformat(), "by": j["created_by"],
                "filename": d.get("filename"), "doc_type": d.get("doc_type"),
                "document_id": did, "status": d.get("status"), "step": d.get("step"),
                "facts": d.get("facts", 0), "in_review": d.get("in_review", 0),
                "error": d.get("error"), "patient": j["mpi_id"], "patient_name": j["name"],
                "review_status": review.get(did or ""), "stages": stages,
            })  # fmt: skip
    return items


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _run(conn: Connection, document_id: str, stage: str) -> dict[str, Any] | None:
    runs = conn.execute(
        text("SELECT * FROM pipeline_run WHERE document_id = :d AND stage = :s "
             "ORDER BY started_at DESC"), {"d": document_id, "s": stage}
    ).mappings().all()  # fmt: skip
    if not runs:
        return None
    r = dict(runs[0])
    secs = (r["finished_at"] - r["started_at"]).total_seconds() if r["finished_at"] else None
    return {"status": r["status"], "model": r["model_name"], "model_version": r["model_version"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "seconds": round(secs, 2) if secs is not None else None, "error": r["error"],
            "metrics": r["metrics"], "attempts": len(runs)}  # fmt: skip


def _ingest(conn: Connection, doc: dict[str, Any]) -> dict[str, Any]:
    pages = conn.execute(
        text("SELECT page_no, width, height, dpi, preproc FROM document_page "
             "WHERE document_id = :d ORDER BY page_no"), {"d": doc["id"]}
    ).mappings().all()  # fmt: skip
    return {"filename": doc["filename"], "mime_type": doc["mime_type"],
            "bytes": doc["byte_size"], "sha256": doc["sha256"], "channel": doc["source_channel"],
            "pages": [dict(p) for p in pages], "document_status": doc["status"],
            "document_error": doc["error"]}  # fmt: skip


def _ocr(conn: Connection, document_id: str) -> dict[str, Any]:
    rows = conn.execute(
        text("SELECT p.page_no, b.reading_order, b.text, b.ocr_conf, b.engine "
             "FROM ocr_block b JOIN document_page p ON p.id = b.page_id "
             "WHERE b.document_id = :d ORDER BY p.page_no, b.reading_order"), {"d": document_id}
    ).mappings().all()  # fmt: skip
    confs = [r["ocr_conf"] for r in rows if r["ocr_conf"] is not None]
    return {"blocks": len(rows), "engines": sorted({r["engine"] for r in rows}),
            "mean_confidence": round(sum(confs) / len(confs), 3) if confs else None,
            "low_confidence_blocks": sum(1 for c in confs if c < LOW_OCR_CONF),
            "lines": [dict(r) for r in rows[:OCR_BLOCKS_SHOWN]],
            "lines_shown": min(len(rows), OCR_BLOCKS_SHOWN)}  # fmt: skip


def _facts(conn: Connection, document_id: str) -> list[dict[str, Any]]:
    keep = (
        "id",
        "fact_type",
        "local_text",
        "value_text",
        "value_num",
        "unit_ucum",
        "code_system",
        "code",
        "code_display",
        "code_status",
        "confidence_extract",
        "confidence_ocr",
        "confidence_terminology",
        "confidence_overall",
        "review_state",
        "drug_text",
        "strength_num",
        "strength_unit",
        "dose_num",
        "frequency_per_day",
        "duration_days",
    )
    facts = [f for f in repo.get_facts_for_document(conn, document_id) if f.get("is_current")]
    # the medicine catalogue (Tata 1mg) identification of each medication line
    cat = matches_for_facts(conn, [str(f["id"]) for f in facts if f["fact_type"] == "medication"])
    out = []
    for f in facts:
        row = {k: f.get(k) for k in keep if k in f}
        m = cat.get(str(f["id"]))
        row["catalogue"] = (m["display_name"] + (f" ({m['score']:.2f})" if m.get("score") else "")
                            if m else None)  # fmt: skip
        out.append(row)
    return out


def _agent(conn: Connection, did: str) -> dict[str, Any] | None:
    """The medication agent's latest suggestions and the steps it took (``agent_trace``)."""
    from cdi_adapter.agent.medication import latest_for_document

    got = latest_for_document(conn, did)
    if got["run"] is None:
        return None
    lines = sorted(got["by_fact"].values(), key=lambda x: x.get("line") or 0)
    trace = conn.execute(
        text("SELECT t.seq, t.step, t.tool, t.confidence, t.duration_ms FROM agent_trace t "
             "WHERE t.run_id = (SELECT id FROM pipeline_run WHERE document_id = :d "
             "  AND stage = 'agent' ORDER BY started_at DESC LIMIT 1) ORDER BY t.seq"),
        {"d": did},
    ).mappings().all()  # fmt: skip
    return {
        "lines": [{"read_as": x.get("read_as"), "status": x.get("status"),
                   "product": (x.get("product") or {}).get("name"), "treats": x.get("treats"),
                   "evidence": x.get("evidence"), "confidence": x.get("confidence"),
                   "why": x.get("why"),
                   "rationale": x.get("rationale") or x.get("reason")} for x in lines],
        "trace": [dict(t) for t in trace],
    }  # fmt: skip


def document_flow(conn: Connection, document_id: UUID | str) -> dict[str, Any] | None:
    did = str(document_id)
    row = conn.execute(text("SELECT * FROM source_document WHERE id = :d"),
                       {"d": did}).mappings().first()  # fmt: skip
    if row is None:
        return None
    doc: dict[str, Any] = dict(row)
    live: dict[str, Any] = {}
    job = conn.execute(
        text("SELECT id, state, docs FROM web_job WHERE docs @> CAST(:m AS jsonb) "
             "ORDER BY created_at DESC LIMIT 1"),
        {"m": json.dumps([{"document_id": did}])},
    ).mappings().first()  # fmt: skip
    if job:
        live = next((d for d in _docs(job["docs"]) if d.get("document_id") == did), {})
    facts = _facts(conn, did)
    cls = repo.get_latest_classification(conn, did)
    ext = conn.execute(
        text("SELECT schema_id, model_name, model_version, is_partial, payload, created_at "
             "FROM extraction WHERE document_id = :d ORDER BY created_at DESC LIMIT 1"),
        {"d": did},
    ).mappings().first()  # fmt: skip
    item = conn.execute(
        text("SELECT id, status, assigned_to, claimed_at, decided_at, created_at FROM rv_item "
             "WHERE source_document_id = :d ORDER BY created_at DESC LIMIT 1"), {"d": did}
    ).mappings().first()  # fmt: skip
    review: dict[str, Any] | None = None
    if item:
        states = conn.execute(
            text("SELECT state, count(*) AS n FROM rv_item_element WHERE rv_item_id = :i "
                 "GROUP BY state"), {"i": item["id"]}
        ).mappings().all()  # fmt: skip
        review = {**dict(item), "elements": {s["state"]: s["n"] for s in states}}
    bundle = conn.execute(
        text("SELECT artifact_type, validation_status, status, fhir_version, ig_package, "
             "created_at FROM fhir_bundle WHERE source_document_id = :d "
             "ORDER BY created_at DESC LIMIT 1"), {"d": did}
    ).mappings().first()  # fmt: skip

    validated = _run(conn, did, "validate")
    details: dict[str, Any] = {
        "ingest": _ingest(conn, doc),
        "ocr": _ocr(conn, did),
        "classify": dict(cls) if cls else None,
        "extract": ({**dict(ext), "facts": len(facts)} if ext else None),
        "terminology": {
            "facts": facts,
            "coded": sum(1 for f in facts if f.get("code")),
            "total": len(facts),
        },  # fmt: skip
        "validate": validated["metrics"] if validated else None,
        "agent": _agent(conn, did),
        "review": review,
        "fhir": dict(bundle) if bundle else None,
    }
    stages = []
    for s in FLOW:
        run = _run(conn, did, s) if s in RUN_STAGES else None
        if run:
            state = run["status"]
        elif s in RUN_STAGES:
            state = _live_state(live, s) or "pending"
        elif s == "review":
            state = "ok" if review else "pending"
        else:  # fhir: a persisted bundle, or buildable once the document is validated
            ready = validated is not None and validated["status"] == "ok"
            state = "ok" if bundle else ("ready" if ready else "pending")
        stages.append({"key": s, "state": state, "run": run, "detail": details[s]})
    return _jsonable({
        "document": {"id": did, "filename": doc["filename"], "status": doc["status"],
                     "created_at": doc["created_at"]},
        "job": {"id": job["id"], "state": job["state"], "live": live} if job else None,
        "stages": stages,
    })  # fmt: skip


@router.get("/monitor", response_class=HTMLResponse)
def page() -> str:
    return monitor_page()


@router.get("/api/monitor/items")
def items(limit: int = 40) -> list[dict[str, Any]]:
    with session_scope() as c:
        return list_items(c, limit)


@router.get("/api/monitor/documents/{document_id}")
def document(document_id: UUID) -> dict[str, Any]:
    with session_scope() as c:
        flow = document_flow(c, document_id)
    if flow is None:
        raise HTTPException(404, "document not found")
    return flow
