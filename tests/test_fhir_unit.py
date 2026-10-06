"""E0-S11 unit: resource builders, artifact map, structural lint, Python ABDM conformance."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime

import pytest

from cdi_adapter.fhir import resources as R
from cdi_adapter.fhir.validate_abdm import python_checks, validate

PAT, ENC, PR, ORG = "urn:uuid:p", "urn:uuid:e", "urn:uuid:pr", "urn:uuid:o"


@pytest.mark.parametrize(
    "doc_type,artifact,code",
    [("prescription", "PrescriptionRecord", "440545006"),
     ("lab_report", "DiagnosticReportRecord", "721981007"),
     ("radiology", "DiagnosticReportRecord", "721981007"),
     ("opd_note", "OPConsultRecord", "371530004"), ("referral", "OPConsultRecord", "371530004"),
     ("discharge_summary", "DischargeSummaryRecord", "373942005"),
     ("operative_note", "DischargeSummaryRecord", "373942005"),
     ("vitals_sheet", "WellnessRecord", None),
     ("administrative", "HealthDocumentRecord", "419891008"), (None, "HealthDocumentRecord",
                                                                 "419891008")],
)  # fmt: skip
def test_artifact_map_matches_section_5_s9(doc_type, artifact, code):
    assert R.artifact_for(doc_type)[:2] == (artifact, code)


def test_patient_identifiers_abha_mrn_careflow():
    p = R.patient({"mpi_id": "CFP-2026-000001", "abha_number": "91-1234-5678-9012",
                   "legacy_mrn": "LEG-7", "name": "Anjali Das", "sex": "F",
                   "birth_date": date(1978, 1, 1)}, PAT)  # fmt: skip
    systems = {i["system"]: i["value"] for i in p["identifier"]}
    assert systems == {R.MPI_SYSTEM: "CFP-2026-000001", R.ABHA_SYSTEM: "91-1234-5678-9012",
                       R.LEGACY_MRN_SYSTEM: "LEG-7"}  # fmt: skip
    assert (p["gender"], p["birthDate"]) == ("female", "1978-01-01")
    assert p["meta"]["profile"] == [R.PROFILE_BASE + "Patient"]


def _f(**kw):
    base = {"id": "f1", "local_text": "x", "code": None, "code_system": None,
            "asserted_time": datetime(2026, 8, 12, tzinfo=UTC)}  # fmt: skip
    return {**base, **kw}


def test_condition_is_coded_with_snomed_and_icd10():
    c = R.condition(_f(fact_type="condition", local_text="T2DM", code="44054006",
                       code_system=R.SNOMED, code_display="Type 2 diabetes mellitus",
                       icd10="E11"), "urn:uuid:c", PAT, ENC)  # fmt: skip
    assert [x["code"] for x in c["code"]["coding"]] == ["44054006", "E11"]
    assert c["clinicalStatus"]["coding"][0]["code"] == "active"
    assert c["subject"] == {"reference": PAT}


def test_lab_observation_quantity_range_interpretation():
    o = R.measured_observation(_f(fact_type="lab_result", local_text="HbA1c", code="4548-4",
                                  code_system=R.LOINC, value_num=7.8, value_unit_ucum="%",
                                  ref_range_low=4.0, ref_range_high=5.6, abnormal_flag="H",
                                  effective_time=datetime(2026, 8, 12, tzinfo=UTC)),
                               "urn:uuid:o1", PAT, ENC)  # fmt: skip
    assert o["category"][0]["coding"][0]["code"] == "laboratory"
    assert o["valueQuantity"] == {"value": 7.8, "unit": "%", "system": R.UCUM, "code": "%"}
    assert o["referenceRange"][0]["low"]["value"] == 4.0
    assert o["interpretation"][0]["coding"][0]["code"] == "H"


def test_vital_observation_uses_vital_signs_profile():
    o = R.measured_observation(_f(fact_type="vital_sign", code="8480-6", code_system=R.LOINC,
                                  value_num=138, value_unit_ucum="mm[Hg]"), "urn:uuid:v",
                               PAT, ENC)  # fmt: skip
    assert o["meta"]["profile"] == [R.PROFILE_BASE + "ObservationVitalSigns"]
    assert o["category"][0]["coding"][0]["code"] == "vital-signs"


@pytest.mark.parametrize("fpd,timing", [
    (2, {"frequency": 2, "period": 1, "periodUnit": "d"}),
    (0.143, {"frequency": 1, "period": 7, "periodUnit": "d"}),
    (0.006, {"frequency": 1, "period": 167, "periodUnit": "d"}),
])  # fmt: skip
def test_medication_request_timing(fpd, timing):
    m = R.medication_request(_f(fact_type="medication", local_text="Tab Metformin 500 mg",
                                drug_text="Metformin", code="372567009", code_system=R.SNOMED,
                                frequency_per_day=fpd, route="SC", duration_days=30,
                                dose_num=1, dose_unit_ucum="{tbl}"), "urn:uuid:m", PAT, ENC,
                             PR)  # fmt: skip
    d = m["dosageInstruction"][0]
    assert d["timing"]["repeat"] == timing
    assert d["route"]["coding"][0]["code"] == "34206005"
    assert m["medicationCodeableConcept"]["coding"][0]["code"] == "372567009"
    assert m["dispenseRequest"]["expectedSupplyDuration"]["value"] == 30.0
    assert (m["intent"], m["requester"]) == ("order", {"reference": PR})


def test_allergy_procedure_exam_observation():
    a = R.allergy(_f(fact_type="allergy", local_text="Penicillin", code="373270004",
                     code_system=R.SNOMED, value_text="rash"), "urn:uuid:a", PAT)  # fmt: skip
    assert a["reaction"][0]["manifestation"][0]["text"] == "rash" and a["patient"]["reference"]
    p = R.procedure(_f(fact_type="procedure", local_text="PTCA", value_text="stent to LAD"),
                    "urn:uuid:pp", PAT, ENC)  # fmt: skip
    assert p["status"] == "completed" and p["note"] == [{"text": "stent to LAD"}]
    o = R.exam_observation(_f(fact_type="finding", local_text="LAD 90% stenosis"),
                           "urn:uuid:ob", PAT, ENC)  # fmt: skip
    assert o["valueCodeableConcept"]["text"] == "LAD 90% stenosis"


def _mini_bundle(final=True):
    comp = R.composition("urn:uuid:c", "prescription", PAT, ENC, PR, ORG,
                         {"medication": ["urn:uuid:m"]}, ["low salt diet"],
                         "2026-09-21T00:00:00+00:00", final)  # fmt: skip
    ents = [("urn:uuid:c", comp), (PAT, R.patient({"mpi_id": "CFP-1"}, PAT)),
            (ORG, R.organization("CareFlow", ORG)), (PR, R.practitioner(None, PR)),
            (ENC, R.encounter({"class": "AMB"}, ENC, PAT)),
            ("urn:uuid:m", R.medication_request(_f(fact_type="medication", local_text="Tab A"),
                                                "urn:uuid:m", PAT, ENC, PR))]  # fmt: skip
    return R.bundle(ents, "2026-09-21T00:00:00+00:00")


def test_bundle_shape_and_lint_clean():
    b = _mini_bundle()
    assert b["type"] == "document" and b["entry"][0]["resource"]["resourceType"] == "Composition"
    assert all(e["fullUrl"].startswith("urn:uuid:") for e in b["entry"])
    comp = b["entry"][0]["resource"]
    assert [s["title"] for s in comp["section"]] == ["Medications", "Advisory notes"]
    assert R._lint(b) == []


def test_lint_catches_dangling_ref_missing_value_uncoded_condition_and_order():
    b = _mini_bundle()
    b["entry"][5]["resource"]["subject"]["reference"] = "urn:uuid:ghost"
    b["entry"].append({"fullUrl": "urn:uuid:obs", "resource": {"resourceType": "Observation"}})
    cond = {"resourceType": "Condition", "code": {"text": "rare"}}
    b["entry"].append({"fullUrl": "urn:uuid:cond", "resource": cond})
    codes = {(i["severity"], i["code"]) for i in R._lint(b)}
    assert codes == {("error", "dangling-reference"), ("error", "observation-no-value"),
                     ("error", "condition-uncoded")}  # fmt: skip
    b2 = _mini_bundle()
    b2["entry"].reverse()
    assert ("error", "composition-first") in {(i["severity"], i["code"]) for i in R._lint(b2)}


def test_python_conformance_passes_clean_bundle_and_uses_python_engine():
    out = validate(_mini_bundle(), "PrescriptionRecord")
    assert out == {"engine": "python", "valid": True, "issues": []}


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda b: b.update(type="collection"), "bundle-type"),
        (lambda b: b.pop("timestamp"), "bundle-timestamp"),
        (lambda b: b["entry"][0]["resource"]["meta"].update(profile=["x"]), "composition-profile"),
        (lambda b: b["entry"][0]["resource"].pop("author"), "composition-author"),
        (lambda b: b["entry"][1]["resource"].update(identifier=[]), "patient-identifier"),
        (lambda b: b["entry"][2].update(fullUrl="http://x/Org/1"), "fullurl"),
    ],
)
def test_python_conformance_negative_cases(mutate, code):
    b = copy.deepcopy(_mini_bundle())
    mutate(b)
    issues = python_checks(b, "PrescriptionRecord")
    assert code in {i["code"] for i in issues}
    assert validate(b, "PrescriptionRecord")["valid"] is False


def test_blood_pressure_observation_carries_loinc_components():
    o = R.measured_observation(_f(fact_type="vital_sign", local_text="blood pressure",
                                  code="85354-9", code_system=R.LOINC, value_text="138/86",
                                  value_unit_ucum="mm[Hg]"), "urn:uuid:bp", PAT, ENC)  # fmt: skip
    comps = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"] for c in o["component"]}
    assert comps == {"8480-6": 138.0, "8462-4": 86.0}  # §8.2: BP -> 8480-6 / 8462-4
    assert o["valueString"] == "138/86"  # §5 S9 lint: Observations have a value
    assert (
        R._lint(
            R.bundle([("urn:uuid:c", {"resourceType": "Composition"}), ("urn:uuid:bp", o)], "t")
        )[0]["code"]
        == "dangling-reference"
    )  # only the fake refs fail


def test_scan_url_is_the_s3_location():
    assert R.scan_url({"storage_key": "documents/ab/x/original.pdf"}, "cdi-documents") == (
        "s3://cdi-documents/documents/ab/x/original.pdf"
    )
    assert R.INLINE_MAX_BYTES == 700_000  # §7.2
