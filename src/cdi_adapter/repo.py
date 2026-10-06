"""Raw-SQL data-access layer (ARCHITECTURE §4: "DAL is raw SQL … no ORM drift").

Every function takes an open :class:`sqlalchemy.Connection` so the caller owns the transaction.
JSON goes in as ``CAST(:x AS jsonb)``; never ``:x::jsonb``, which SQLAlchemy mis-parses as a bind.
This module holds the helpers every stage shares (audit, pipeline runs, document status); each
pipeline stage adds its own queries when it is built.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

# "agent": the advisory medication agent after validation (migration 0010, backlog E12-S4)
PIPELINE_STAGES = ("ingest", "ocr", "classify", "extract", "terminology", "validate", "project",
                   "agent")  # fmt: skip
DOCUMENT_STATUSES = (
    "received",
    "pages_rendered",
    "ocr_done",
    "classified",
    "extracted",
    "normalized",
    "validated",
    "projected",
    "error",
)
AUDIT_ACTIONS = ("create", "update", "read", "delete")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, default=str)


# ------------------------------------------------------------------ audit


def write_audit(
    conn: Connection,
    *,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"audit action must be one of {AUDIT_ACTIONS}, got {action!r}")
    return conn.execute(
        text(
            "INSERT INTO audit_log (actor, action, entity, entity_id, detail) "
            "VALUES (:actor, :action, :entity, :entity_id, CAST(:detail AS jsonb)) RETURNING id"
        ),
        {
            "actor": actor,
            "action": action,
            "entity": entity,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "detail": _json(detail),
        },
    ).scalar_one()


# ------------------------------------------------------------------ pipeline runs


def start_run(
    conn: Connection,
    *,
    document_id: UUID | str | None,
    stage: str,
    model_name: str | None = None,
    model_version: str | None = None,
) -> UUID:
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"stage must be one of {PIPELINE_STAGES}, got {stage!r}")
    return conn.execute(
        text(
            "INSERT INTO pipeline_run (document_id, stage, model_name, model_version) "
            "VALUES (:document_id, :stage, :model_name, :model_version) RETURNING id"
        ),
        {
            "document_id": document_id,
            "stage": stage,
            "model_name": model_name,
            "model_version": model_version,
        },
    ).scalar_one()


def finish_run(conn: Connection, run_id: UUID, *, metrics: dict[str, Any] | None = None) -> None:
    conn.execute(
        text(
            "UPDATE pipeline_run SET status = 'ok', finished_at = now(), "
            "metrics = CAST(:metrics AS jsonb) WHERE id = :id"
        ),
        {"id": run_id, "metrics": _json(metrics)},
    )


def fail_run(conn: Connection, run_id: UUID, *, error: str) -> None:
    conn.execute(
        text(
            "UPDATE pipeline_run SET status = 'failed', finished_at = now(), error = :error "
            "WHERE id = :id"
        ),
        {"id": run_id, "error": error},
    )


def get_runs(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM pipeline_run WHERE document_id = :d ORDER BY started_at, stage"),
        {"d": document_id},
    ).mappings()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ documents


def get_document(conn: Connection, document_id: UUID | str) -> dict[str, Any] | None:
    row = (
        conn.execute(text("SELECT * FROM source_document WHERE id = :id"), {"id": document_id})
        .mappings()
        .first()
    )
    return dict(row) if row else None


def find_document_by_sha(conn: Connection, sha256: str) -> dict[str, Any] | None:
    row = (
        conn.execute(text("SELECT * FROM source_document WHERE sha256 = :s"), {"s": sha256})
        .mappings()
        .first()
    )
    return dict(row) if row else None


def insert_source_document(conn: Connection, **fields: Any) -> UUID | None:
    """Insert a ``source_document``; returns None if another writer already holds this SHA-256.

    ``ON CONFLICT DO NOTHING`` is the race-safe dedupe: the loser reuses the winner's row
    without aborting its own transaction.
    """
    return conn.execute(
        text(
            "INSERT INTO source_document (sha256, filename, mime_type, byte_size, storage_key, "
            "  source_channel, legacy_ref, legacy_patient_ref, patient_id) "
            "VALUES (:sha256, :filename, :mime_type, :byte_size, :storage_key, "
            "  :source_channel, :legacy_ref, :legacy_patient_ref, :patient_id) "
            "ON CONFLICT (sha256) DO NOTHING RETURNING id"
        ),
        {
            "legacy_ref": None,
            "legacy_patient_ref": None,
            "patient_id": None,
            "source_channel": "upload",
            **fields,
        },
    ).scalar_one_or_none()


def insert_document_page(
    conn: Connection,
    *,
    document_id: UUID | str,
    page_no: int,
    storage_key: str,
    thumb_key: str,
    width: int,
    height: int,
    dpi: int,
    preproc: dict[str, Any],
) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO document_page "
            "  (document_id, page_no, storage_key, thumb_key, width, height, dpi, preproc) "
            "VALUES (:document_id, :page_no, :storage_key, :thumb_key, :width, :height, :dpi, "
            "  CAST(:preproc AS jsonb)) RETURNING id"
        ),
        {
            "document_id": document_id,
            "page_no": page_no,
            "storage_key": storage_key,
            "thumb_key": thumb_key,
            "width": width,
            "height": height,
            "dpi": dpi,
            "preproc": _json(preproc),
        },
    ).scalar_one()


def get_pages(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM document_page WHERE document_id = :d ORDER BY page_no"),
        {"d": document_id},
    ).mappings()
    return [dict(r) for r in rows]


def set_page_count(conn: Connection, document_id: UUID | str, page_count: int) -> None:
    conn.execute(
        text("UPDATE source_document SET page_count = :n, updated_at = now() WHERE id = :id"),
        {"id": document_id, "n": page_count},
    )


# ------------------------------------------------------------------ OCR blocks (S3)


def supersede_ocr_observations(conn: Connection, document_id: UUID | str,
                               run_id: UUID | str) -> int:  # fmt: skip
    """A new OCR run of the document points its current observations at itself (backlog E4-S7):
    raw evidence is never deleted, and ``ocr_block`` then shows only the new run's."""
    return conn.execute(
        text("UPDATE ocr_observation SET superseded_by = :r, superseded_at = now() "
             "WHERE document_id = :d AND superseded_by IS NULL"),
        {"d": document_id, "r": run_id},
    ).rowcount  # fmt: skip


# recognizer metadata an observation may carry (E4-S7); none of it is a code or a concept
OBSERVATION_EXTRA = ("crop_hash", "region_kind", "field_domain", "engine_version", "prompt_hash",
                     "token_confidences")  # fmt: skip


def insert_ocr_blocks(conn: Connection, rows: list[dict[str, Any]]) -> None:
    """Bulk insert of raw observations (``ocr_observation``, append-only). Each row: document_id,
    page_id, block_type, text, bbox, polygon, ocr_conf, reading_order, engine, pipeline_run_id,
    and optionally the ``OBSERVATION_EXTRA`` recognizer metadata."""
    if not rows:
        return
    conn.execute(
        text(
            "INSERT INTO ocr_observation (document_id, page_id, block_type, raw_text, bbox, "
            "  polygon, ocr_conf, reading_order, engine, pipeline_run_id, crop_hash, region_kind, "
            "  field_domain, engine_version, prompt_hash, token_confidences) "
            "VALUES (:document_id, :page_id, :block_type, :text, :bbox, CAST(:polygon AS jsonb), "
            "  :ocr_conf, :reading_order, :engine, :pipeline_run_id, :crop_hash, :region_kind, "
            "  :field_domain, :engine_version, :prompt_hash, CAST(:token_confidences AS jsonb))"
        ),
        [
            {
                **{k: None for k in OBSERVATION_EXTRA},
                **r,
                "polygon": json.dumps(r.get("polygon")),
                "token_confidences": (
                    json.dumps(r["token_confidences"]) if r.get("token_confidences") else None
                ),
            }
            for r in rows
        ],
    )


def get_ocr_blocks(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT b.*, p.page_no FROM ocr_block b JOIN document_page p ON p.id = b.page_id "
            "WHERE b.document_id = :d ORDER BY p.page_no, b.reading_order"
        ),
        {"d": document_id},
    ).mappings()
    return [dict(r) for r in rows]


def document_shape(conn: Connection, document_id: UUID | str) -> dict[str, int]:
    """Page and OCR-block counts: how "short" a document is for extract batching (E2-S8)."""
    row = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM document_page WHERE document_id = :d) AS pages, "
            "       (SELECT count(*) FROM ocr_block WHERE document_id = :d) AS blocks"
        ),
        {"d": document_id},
    ).one()
    return {"pages": int(row.pages), "blocks": int(row.blocks)}


def insert_classification(
    conn: Connection,
    *,
    document_id: UUID | str,
    run_id: UUID | None,
    doc_type: str,
    specialty: str | None,
    languages: list[str],
    is_handwritten: bool,
    page_spans: list[Any],
    confidence: float,
    method: str,
    raw: dict[str, Any] | None = None,
    model: str | None = None,
) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO doc_classification (document_id, doc_type, specialty, languages, "
            "  is_handwritten, page_spans, confidence, method, raw, pipeline_run_id) "
            "VALUES (:document_id, :doc_type, :specialty, :languages, :is_handwritten, "
            "  CAST(:page_spans AS jsonb), :confidence, :method, CAST(:raw AS jsonb), :run_id) "
            "RETURNING id"
        ),
        {
            "document_id": document_id,
            "doc_type": doc_type,
            "specialty": specialty,
            "languages": languages,
            "is_handwritten": is_handwritten,
            "page_spans": json.dumps(page_spans),
            "confidence": confidence,
            "method": method,
            "raw": _json({**(raw or {}), "model": model}),
            "run_id": run_id,
        },
    ).scalar_one()


def get_latest_classification(conn: Connection, document_id: UUID | str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT * FROM doc_classification WHERE document_id = :d "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"d": document_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# ------------------------------------------------------------------ extraction + facts (S4)

FACT_COLUMNS = (
    "patient_id", "encounter_id", "fact_type", "local_text", "value_kind", "value_num",
    "value_unit_ucum", "value_text", "value_low_num", "value_high_num", "ref_range_low",
    "ref_range_high", "ref_range_text", "abnormal_flag", "clinical_status", "effective_time",
    "asserted_time", "extraction_id", "source_doc_ids", "confidence_overall", "confidence_ocr",
    "confidence_extract", "review_state", "dedup_key",
)  # fmt: skip
MED_COLUMNS = (
    "drug_text", "form", "strength_num", "strength_unit", "dose_num", "dose_unit_ucum", "route",
    "frequency_code", "frequency_per_day", "duration_days", "prn", "instructions", "intent",
)  # fmt: skip


def insert_extraction(conn: Connection, **f: Any) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO extraction (document_id, doc_type, schema_id, model_name, model_version, "
            "  payload, evidence_map, is_partial, pipeline_run_id) "
            "VALUES (:document_id, :doc_type, :schema_id, :model_name, :model_version, "
            "  CAST(:payload AS jsonb), CAST(:evidence_map AS jsonb), :is_partial, :run_id) "
            "RETURNING id"
        ),
        {**f, "payload": _json(f["payload"]), "evidence_map": _json(f["evidence_map"])},
    ).scalar_one()


def insert_encounter(conn: Connection, **f: Any) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO encounter (patient_id, source_document_id, class, period_start, "
            "  date_precision, doc_type) "
            "VALUES (:patient_id, :document_id, :class_, :period_start, :date_precision, "
            "  :doc_type) RETURNING id"
        ),
        f,
    ).scalar_one()


def insert_clinical_fact(conn: Connection, **f: Any) -> UUID:
    cols = [c for c in FACT_COLUMNS if c in f]
    return conn.execute(
        text(
            f"INSERT INTO clinical_fact ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)}) RETURNING id"
        ),
        {c: f[c] for c in cols},
    ).scalar_one()


def insert_medication_detail(conn: Connection, fact_id: UUID, **f: Any) -> None:
    cols = [c for c in MED_COLUMNS if c in f]
    conn.execute(
        text(
            f"INSERT INTO medication_detail (fact_id, {', '.join(cols)}) "
            f"VALUES (:fact_id, {', '.join(':' + c for c in cols)})"
        ),
        {"fact_id": fact_id, **{c: f[c] for c in cols}},
    )


def insert_fact_provenance(conn: Connection, **f: Any) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO fact_provenance (fact_id, source_doc_id, page_id, ocr_block_ids, "
            "  bbox_union, extracted_text, pipeline_run_ids, model_stack, agent) "
            "VALUES (:fact_id, :source_doc_id, :page_id, CAST(:ocr_block_ids AS uuid[]), "
            "  :bbox_union, :extracted_text, CAST(:pipeline_run_ids AS uuid[]), "
            "  CAST(:model_stack AS jsonb), :agent) RETURNING id"
        ),
        {**f, "model_stack": _json(f.get("model_stack")), "agent": f.get("agent", "system")},
    ).scalar_one()


def get_facts_for_document(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT f.*, m.drug_text, m.strength_num, m.strength_unit, m.dose_num, "
            "  m.dose_unit_ucum, m.route, m.frequency_code, m.frequency_per_day, "
            "  m.duration_days, m.prn, m.form, m.instructions "
            "FROM clinical_fact f LEFT JOIN medication_detail m ON m.fact_id = f.id "
            "WHERE :d = ANY(f.source_doc_ids) ORDER BY f.created_at, f.id"
        ),
        {"d": str(document_id)},
    ).mappings()
    return [dict(r) for r in rows]


def get_provenance(conn: Connection, fact_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM fact_provenance WHERE fact_id = :f ORDER BY recorded_at"),
        {"f": fact_id},
    ).mappings()
    return [dict(r) for r in rows]


def purge_document_facts(conn: Connection, document_id: UUID | str) -> int:
    """Re-extraction supersedes: clear the document's facts / extraction / encounter / aliases
    (+ its review tasks and projections, which were derived from those facts)."""
    d = {"d": str(document_id)}
    from cdi_adapter.training.calibration import preserve_document_decisions

    preserve_document_decisions(conn, document_id)
    n = conn.execute(text("DELETE FROM clinical_fact WHERE :d = ANY(source_doc_ids)"), d).rowcount
    for sql in (
        "DELETE FROM review_task WHERE document_id = :d",
        "DELETE FROM fhir_resource WHERE source_document_id = :d",
        "DELETE FROM fhir_bundle WHERE source_document_id = :d",
        "DELETE FROM extraction WHERE document_id = :d",
        "DELETE FROM encounter WHERE source_document_id = :d",
        "DELETE FROM patient_identity_alias WHERE source_document_id = :d",
    ):
        conn.execute(text(sql), d)
    return n


def set_document_patient(conn: Connection, document_id: UUID | str, patient_id: UUID) -> None:
    conn.execute(
        text("UPDATE source_document SET patient_id = :p, updated_at = now() WHERE id = :d"),
        {"d": document_id, "p": patient_id},
    )


def set_document_status(
    conn: Connection,
    document_id: UUID | str,
    status: str,
    *,
    error: dict[str, Any] | None = None,
) -> None:
    if status not in DOCUMENT_STATUSES:
        raise ValueError(f"status must be one of {DOCUMENT_STATUSES}, got {status!r}")
    conn.execute(
        text(
            "UPDATE source_document SET status = :status, updated_at = now(), "
            "error = CASE WHEN CAST(:error AS text) IS NULL THEN error "
            "             ELSE CAST(:error AS jsonb) END "
            "WHERE id = :id"
        ),
        {"id": document_id, "status": status, "error": json.dumps(error) if error else None},
    )


# ------------------------------------------------------------------ dead letters (E9-S3)

DEAD_LETTER_KINDS = ("web_job", "document", "task")


def write_dead_letter(
    conn: Connection,
    *,
    kind: str,
    ref: str,
    attempts: int,
    error: str | None = None,
    queue: str | None = None,
    task: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Record work that stopped being retried (migration 0022), so it leaves the queue instead of
    blocking it. One row per ``(kind, ref)``: a later failure of the same job or document updates
    that row - its ``first_at`` keeps saying when the trouble started - and clears any earlier
    replay. Returns the row id."""
    if kind not in DEAD_LETTER_KINDS:
        raise ValueError(f"dead-letter kind must be one of {DEAD_LETTER_KINDS}, got {kind!r}")
    params = {"k": kind, "r": str(ref), "q": queue, "t": task, "n": attempts, "e": error,
              "d": _json(detail)}  # fmt: skip
    return conn.execute(
        text(
            "INSERT INTO dead_letter (kind, ref, queue, task, attempts, error, detail) "
            "VALUES (:k, :r, :q, :t, :n, :e, CAST(:d AS jsonb)) "
            "ON CONFLICT (kind, ref) DO UPDATE SET attempts = EXCLUDED.attempts, "
            "error = EXCLUDED.error, queue = EXCLUDED.queue, task = EXCLUDED.task, "
            "detail = EXCLUDED.detail, at = now(), replayed_at = NULL, replayed_by = NULL "
            "RETURNING id"
        ),
        params,
    ).scalar_one()


def dead_letters(conn: Connection, *, kind: str | None = None, open_only: bool = True,
                 limit: int = 100) -> list[dict[str, Any]]:  # fmt: skip
    """What is in the dead-letter queue, newest first; ``open_only`` leaves out the replayed."""
    rows = conn.execute(
        text(
            "SELECT * FROM dead_letter WHERE (CAST(:k AS text) IS NULL OR kind = :k) "
            "AND (NOT :o OR replayed_at IS NULL) ORDER BY at DESC LIMIT :n"
        ),
        {"k": kind, "o": open_only, "n": limit},
    ).mappings()
    return [dict(r) for r in rows]


def mark_replayed(conn: Connection, *, kind: str, ref: str, actor: str) -> bool:
    """Say a dead letter has been sent back through (the operator decided to try it again)."""
    n = conn.execute(
        text(
            "UPDATE dead_letter SET replayed_at = now(), replayed_by = :a "
            "WHERE kind = :k AND ref = :r AND replayed_at IS NULL"
        ),
        {"k": kind, "r": str(ref), "a": actor},
    ).rowcount
    return n == 1
