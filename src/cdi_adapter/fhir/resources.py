"""Deterministic fact -> FHIR R4 resource builders (ARCHITECTURE §5 S9, §6). Pure dicts, no I/O.

References are ``urn:uuid:`` full-urls (ABDM document-bundle style); profiles are asserted in
``meta.profile`` against ``https://nrces.in/ndhm/fhir/r4/StructureDefinition/...``.

| fact_type         | resource                     | binding                                    |
|-------------------|------------------------------|--------------------------------------------|
| condition         | Condition                    | SNOMED CT + clinical/verification status   |
| symptom / finding | Observation (exam)           | SNOMED CT                                  |
| lab_result        | Observation (laboratory)     | LOINC + UCUM valueQuantity, referenceRange |
| vital_sign        | Observation (vital-signs)    | LOINC + UCUM                               |
| medication        | MedicationRequest            | SNOMED drug, dosageInstruction, duration   |
| procedure         | Procedure                    | SNOMED CT, performedDateTime, note         |
| allergy           | AllergyIntolerance           | SNOMED substance + reaction manifestation  |
| diagnostic_report | DiagnosticReport             | result[] -> lab Observations, conclusion   |
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

from cdi_adapter.terminology.seed import ICD10, LOINC, ROUTE_SCT, SNOMED

PROFILE_BASE = "https://nrces.in/ndhm/fhir/r4/StructureDefinition/"
UCUM = "http://unitsofmeasure.org"
ABHA_SYSTEM = "https://healthid.ndhm.gov.in"
ABHA_ADDRESS_SYSTEM = "https://healthid.ndhm.gov.in/address"
MPI_SYSTEM = "https://careflow.clinic/mpi"
LEGACY_MRN_SYSTEM = "urn:cdi:legacy-mrn"
BUNDLE_ID_SYSTEM = "urn:cdi:bundle"
FACILITY_NAME = "CareFlow Polyclinic"  # ARCHITECTURE §7 branding
# §7.2: "scans over 700 KB are referenced by URL rather than base64-inlined"
INLINE_MAX_BYTES = 700_000


def scan_url(doc: dict[str, Any], bucket: str) -> str:
    """The original scan's object-store location (§5 S1 key layout)."""
    return f"s3://{bucket}/{doc['storage_key']}"


# doc_type -> (artifact profile, Composition.type SNOMED code, title)
ARTIFACT: dict[str, tuple[str, str | None, str]] = {
    "prescription": ("PrescriptionRecord", "440545006", "Prescription record"),
    "lab_report": ("DiagnosticReportRecord", "721981007", "Diagnostic studies report"),
    "radiology": ("DiagnosticReportRecord", "721981007", "Diagnostic studies report"),
    "opd_note": ("OPConsultRecord", "371530004", "Clinical consultation report"),
    "referral": ("OPConsultRecord", "371530004", "Clinical consultation report"),
    "discharge_summary": ("DischargeSummaryRecord", "373942005", "Discharge summary"),
    "operative_note": ("DischargeSummaryRecord", "373942005", "Discharge summary"),
    "vitals_sheet": ("WellnessRecord", None, "Wellness Record"),
}
DEFAULT_ARTIFACT = ("HealthDocumentRecord", "419891008", "Record artifact")

GOVERNED = ("auto_accepted", "clinician_confirmed", "corrected")
HELD = ("pending", "in_review")

_OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
_COND_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
_COND_VERIFY = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
_ALLERGY_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
_ALLERGY_VERIFY = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
_INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
_GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown"}


def artifact_for(doc_type: str | None) -> tuple[str, str | None, str]:
    return ARTIFACT.get(doc_type or "", DEFAULT_ARTIFACT)


def new_ref() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _meta(profile: str) -> dict[str, Any]:
    return {"profile": [PROFILE_BASE + profile]}


def _iso(x: Any) -> str | None:
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, date):
        return x.isoformat()
    return str(x) if x else None


def codeable(fact: dict[str, Any], *, text_col: str = "local_text") -> dict[str, Any]:
    cc: dict[str, Any] = {"text": fact.get(text_col) or fact.get("code_display") or ""}
    if fact.get("code") and fact.get("code_system"):
        cc["coding"] = [{"system": fact["code_system"], "code": fact["code"],
                         "display": fact.get("code_display") or cc["text"]}]  # fmt: skip
    return cc


def _quantity(value: Any, unit: str | None) -> dict[str, Any]:
    q: dict[str, Any] = {"value": float(value)}
    if unit:
        q.update({"unit": unit, "system": UCUM, "code": unit})
    return q


# ------------------------------------------------------------------ actors


def patient(p: dict[str, Any], ref: str) -> dict[str, Any]:
    ids = []
    if p.get("mpi_id"):
        ids.append({"use": "usual", "system": MPI_SYSTEM, "value": p["mpi_id"]})
    if p.get("abha_number"):
        ids.append({"use": "official", "system": ABHA_SYSTEM, "value": p["abha_number"],
                    "type": {"text": "ABHA number"}})  # fmt: skip
    if p.get("abha_address"):
        ids.append({"system": ABHA_ADDRESS_SYSTEM, "value": p["abha_address"],
                    "type": {"text": "ABHA address"}})  # fmt: skip
    if p.get("legacy_mrn"):
        ids.append({"system": LEGACY_MRN_SYSTEM, "value": p["legacy_mrn"],
                    "type": {"text": "Legacy MRN"}})  # fmt: skip
    res: dict[str, Any] = {"resourceType": "Patient", "id": ref.split(":")[-1],
                           "meta": _meta("Patient"), "identifier": ids}  # fmt: skip
    if p.get("name"):
        res["name"] = [{"text": p["name"]}]
    if p.get("sex") in _GENDER:
        res["gender"] = _GENDER[p["sex"]]
    if p.get("birth_date"):
        res["birthDate"] = _iso(p["birth_date"])
    return res


def organization(name: str, ref: str) -> dict[str, Any]:
    # HFR id: backlog E8-S1 registers the facility and adds the identifier
    return {"resourceType": "Organization", "id": ref.split(":")[-1],
            "meta": _meta("Organization"), "name": name}  # fmt: skip


def practitioner(name: str | None, ref: str) -> dict[str, Any]:
    # HPR id: backlog E8-S1
    return {"resourceType": "Practitioner", "id": ref.split(":")[-1],
            "meta": _meta("Practitioner"),
            "name": [{"text": name or "Attending clinician"}]}  # fmt: skip


def encounter(enc: dict[str, Any], ref: str, patient_ref: str) -> dict[str, Any]:
    cls = enc.get("class") or "AMB"
    res: dict[str, Any] = {
        "resourceType": "Encounter", "id": ref.split(":")[-1], "meta": _meta("Encounter"),
        "status": "finished",
        "class": {"system": _ACT_CODE, "code": cls,
                  "display": "inpatient encounter" if cls == "IMP" else "ambulatory"},
        "subject": {"reference": patient_ref},
    }  # fmt: skip
    if enc.get("period_start"):
        res["period"] = {"start": _iso(enc["period_start"])}
    return res


# ------------------------------------------------------------------ clinical facts


def condition(f, ref, pat, enc) -> dict[str, Any]:
    cc = codeable(f)
    if f.get("icd10"):
        cc.setdefault("coding", []).append({"system": ICD10, "code": f["icd10"]})
    return {
        "resourceType": "Condition", "id": ref.split(":")[-1], "meta": _meta("Condition"),
        "clinicalStatus": {"coding": [{"system": _COND_CLINICAL, "code": "active"}]},
        "verificationStatus": {"coding": [{"system": _COND_VERIFY, "code": "confirmed"}]},
        "code": cc, "subject": {"reference": pat}, "encounter": {"reference": enc},
        "recordedDate": _iso(f.get("asserted_time")),
    }  # fmt: skip


def exam_observation(f, ref, pat, enc) -> dict[str, Any]:
    kind = "Clinical finding" if f["fact_type"] == "finding" else "Symptom"
    return {
        "resourceType": "Observation", "id": ref.split(":")[-1], "meta": _meta("Observation"),
        "status": "final",
        "category": [{"coding": [{"system": _OBS_CATEGORY, "code": "exam"}]}],
        "code": {"coding": [{"system": SNOMED, "code": "404684003",
                             "display": "Clinical finding"}], "text": kind},
        "subject": {"reference": pat}, "encounter": {"reference": enc},
        "valueCodeableConcept": codeable(f),
    }  # fmt: skip


def measured_observation(f, ref, pat, enc) -> dict[str, Any]:
    lab = f["fact_type"] == "lab_result"
    res: dict[str, Any] = {
        "resourceType": "Observation", "id": ref.split(":")[-1],
        "meta": _meta("Observation" if lab else "ObservationVitalSigns"), "status": "final",
        "category": [{"coding": [{"system": _OBS_CATEGORY,
                                  "code": "laboratory" if lab else "vital-signs"}]}],
        "code": codeable(f), "subject": {"reference": pat}, "encounter": {"reference": enc},
    }  # fmt: skip
    if f.get("effective_time"):
        res["effectiveDateTime"] = _iso(f["effective_time"])
    if f.get("value_num") is not None:
        res["valueQuantity"] = _quantity(f["value_num"], f.get("value_unit_ucum"))
    elif f.get("value_text"):
        res["valueString"] = f["value_text"]
    bp = re.match(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*$", f.get("value_text") or "")
    if not lab and bp:  # blood pressure: LOINC 8480-6 / 8462-4 components (§8.2)
        res["component"] = [
            {"code": {"coding": [{"system": LOINC, "code": code, "display": display}]},
             "valueQuantity": _quantity(value, "mm[Hg]")}
            for code, display, value in (("8480-6", "Systolic blood pressure", bp.group(1)),
                                         ("8462-4", "Diastolic blood pressure", bp.group(2)))
        ]  # fmt: skip
    rr: dict[str, Any] = {}
    if f.get("ref_range_low") is not None:
        rr["low"] = _quantity(f["ref_range_low"], f.get("value_unit_ucum"))
    if f.get("ref_range_high") is not None:
        rr["high"] = _quantity(f["ref_range_high"], f.get("value_unit_ucum"))
    if f.get("ref_range_text"):
        rr["text"] = f["ref_range_text"]
    if rr:
        res["referenceRange"] = [rr]
    if f.get("abnormal_flag") in ("H", "L"):
        res["interpretation"] = [{"coding": [{"system": _INTERP, "code": f["abnormal_flag"]}]}]
    return res


def _timing(fpd: float | None) -> dict[str, Any] | None:
    if not fpd:
        return None
    if fpd >= 1:
        return {"repeat": {"frequency": int(round(fpd)), "period": 1, "periodUnit": "d"}}
    return {"repeat": {"frequency": 1, "period": round(1 / fpd), "periodUnit": "d"}}


def medication_request(f, ref, pat, enc, practitioner_ref) -> dict[str, Any]:
    med = codeable({**f, "local_text": f.get("drug_text") or f.get("local_text")})
    dosage: dict[str, Any] = {"text": " ".join(str(x) for x in (
        f.get("local_text"), f.get("frequency_code"), f.get("instructions")) if x)}  # fmt: skip
    if t := _timing(f.get("frequency_per_day")):
        dosage["timing"] = t
    if f.get("prn"):
        dosage["asNeededBoolean"] = True
    route = ROUTE_SCT.get(str(f.get("route") or "").lower())
    if route:
        dosage["route"] = {"coding": [{"system": SNOMED, "code": route[0], "display": route[1]}]}
    elif f.get("route"):
        dosage["route"] = {"text": f["route"]}
    if f.get("dose_num") is not None:
        dosage["doseAndRate"] = [
            {"doseQuantity": _quantity(f["dose_num"], f.get("dose_unit_ucum"))}
        ]
    res: dict[str, Any] = {
        "resourceType": "MedicationRequest", "id": ref.split(":")[-1],
        "meta": _meta("MedicationRequest"), "status": "active", "intent": "order",
        "medicationCodeableConcept": med, "subject": {"reference": pat},
        "encounter": {"reference": enc}, "authoredOn": _iso(f.get("asserted_time")),
        "requester": {"reference": practitioner_ref}, "dosageInstruction": [dosage],
    }  # fmt: skip
    if f.get("duration_days"):
        supply = {"value": float(f["duration_days"]), "unit": "days", "system": UCUM,
                  "code": "d"}  # fmt: skip
        res["dispenseRequest"] = {"expectedSupplyDuration": supply}
    return res


def procedure(f, ref, pat, enc) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Procedure", "id": ref.split(":")[-1], "meta": _meta("Procedure"),
        "status": "completed", "code": codeable(f), "subject": {"reference": pat},
        "encounter": {"reference": enc},
    }  # fmt: skip
    if f.get("effective_time"):
        res["performedDateTime"] = _iso(f["effective_time"])
    if f.get("value_text"):
        res["note"] = [{"text": f["value_text"]}]
    return res


def allergy(f, ref, pat) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "AllergyIntolerance", "id": ref.split(":")[-1],
        "meta": _meta("AllergyIntolerance"),
        "clinicalStatus": {"coding": [{"system": _ALLERGY_CLINICAL, "code": "active"}]},
        "verificationStatus": {"coding": [{"system": _ALLERGY_VERIFY, "code": "confirmed"}]},
        "code": codeable(f), "patient": {"reference": pat},
    }  # fmt: skip
    if f.get("value_text"):
        res["reaction"] = [{"manifestation": [{"text": f["value_text"]}]}]
    return res


def diagnostic_report(code: dict[str, Any], ref, pat, enc, results: list[str],
                      conclusion: str | None) -> dict[str, Any]:  # fmt: skip
    res: dict[str, Any] = {
        "resourceType": "DiagnosticReport", "id": ref.split(":")[-1],
        "meta": _meta("DiagnosticReportLab" if results else "DiagnosticReportImaging"),
        "status": "final", "code": code, "subject": {"reference": pat},
        "encounter": {"reference": enc}, "result": [{"reference": r} for r in results],
    }  # fmt: skip
    if conclusion:
        res["conclusion"] = conclusion
    return res


def document_reference(doc: dict[str, Any], ref, pat, *, data_b64: str | None,
                       url: str | None) -> dict[str, Any]:  # fmt: skip
    att: dict[str, Any] = {"contentType": doc["mime_type"], "title": doc["filename"]}
    if data_b64 is not None:
        att["data"] = data_b64
    elif url:
        att["url"] = url
    return {
        "resourceType": "DocumentReference", "id": ref.split(":")[-1],
        "meta": _meta("DocumentReference"), "status": "current",
        "type": {"text": "Scanned source document"}, "subject": {"reference": pat},
        "content": [{"attachment": att}],
    }  # fmt: skip


def provenance(ref, targets: list[str], doc_ref: str, org_ref: str,
               model_stack: str, recorded: str) -> dict[str, Any]:  # fmt: skip
    return {
        "resourceType": "Provenance", "id": ref.split(":")[-1], "meta": _meta("Provenance"),
        "target": [{"reference": t} for t in targets], "recorded": recorded,
        "agent": [{"type": {"text": "assembler"}, "who": {"reference": org_ref},
                   "onBehalfOf": {"reference": org_ref}}],
        "entity": [{"role": "source", "what": {"reference": doc_ref}}],
        "reason": [{"text": f"Generated by CDI-Adapter from the scan; model stack: {model_stack}"}],
    }  # fmt: skip


# ------------------------------------------------------------------ composition + bundle

SECTION_ORDER = (
    ("Chief complaints", "symptom"), ("Diagnosis", "condition"),
    ("Investigations", "investigation"), ("Medications", "medication"),
    ("Procedures", "procedure"), ("Allergies", "allergy"), ("Vital signs", "vital_sign"),
    ("Clinical findings", "finding"), ("Document reference", "document"),
    ("Advisory notes", "advice"),
)  # fmt: skip


def composition(ref, doc_type, pat, enc, author, custodian, sections: dict[str, list[str]],
                advice: list[str], date_iso: str, final: bool) -> dict[str, Any]:  # fmt: skip
    profile, code, title = artifact_for(doc_type)
    ctype: dict[str, Any] = {"text": title}
    if code:
        ctype["coding"] = [{"system": SNOMED, "code": code, "display": title}]
    secs: list[dict[str, Any]] = []
    for sec_title, key in SECTION_ORDER:
        if key == "advice":
            if advice:
                items = "".join(f"<li>{a}</li>" for a in advice)
                div = f'<div xmlns="http://www.w3.org/1999/xhtml"><ul>{items}</ul></div>'
                secs.append({"title": sec_title, "text": {"status": "generated", "div": div}})
            continue
        if sections.get(key):
            secs.append({"title": sec_title, "entry": [{"reference": r} for r in sections[key]]})
    return {
        "resourceType": "Composition", "id": ref.split(":")[-1], "meta": _meta(profile),
        "status": "final" if final else "preliminary", "type": ctype,
        "subject": {"reference": pat}, "encounter": {"reference": enc}, "date": date_iso,
        "author": [{"reference": author}], "custodian": {"reference": custodian},
        "title": title, "section": secs,
    }  # fmt: skip


def bundle(entries: list[tuple[str, dict[str, Any]]], timestamp: str) -> dict[str, Any]:
    return {
        "resourceType": "Bundle", "id": str(uuid.uuid4()), "meta": _meta("DocumentBundle"),
        "identifier": {"system": BUNDLE_ID_SYSTEM, "value": str(uuid.uuid4())},
        "type": "document", "timestamp": timestamp,
        "entry": [{"fullUrl": url, "resource": res} for url, res in entries],
    }  # fmt: skip


# ------------------------------------------------------------------ structural lint


def _refs(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                out.append(v)
            else:
                out += _refs(v)
    elif isinstance(node, list):
        for v in node:
            out += _refs(v)
    return out


def _lint(b: dict[str, Any]) -> list[dict[str, str]]:
    """Structural checks: Composition first; no dangling ``urn:uuid:`` references; Observations
    have a value; Conditions coded."""
    issues: list[dict[str, str]] = []
    entries = b.get("entry") or []
    if not entries or entries[0]["resource"].get("resourceType") != "Composition":
        issues.append({"severity": "error", "code": "composition-first",
                       "message": "first entry must be the Composition"})  # fmt: skip
    urls = {e["fullUrl"] for e in entries}
    for e in entries:
        for r in _refs(e["resource"]):
            if r.startswith("urn:uuid:") and r not in urls:
                issues.append({"severity": "error", "code": "dangling-reference",
                               "message": f"{e['fullUrl']} -> {r}"})  # fmt: skip
        res = e["resource"]
        if res["resourceType"] == "Observation" and not any(k.startswith("value") for k in res):
            issues.append({"severity": "error", "code": "observation-no-value",
                           "message": e["fullUrl"]})  # fmt: skip
        if res["resourceType"] == "Condition" and not res.get("code", {}).get("coding"):
            msg = f"{e['fullUrl']} {res.get('code', {}).get('text')}"
            issues.append({"severity": "error", "code": "condition-uncoded", "message": msg})
    return issues


LOINC_SYSTEM = LOINC  # re-exported for callers building DiagnosticReport codes
