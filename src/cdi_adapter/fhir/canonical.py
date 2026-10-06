"""FHIR bundle from the canonical ``cn_*`` rows of one review item (ARCHITECTURE §4.5).

``build_bundle(conn, rv_item_id)`` reads what ``promote_review_item`` wrote for that item and
projects it with the same deterministic builders as S9. Everything in ``cn_*`` is governed by
construction, so the bundle asserts all of it; the scan is referenced by its ``s3://`` location.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.config import get_settings
from cdi_adapter.fhir import resources as R
from cdi_adapter.mpi.service import get_patient
from cdi_adapter.terminology.seed import CONDITIONS

_NS = uuid.UUID("0b7f3d2e-9a61-4c1f-8d55-3e2a1c9b7f60")  # canonical bundle id namespace
_ICD10 = {c.code: c.icd10 for c in CONDITIONS if c.icd10}


def _ref(*parts: Any) -> str:
    return f"urn:uuid:{uuid.uuid5(_NS, ':'.join(str(p) for p in parts))}"


def _rows(conn: Connection, table: str, item: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        text(f"SELECT * FROM {table} WHERE rv_item_id = :i ORDER BY recorded_at, id"), {"i": item}
    ).mappings()]  # fmt: skip


def build_bundle(conn: Connection, rv_item_id: UUID | str) -> dict[str, Any]:
    item_id = str(rv_item_id)
    item = conn.execute(text("SELECT * FROM rv_item WHERE id = :i"),
                        {"i": item_id}).mappings().one()  # fmt: skip
    doc = conn.execute(text("SELECT * FROM source_document WHERE id = :d"),
                       {"d": item["source_document_id"]}).mappings().one()  # fmt: skip
    enc_row = conn.execute(text("SELECT * FROM encounter WHERE source_document_id = :d LIMIT 1"),
                           {"d": item["source_document_id"]}).mappings().first()  # fmt: skip
    enc: dict[str, Any] = dict(enc_row) if enc_row else {}
    pat = get_patient(conn, item["patient_id"]) or {}
    now = datetime.now(UTC).isoformat()

    p_ref, o_ref, pr_ref = _ref("patient", item["patient_id"]), _ref("org"), _ref("pract", item_id)
    e_ref, d_ref = _ref("encounter", item_id), _ref("docref", item_id)
    entries: list[tuple[str, dict[str, Any]]] = [
        (p_ref, R.patient(pat, p_ref)),
        (o_ref, R.organization(R.FACILITY_NAME, o_ref)),
        (pr_ref, R.practitioner(None, pr_ref)),
        (e_ref, R.encounter(enc, e_ref, p_ref)),
    ]
    sections: dict[str, list[str]] = {}
    clinical: list[str] = []
    lab_refs: list[str] = []

    def add(key: str, ref: str, res: dict[str, Any]) -> None:
        entries.append((ref, res))
        sections.setdefault(key, []).append(ref)
        clinical.append(ref)

    for r in _rows(conn, "cn_condition", item_id):
        ref = _ref("cn_condition", r["id"])
        add("condition", ref, R.condition({"local_text": r["display"], "code": r["code"],
                                           "code_system": r["code_system"],
                                           "code_display": r["display"],
                                           "icd10": _ICD10.get(r["code"] or ""),
                                           "asserted_time": r["recorded_at"]},
                                          ref, p_ref, e_ref))  # fmt: skip
    for r in _rows(conn, "cn_medication", item_id):
        ref = _ref("cn_medication", r["id"])
        add("medication", ref, R.medication_request(
            {**r, "local_text": r["drug_text"], "asserted_time": r["recorded_at"],
             "code_display": r["drug_text"]}, ref, p_ref, e_ref, pr_ref))  # fmt: skip
    for r in _rows(conn, "cn_lab_result", item_id):
        ref = _ref("cn_lab_result", r["id"])
        add("investigation", ref, R.measured_observation(
            {"fact_type": "lab_result", "local_text": r["test_display"], "code": r["test_code"],
             "code_system": r["test_system"], "code_display": r["test_display"],
             "value_num": r["value_num"], "value_text": r["value_text"],
             "value_unit_ucum": r["unit_ucum"], "ref_range_low": r["ref_range_low"],
             "ref_range_high": r["ref_range_high"], "ref_range_text": r["ref_range_text"],
             "abnormal_flag": r["abnormal_flag"], "effective_time": r["effective_time"]},
            ref, p_ref, e_ref))  # fmt: skip
        lab_refs.append(ref)
    for r in _rows(conn, "cn_vital_sign", item_id):
        ref = _ref("cn_vital_sign", r["id"])
        add("vital_sign", ref, R.measured_observation(
            {"fact_type": "vital_sign", "local_text": r["display"], "code": r["code"],
             "code_system": r["code_system"], "code_display": r["display"],
             "value_num": r["value_num"], "value_text": r["value_text"],
             "value_unit_ucum": r["unit_ucum"],
             "effective_time": r["effective_time"]}, ref, p_ref, e_ref))  # fmt: skip
    for r in _rows(conn, "cn_allergy", item_id):
        ref = _ref("cn_allergy", r["id"])
        add("allergy", ref, R.allergy({"local_text": r["substance"], "code": r["code"],
                                       "code_system": r["code_system"],
                                       "code_display": r["substance"],
                                       "value_text": r["reaction"]}, ref, p_ref))  # fmt: skip
    if lab_refs:
        dr = _ref("labreport", item_id)
        add("investigation", dr, R.diagnostic_report({"text": "Laboratory report"}, dr, p_ref,
                                                      e_ref, lab_refs, None))  # fmt: skip

    entries.append(
        (
            d_ref,
            R.document_reference(
                dict(doc),
                d_ref,
                p_ref,
                data_b64=None,
                url=R.scan_url(dict(doc), get_settings().s3_bucket),
            ),
        )
    )
    sections["document"] = [d_ref]
    prov = _ref("provenance", item_id)
    entries.append(
        (
            prov,
            R.provenance(
                prov,
                clinical + [d_ref],
                d_ref,
                o_ref,
                "canonical promotion (clinician-governed)",
                now,
            ),
        )
    )
    comp_ref = _ref("composition", item_id)
    comp = R.composition(comp_ref, item["doc_type"], p_ref, e_ref, pr_ref, o_ref, sections, [],
                         now, final=True)  # fmt: skip
    b = R.bundle([(comp_ref, comp)] + entries, now)
    b["identifier"]["value"] = str(uuid.uuid5(_NS, f"bundle:{item_id}"))
    return b
