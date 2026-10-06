"""S4 extract service (ARCHITECTURE §5 S4).

``extract_document`` loads the classification + OCR blocks, builds the ``[bN]`` prompt, sends
page 1 to the VLM through ``client.vlm_json`` (repair + one retry + ``_partial`` fallback) and hands
the payload to ``write_extraction``, which does all the database work in one transaction:

* **Re-extraction supersedes** — ``purge_document_facts`` clears the prior attempt first.
* ``extraction`` row: the payload verbatim + ``evidence_map`` (bN -> ocr_block uuid) + ``_partial``.
* Identity: a ``patient_id`` passed in by the web-app job is used; otherwise the payload's
  ``patient`` block goes through ``resolve_identity``. Every reading is stored as an alias.
* **Existing-patient mismatch guard** — when the job was started against a registry patient,
  the extracted name / DOB must match (``names_match``, ``dob_match``); on a mismatch this
  document's facts are purged and :class:`IdentityMismatch` is raised (the job stops).
* One ``encounter`` per document (``IMP`` for discharge/operative, else ``AMB``), partial-date
  aware.
* Each fact: ``clinical_fact`` (+ ``medication_detail``) + ``fact_provenance`` resolving the
  evidence ids to ``ocr_block`` uuids and a ``bbox_union``. Fused
  ``confidence_overall = 0.5 * (extract_conf + mean OCR conf)``. A fact with no supporting OCR
  block gets no OCR confidence and no ``extracted_text`` — S6 then holds it.
* Unknown ``doc_type`` -> skipped, ``status='normalized'``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from cdi_adapter import repo
from cdi_adapter.dates import parse_date
from cdi_adapter.db import session_scope
from cdi_adapter.extract.facts import ENCOUNTER_DATE_KEYS, FactDraft, facts_from_payload
from cdi_adapter.extract.prompt import (
    SCHEMA_FOR_DOC_TYPE,
    build_extraction_prompt,
    max_tokens_for,
)
from cdi_adapter.logging import get_logger
from cdi_adapter.ml.client import MLClient, get_client, is_partial
from cdi_adapter.mpi import service as mpi
from cdi_adapter.storage import ObjectStore, get_store

log = get_logger(__name__)

INPATIENT_TYPES = ("discharge_summary", "operative_note")


class IdentityMismatch(Exception):
    """The document's patient does not match the registry patient the job was started for."""

    def __init__(self, document_id, expected: dict[str, Any], found: mpi.Candidate):
        super().__init__(f"document {document_id} does not belong to the selected patient")
        self.document_id, self.expected, self.found = document_id, expected, found


def _encounter_date(payload: dict[str, Any]) -> tuple[date | None, str | None]:
    for key in ENCOUNTER_DATE_KEYS:
        parsed = parse_date(payload.get(key))
        if parsed:
            return parsed
    return None, None


def _dedup_key(d: FactDraft) -> str:
    text_key = " ".join(d.local_text.lower().split())
    return f"{d.fact_type}:{text_key}"


def _locate(d: FactDraft, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback evidence: the OCR block that literally contains the fact text (no fuzzing)."""
    needle = " ".join(d.local_text.lower().split())
    if len(needle) < 3:
        return []
    for b in blocks:
        if needle in " ".join(str(b["text"]).lower().split()):
            return [b]
    return []


def _bbox_union(blocks: list[dict[str, Any]]) -> list[int] | None:
    if not blocks:
        return None
    return [min(b["bbox"][0] for b in blocks), min(b["bbox"][1] for b in blocks),
            max(b["bbox"][2] for b in blocks), max(b["bbox"][3] for b in blocks)]  # fmt: skip


def check_mismatch(expected: dict[str, Any], found: mpi.Candidate) -> bool:
    """True when the document clearly belongs to someone else."""
    if found.name and expected.get("name") and not mpi.names_match(found.name, expected["name"]):
        return True
    exp_dob = expected.get("dob") or expected.get("birth_date")
    if isinstance(exp_dob, str):
        exp_dob = (parse_date(exp_dob) or (None,))[0]
    return not mpi.dob_match(exp_dob, found.dob or found.birth_year)


def write_extraction(
    conn: Connection,
    *,
    document_id: UUID | str,
    doc_type: str,
    payload: dict[str, Any],
    block_id_map: dict[str, str],
    blocks: list[dict[str, Any]],
    run_ids: list[UUID | str],
    model_stack: dict[str, Any],
    patient_id: UUID | str | None = None,
    expected_patient: dict[str, Any] | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    repo.purge_document_facts(conn, document_id)
    partial = is_partial(payload)
    extraction_id = repo.insert_extraction(
        conn, document_id=document_id, doc_type=doc_type, schema_id=SCHEMA_FOR_DOC_TYPE[doc_type],
        model_name=model_name, model_version=None, payload=payload, evidence_map=block_id_map,
        is_partial=partial, run_id=run_ids[-1] if run_ids else None,
    )  # fmt: skip

    cand = mpi.candidate_from_payload(payload)
    if expected_patient is not None and check_mismatch(expected_patient, cand):
        repo.purge_document_facts(conn, document_id)
        raise IdentityMismatch(document_id, expected_patient, cand)
    how = "given"
    if patient_id is None:
        patient_id, how = mpi.resolve_identity(conn, cand)
    if cand.name or cand.sex or cand.dob or cand.age_text:
        mpi.add_alias(conn, patient_id, document_id, cand)
    repo.set_document_patient(conn, document_id, UUID(str(patient_id)))

    enc_date, precision = _encounter_date(payload)
    encounter_id = repo.insert_encounter(
        conn, patient_id=patient_id, document_id=document_id,
        class_="IMP" if doc_type in INPATIENT_TYPES else "AMB", period_start=enc_date,
        date_precision=precision, doc_type=doc_type,
    )  # fmt: skip
    effective = datetime.combine(enc_date, time(0), tzinfo=UTC) if enc_date else None

    by_id = {str(b["id"]): b for b in blocks}
    drafts = facts_from_payload(doc_type, payload)
    for d in drafts:
        ev_blocks = [by_id[block_id_map[e]] for e in d.evidence
                     if e in block_id_map and block_id_map[e] in by_id]  # fmt: skip
        if not ev_blocks:
            ev_blocks = _locate(d, blocks)
        page_id = ev_blocks[0]["page_id"] if ev_blocks else None
        same_page = [b for b in ev_blocks if b["page_id"] == page_id]
        ocr_conf = (sum(float(b["ocr_conf"]) for b in ev_blocks) / len(ev_blocks)
                    if ev_blocks else None)  # fmt: skip
        overall = 0.5 * (d.confidence_extract + (ocr_conf or 0.0))
        fact_id = repo.insert_clinical_fact(
            conn, patient_id=patient_id, encounter_id=encounter_id, fact_type=d.fact_type,
            local_text=d.local_text, value_kind=d.value_kind, value_num=d.value_num,
            value_unit_ucum=d.value_unit_ucum, value_text=d.value_text,
            value_low_num=d.value_low_num, value_high_num=d.value_high_num,
            ref_range_low=d.ref_range_low, ref_range_high=d.ref_range_high,
            ref_range_text=d.ref_range_text, abnormal_flag=d.abnormal_flag,
            clinical_status=d.clinical_status,
            effective_time=effective if d.fact_type in ("lab_result", "vital_sign") else None,
            asserted_time=datetime.now(UTC), extraction_id=extraction_id,
            source_doc_ids=[str(document_id)], confidence_overall=round(min(overall, 1.0), 4),
            confidence_ocr=round(ocr_conf, 4) if ocr_conf is not None else None,
            confidence_extract=round(min(max(d.confidence_extract, 0.0), 1.0), 4),
            review_state="pending", dedup_key=_dedup_key(d),
        )  # fmt: skip
        if d.medication is not None:
            repo.insert_medication_detail(conn, fact_id, **d.medication.__dict__)
        repo.insert_fact_provenance(
            conn, fact_id=fact_id, source_doc_id=document_id, page_id=page_id,
            ocr_block_ids=[str(b["id"]) for b in ev_blocks], bbox_union=_bbox_union(same_page),
            extracted_text=" | ".join(str(b["text"]) for b in ev_blocks) or None,
            pipeline_run_ids=[str(r) for r in run_ids], model_stack=model_stack,
        )  # fmt: skip
    return {"extraction_id": extraction_id, "patient_id": patient_id, "identity": how,
            "encounter_id": encounter_id, "facts": len(drafts), "partial": partial}  # fmt: skip


def extract_document(
    document_id: UUID | str,
    *,
    patient_id: UUID | str | None = None,
    expected_patient: dict[str, Any] | None = None,
    client: MLClient | None = None,
    store: ObjectStore | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    with session_scope() as conn:
        cls = repo.get_latest_classification(conn, document_id)
        doc_type = cls["doc_type"] if cls else None
        if doc_type not in SCHEMA_FOR_DOC_TYPE:
            repo.set_document_status(conn, document_id, "normalized")
            log.info("extract_skipped", document_id=str(document_id), doc_type=doc_type)
            return {"skipped": True, "doc_type": doc_type, "facts": 0}
        blocks = repo.get_ocr_blocks(conn, document_id)
        pages = repo.get_pages(conn, document_id)
        ocr_runs = [r["id"] for r in repo.get_runs(conn, document_id)
                    if r["stage"] in ("ocr", "classify") and r["status"] == "ok"]  # fmt: skip
    vlm = client or get_client()
    with session_scope() as conn:
        run_id = repo.start_run(conn, document_id=document_id, stage="extract",
                                model_name=vlm.model)  # fmt: skip
    try:
        prompt, block_map = build_extraction_prompt(doc_type, blocks)
        image = (store or get_store()).get_bytes(pages[0]["storage_key"]) if pages else None
        payload = vlm.vlm_json(image, prompt, SCHEMA_FOR_DOC_TYPE[doc_type],
                               max_tokens=max_tokens_for(doc_type))  # fmt: skip
        model_stack = {"classifier": (cls or {}).get("method"),
                       "ocr": blocks[0]["engine"] if blocks else None, "extractor": vlm.model,
                       "schema": SCHEMA_FOR_DOC_TYPE[doc_type]}  # fmt: skip
        with session_scope() as conn:
            out = write_extraction(
                conn, document_id=document_id, doc_type=doc_type, payload=payload,
                block_id_map=block_map, blocks=blocks, run_ids=[*ocr_runs, run_id],
                model_stack=model_stack, patient_id=patient_id,
                expected_patient=expected_patient, model_name=vlm.model,
            )  # fmt: skip
            repo.set_document_status(conn, document_id, "extracted")
            repo.finish_run(conn, run_id, metrics={"facts": out["facts"],
                                                   "partial": out["partial"]})  # fmt: skip
            repo.write_audit(conn, actor=actor, action="create", entity="extraction",
                             entity_id=out["extraction_id"],
                             detail={"document_id": str(document_id), "facts": out["facts"],
                                     "partial": out["partial"]})  # fmt: skip
    except IdentityMismatch as exc:
        with session_scope() as conn:
            repo.purge_document_facts(conn, document_id)
            repo.fail_run(conn, run_id, error="identity_mismatch")
            repo.write_audit(conn, actor=actor, action="update", entity="source_document",
                             entity_id=document_id,
                             detail={"stage": "extract", "identity_mismatch": True})  # fmt: skip
        log.warning("identity_mismatch", document_id=str(document_id),
                    found=exc.found.as_json())  # fmt: skip
        raise
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            repo.set_document_status(conn, document_id, "error",
                                     error={"stage": "extract", "message": str(exc)})  # fmt: skip
        log.error("extract_failed", document_id=str(document_id), error=str(exc))
        raise
    log.info("extracted", document_id=str(document_id), facts=out["facts"],
             partial=out["partial"])  # fmt: skip
    return out
