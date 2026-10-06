"""S9 FHIR projection, gated (ARCHITECTURE §5 S9, §6).

``project_document(document_id)``:

1. Load facts + medication details + patient + encounter; the original scan goes into
   ``DocumentReference.content.attachment`` — base64-inlined, except that scans over 700 KB are
   referenced by URL (§7.2) — the ``s3://`` location of §5 S1.
2. Core actors: ``Patient`` (CareFlow id + ABHA + legacy MRN), ``Organization`` (HFR),
   ``Practitioner`` (HPR), ``Encounter``.
3. **The gate is enforced here** (§6): only governed facts
   (``auto_accepted`` / ``clinician_confirmed`` / ``corrected``) become resources and Composition
   entries. Held facts are returned in ``held_facts`` and reported as ``info`` issues; rejected
   and superseded facts are never projected.
4. ``DocumentReference`` (the scan) + ``Provenance`` (every clinical resource + the scan).
5. ``Composition`` with sections -> ``Bundle(type=document)``.
6. ``_lint`` structural checks.
7. ``status = ready_to_share`` only when fully governed **and** lint-clean; held facts ->
   ``draft`` / ``warning``; structural errors -> ``draft`` / ``error``. Persisted to
   ``fhir_resource`` + ``fhir_bundle`` (replacing this document's previous projection);
   ``pipeline_run(stage='project')``; ``source_document.status='projected'``; audit.

``project_patient(patient_id)`` -> every document for that patient -> its bundle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.config import get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.fhir import resources as R
from cdi_adapter.logging import get_logger
from cdi_adapter.mpi.service import get_patient
from cdi_adapter.storage import ObjectStore, get_store
from cdi_adapter.terminology.seed import CONDITIONS

log = get_logger(__name__)

_NS = uuid.UUID("5d1e9a8e-7c55-4a6b-9f3e-0c2b8d7a6e41")  # CDI FHIR id namespace
_ICD10 = {c.code: c.icd10 for c in CONDITIONS if c.icd10}


def _ref(*parts: Any) -> str:
    return f"urn:uuid:{uuid.uuid5(_NS, ':'.join(str(p) for p in parts))}"


def bundle_hash(b: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(b, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()  # fmt: skip


def _scan(doc: dict[str, Any], store: ObjectStore | None) -> tuple[str | None, str | None]:
    """The scan's bytes (base64) and, for a large one, also its object-store URL. The ABDM
    profiles require the bytes in the bundle (DocumentReference / Binary / Media ``data`` 1..1)."""
    raw = (store or get_store()).get_bytes(doc["storage_key"])
    url = (
        R.scan_url(doc, get_settings().s3_bucket) if doc["byte_size"] > R.INLINE_MAX_BYTES else None
    )
    return base64.b64encode(raw).decode(), url


Parts = tuple[list[tuple[str, dict[str, Any]]], dict[str, list[str]], str]


def document_parts(artifact: str, doc: dict[str, Any], ref: Any, p_ref: str, e_ref: str,
                   pr_ref: str, *, lab_refs: list[str], report_texts: list[str],
                   data_b64: str | None, url: str | None) -> Parts:  # fmt: skip
    """The artifact-specific parts of a document bundle (ABDM 6.5.0), shared by the S9 projection
    and the canonical bundle: the scan (``Binary`` for a PrescriptionRecord, else
    ``DocumentReference``) and, for a DiagnosticReportRecord or any lab results, the report
    (``DiagnosticReportLab`` over the results, else ``DiagnosticReportImaging`` over the scan as
    ``Media``). Returns (entries, section refs, the scan's reference for Provenance)."""
    entries: list[tuple[str, dict[str, Any]]] = []
    sections: dict[str, list[str]] = {}
    if artifact == "PrescriptionRecord" and data_b64 is not None:
        src = ref("binary")
        entries.append((src, R.binary(doc, src, data_b64=data_b64)))
        sections["binary"] = [src]
    else:
        src = ref("docref")
        entries.append((src, R.document_reference(doc, src, p_ref, data_b64=data_b64, url=url)))
        sections["document"] = [src]
    if artifact == "DiagnosticReportRecord" or lab_refs:
        dr, media_refs = ref("labreport"), []
        if not lab_refs and data_b64 is not None:  # an imaging / narrative report: the scan
            m = ref("media")
            entries.append((m, R.media(doc, m, p_ref, data_b64=data_b64)))
            media_refs = [m]
        if lab_refs or media_refs:
            code = {"text": "Laboratory report" if lab_refs else "Diagnostic imaging report"}
            entries.append((dr, R.diagnostic_report(
                code, dr, p_ref, e_ref, lab_refs, "; ".join(report_texts) or None,
                interpreter=pr_ref, media=media_refs)))  # fmt: skip
            sections["report"] = [dr]
    return entries, sections, src


def build_document_bundle(conn: Connection, document_id: str,
                          store: ObjectStore | None = None) -> dict[str, Any]:  # fmt: skip
    doc = repo.get_document(conn, document_id)
    if doc is None:
        raise LookupError(f"document {document_id} not found")
    cls = repo.get_latest_classification(conn, document_id)
    doc_type = cls["doc_type"] if cls else None
    enc_row = conn.execute(
        text("SELECT * FROM encounter WHERE source_document_id = :d LIMIT 1"), {"d": document_id}
    ).mappings().first()  # fmt: skip
    enc: dict[str, Any] = dict(enc_row) if enc_row else {}
    pat = get_patient(conn, doc["patient_id"]) if doc.get("patient_id") else None
    model = conn.execute(
        text("SELECT model_name FROM extraction WHERE document_id = :d "
             "ORDER BY created_at DESC LIMIT 1"), {"d": document_id}
    ).scalar_one_or_none()  # fmt: skip
    facts = [f for f in repo.get_facts_for_document(conn, document_id)
             if f["is_current"] and f["review_state"] != "rejected"]  # fmt: skip
    data_b64, url = _scan(doc, store)
    return assemble_document_bundle(document_id, doc, doc_type, enc, pat, model, facts,
                                    data_b64=data_b64, url=url,
                                    now=datetime.now(UTC).isoformat())  # fmt: skip


def assemble_document_bundle(document_id: str, doc: dict[str, Any], doc_type: str | None,
                             enc: dict[str, Any], pat: dict[str, Any] | None, model: str | None,
                             facts: list[dict[str, Any]], *, data_b64: str | None,
                             url: str | None, now: str) -> dict[str, Any]:  # fmt: skip
    """The S9 document bundle from already-loaded rows: pure, so the same inputs and ``now`` give
    the same bundle (the E6-S4 golden bundles are built through here)."""
    governed = [f for f in facts if f["review_state"] in R.GOVERNED]
    held = [f for f in facts if f["review_state"] in R.HELD]

    p_ref, o_ref, pr_ref = (
        _ref("patient", doc.get("patient_id")),
        _ref("org"),
        _ref("pract", document_id),
    )
    e_ref = _ref("encounter", enc.get("id") or document_id)
    artifact = R.artifact_for(doc_type)[0]
    authored = R._iso(enc.get("period_start")) or now  # a medication line without its own time
    entries: list[tuple[str, dict[str, Any], list[str]]] = []  # (url, resource, fact ids)
    entries.append((p_ref, R.patient(pat or {}, p_ref), []))
    entries.append((o_ref, R.organization(R.FACILITY_NAME, o_ref), []))
    entries.append((pr_ref, R.practitioner(None, pr_ref), []))
    entries.append((e_ref, R.encounter(dict(enc), e_ref, p_ref), []))

    sections: dict[str, list[str]] = {}
    advice: list[str] = []
    lab_refs: list[str] = []
    clinical: list[str] = []
    report_texts: list[str] = []
    report_ids: list[str] = []

    def add(key: str, ref: str, res: dict[str, Any], fact_ids: list[str]) -> None:
        entries.append((ref, res, fact_ids))
        sections.setdefault(key, []).append(ref)
        clinical.append(ref)

    for f in governed:
        fid, ref, t = str(f["id"]), _ref("fact", f["id"]), f["fact_type"]
        if t == "condition":
            add("condition", ref, R.condition({**f, "icd10": _ICD10.get(f.get("code") or "")},
                                              ref, p_ref, e_ref), [fid])  # fmt: skip
        elif t in ("symptom", "finding"):
            add(t, ref, R.exam_observation(f, ref, p_ref, e_ref), [fid])
        elif t == "lab_result":
            add("investigation", ref, R.measured_observation(f, ref, p_ref, e_ref), [fid])
            lab_refs.append(ref)
        elif t == "vital_sign":
            add("vital_sign", ref, R.measured_observation(f, ref, p_ref, e_ref), [fid])
        elif t == "medication":
            add("medication", ref, R.medication_request(f, ref, p_ref, e_ref, pr_ref, authored),
                [fid])  # fmt: skip
        elif t == "procedure":
            add("procedure", ref, R.procedure(f, ref, p_ref, e_ref), [fid])
        elif t == "allergy":
            add("allergy", ref, R.allergy(f, ref, p_ref), [fid])
        elif t == "diagnostic_report":  # the report's own statement: its conclusion
            report_texts.append(str(f.get("value_text") or f["local_text"]))
            report_ids.append(fid)
        elif t == "lab_order":  # a test the prescription advises (owner request 2026-09-28)
            advice.append(advised_test(f["local_text"], f.get("code_display")))
        elif t in ("advice", "referral", "care_plan"):
            advice.append(f["local_text"] if t != "referral" else f"Referred to {f['local_text']}")

    parts, part_secs, src = document_parts(
        artifact, doc, lambda kind: _ref(kind, document_id), p_ref, e_ref, pr_ref,
        lab_refs=lab_refs, report_texts=report_texts, data_b64=data_b64, url=url)  # fmt: skip
    for u, r in parts:
        is_report = u in part_secs.get("report", [])
        entries.append((u, r, report_ids if is_report else []))
        if r["resourceType"] in ("DiagnosticReport", "Media"):
            clinical.append(u)
    for key, refs in part_secs.items():
        sections.setdefault(key, []).extend(refs)
    if part_secs.get("report") and artifact != "DiagnosticReportRecord":
        sections.setdefault("investigation", []).extend(part_secs["report"])
    prov = _ref("provenance", document_id)
    entries.append((prov, R.provenance(prov, clinical + [src], src, o_ref,
                                       f"extractor={model}", now), []))  # fmt: skip

    comp_ref = _ref("composition", document_id)
    comp = R.composition(comp_ref, doc_type, p_ref, e_ref, pr_ref, o_ref, sections, advice,
                         now, final=not held)  # fmt: skip
    b = R.bundle([(comp_ref, comp)] + [(u, r) for u, r, _ in entries], now)
    b["identifier"]["value"] = str(uuid.uuid5(_NS, f"bundle:{document_id}"))

    issues = R._lint(b)
    issues += [{"severity": "info", "code": "held-fact",
                "message": f"{f['fact_type']} '{f['local_text']}' is {f['review_state']}"}
               for f in held]  # fmt: skip
    errors = any(i["severity"] == "error" for i in issues)
    if errors:
        status, vstatus = "draft", "error"
    elif held:
        status, vstatus = "draft", "warning"
    else:
        status, vstatus = "ready_to_share", "valid"
    artifact = R.artifact_for(doc_type)[0]
    return {
        "document_id": str(document_id), "filename": doc["filename"], "doc_type": doc_type,
        "artifact_type": artifact, "status": status, "validation_status": vstatus,
        "issues": issues, "asserted_facts": len(governed),
        "held_facts": [{"id": str(f["id"]), "fact_type": f["fact_type"],
                        "local_text": f["local_text"], "review_state": f["review_state"]}
                       for f in held],
        "bundle": b, "patient_id": doc.get("patient_id"), "encounter_id": enc.get("id"),
        "_entries": [(u, r, ids) for u, r, ids in [(comp_ref, comp, [])] + entries],
    }  # fmt: skip


def _persist(conn: Connection, out: dict[str, Any]) -> None:
    doc_id = out["document_id"]
    conn.execute(text("DELETE FROM fhir_resource WHERE source_document_id = :d"), {"d": doc_id})
    conn.execute(text("DELETE FROM fhir_bundle WHERE source_document_id = :d"), {"d": doc_id})
    issues = json.dumps(out["issues"])
    for url, res, fact_ids in out["_entries"]:
        conn.execute(
            text(
                "INSERT INTO fhir_resource (resource_type, fhir_id, version_id, patient_id, "
                "  encounter_id, source_document_id, profile, resource, derived_from_facts, "
                "  validation_status, validation_issues) "
                "VALUES (:t, :fid, 1, :p, :e, :d, :profile, CAST(:res AS jsonb), "
                "  CAST(:facts AS uuid[]), :vs, CAST(:issues AS jsonb)) "
                "ON CONFLICT (resource_type, fhir_id, version_id) DO UPDATE SET "
                "  resource = EXCLUDED.resource, validation_status = EXCLUDED.validation_status, "
                "  validation_issues = EXCLUDED.validation_issues"
            ),
            {"t": res["resourceType"], "fid": url.split(":")[-1], "p": out["patient_id"],
             "e": out["encounter_id"], "d": doc_id, "profile": res["meta"]["profile"],
             "res": json.dumps(res), "facts": fact_ids, "vs": out["validation_status"],
             "issues": issues},
        )  # fmt: skip
    conn.execute(
        text(
            "INSERT INTO fhir_bundle (patient_id, encounter_id, source_document_id, artifact_type, "
            "  bundle, bundle_hash, ig_package, validation_status, status) "
            "VALUES (:p, :e, :d, :a, CAST(:b AS jsonb), :h, :ig, :vs, :s)"
        ),
        {"p": out["patient_id"], "e": out["encounter_id"], "d": doc_id,
         "a": out["artifact_type"], "b": json.dumps(out["bundle"]),
         "h": bundle_hash(out["bundle"]), "ig": get_settings().ig_package,
         "vs": out["validation_status"], "s": out["status"]},
    )  # fmt: skip


def advised_test(written: str, display: str | None) -> str:
    """An advised test as the record's narrative carries it: as written, with the standard
    name it was coded to when that says more ("Test advised: KFT (Renal function panel ...)").
    ABDM 6.5.0 gives a PrescriptionRecord no place for a ServiceRequest (its one section
    takes MedicationRequest / Binary only), so the test is text there, like the advice."""
    same = (display or "").strip().lower() == written.strip().lower()
    shown = f" ({display})" if display and not same else ""
    return f"Test advised: {written}{shown}"


def project_document(
    document_id: UUID | str,
    *,
    persist: bool = True,
    store: ObjectStore | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    doc_id = str(document_id)
    if not persist:
        with session_scope() as conn:
            out = build_document_bundle(conn, doc_id, store)
        out.pop("_entries")
        return out
    with session_scope() as conn:
        run_id = repo.start_run(conn, document_id=doc_id, stage="project", model_name="resources")
    try:
        with session_scope() as conn:
            out = build_document_bundle(conn, doc_id, store)
            _persist(conn, out)
            repo.set_document_status(conn, doc_id, "projected")
            metrics = {"artifact": out["artifact_type"], "status": out["status"],
                       "asserted": out["asserted_facts"], "held": len(out["held_facts"]),
                       "errors": sum(i["severity"] == "error" for i in out["issues"])}  # fmt: skip
            repo.finish_run(conn, run_id, metrics=metrics)
            repo.write_audit(conn, actor=actor, action="create", entity="fhir_bundle",
                             entity_id=doc_id, detail=metrics)  # fmt: skip
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
        raise
    out.pop("_entries")
    log.info("projected", document_id=doc_id, **metrics)
    return out


def project_patient(patient_id: UUID | str, *, persist: bool = True,
                    store: ObjectStore | None = None) -> dict[str, Any]:  # fmt: skip
    with session_scope() as conn:
        pat = get_patient(conn, patient_id)
        docs = conn.execute(
            text("SELECT id FROM source_document WHERE patient_id = :p ORDER BY created_at"),
            {"p": patient_id},
        ).scalars().all()  # fmt: skip
    bundles = [project_document(d, persist=persist, store=store) for d in docs]
    return {
        "patient": {k: (str(v) if v is not None else None) for k, v in (pat or {}).items()
                    if k in ("id", "mpi_id", "name", "sex", "birth_date", "abha_number",
                             "identity_confidence")},
        "artifact_count": len(bundles),
        "bundles": [{k: b[k] for k in ("document_id", "filename", "doc_type", "artifact_type",
                                       "status", "issues", "held_facts", "asserted_facts",
                                       "bundle")} for b in bundles],
    }  # fmt: skip
