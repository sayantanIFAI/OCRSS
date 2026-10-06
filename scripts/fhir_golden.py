"""Golden FHIR bundles, one per ABDM artifact (backlog E6-S4; ARCHITECTURE §5 S9, §6).

The five document bundles S9 emits (PrescriptionRecord, DiagnosticReportRecord, OPConsultRecord,
DischargeSummaryRecord, WellnessRecord) are built from fixed synthetic inputs through the real
assembly code (``fhir.service.assemble_document_bundle`` -> ``fhir/resources.py``) and kept in
``tests/golden/fhir/``. Any builder change that alters one shows up as a diff, and each golden is
checked by the real HL7 validator against the pinned ABDM IG (E6-S1).

    python scripts/fhir_golden.py --check              # rebuild, compare with the goldens (exit 1)
    python scripts/fhir_golden.py --write              # accept a deliberate change
    python scripts/fhir_golden.py --validate           # real validator: nothing beyond the baseline
    python scripts/fhir_golden.py --validate --ig ndhm.in#6.6.0 --package-home <staging cache>

No real patient data (ARCHITECTURE §11).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cdi_adapter.fhir import resources as R  # noqa: E402
from cdi_adapter.fhir.service import assemble_document_bundle  # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "golden" / "fhir"
ARTIFACTS = ("PrescriptionRecord", "DiagnosticReportRecord", "OPConsultRecord",
             "DischargeSummaryRecord", "WellnessRecord",
             # 2026-09-28: every kind of upload is covered: a scan report (DiagnosticReportImaging
             # inside a DiagnosticReportRecord) and a document with no structured record type
             "DiagnosticReportRecord-Imaging", "HealthDocumentRecord")  # fmt: skip
NOW = "2026-08-12T10:00:00+00:00"
WHEN = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
_NS = uuid.UUID("0b8c3f2e-6a41-4d7e-9c55-2f1e8d7a9b60")
# a 1x1 white PNG: the "scan", inlined as DocumentReference.content.attachment.data
PNG_1X1 = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8ffff3f0005fe02fea7d6a4c500"
    "00000049454e44ae426082")).decode()  # fmt: skip

PATIENT = {"mpi_id": "CFP-2026-000001", "abha_number": "91-1234-5678-9012",
           "abha_address": "anjali.das@abdm", "legacy_mrn": "LEG-7",
           "name": "Anjali Das", "sex": "F", "birth_date": date(1978, 3, 14)}  # fmt: skip


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, ":".join(parts)))


def _fact(n: int, fact_type: str, text: str, **kw: Any) -> dict[str, Any]:
    return {"id": _id("fact", str(n)), "fact_type": fact_type, "local_text": text,
            "code": None, "code_system": None, "code_display": None,
            "review_state": "clinician_confirmed", "is_current": True,
            "asserted_time": WHEN, **kw}  # fmt: skip


def _cond(n: int, text: str, code: str, display: str) -> dict[str, Any]:
    return _fact(n, "condition", text, code=code, code_system=R.SNOMED, code_display=display)


def _lab(n: int, text: str, loinc: str, display: str, value: float, unit: str,
         low: float | None, high: float | None, flag: str | None) -> dict[str, Any]:  # fmt: skip
    return _fact(n, "lab_result", text, code=loinc, code_system=R.LOINC, code_display=display,
                 value_num=value, value_unit_ucum=unit, ref_range_low=low, ref_range_high=high,
                 abnormal_flag=flag, effective_time=WHEN)  # fmt: skip


def _vital(n: int, text: str, loinc: str, display: str, value: float | None, unit: str | None,
           value_text: str | None = None) -> dict[str, Any]:  # fmt: skip
    return _fact(n, "vital_sign", text, code=loinc, code_system=R.LOINC, code_display=display,
                 value_num=value, value_unit_ucum=unit, value_text=value_text,
                 effective_time=WHEN)  # fmt: skip


def _med(n: int, text: str, code: str | None, display: str | None, dose: float, fpd: float,
         days: int) -> dict[str, Any]:  # fmt: skip
    return _fact(n, "medication", text, drug_text=display or text, code=code,
                 code_system=R.SNOMED if code else None, code_display=display, dose_num=dose,
                 dose_unit_ucum="mg", frequency_per_day=fpd, route="oral",
                 duration_days=days)  # fmt: skip


# artifact -> (doc_type, encounter row, facts)
CASES: dict[str, tuple[str, dict[str, Any], list[dict[str, Any]]]] = {
    "PrescriptionRecord": ("prescription", {"class": "AMB", "period_start": WHEN}, [
        _cond(1, "T2DM", "44054006", "Type 2 diabetes mellitus"),
        _cond(2, "HTN", "59621000", "Essential hypertension"),
        _med(3, "Tab Metformin 500 mg 1-0-1 x 30 days", "372567009", "Metformin", 500, 2, 30),
        _med(4, "Tab Amlodipine 5 mg OD x 30 days", None, None, 5, 1, 30),
        _vital(5, "BP", "85354-9", "Blood pressure panel with all children optional", None, None,
               "138/86"),
        _fact(6, "advice", "Low salt diet, walk 30 min daily"),
    ]),
    "DiagnosticReportRecord": ("lab_report", {"class": "AMB", "period_start": WHEN}, [
        _lab(1, "HbA1c", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", 7.8, "%",
             4.0, 5.6, "H"),
        _lab(2, "Fasting Blood Glucose", "1558-6",
             "Fasting glucose [Mass/volume] in Serum or Plasma", 142, "mg/dL", 70, 100, "H"),
        _lab(3, "Serum Creatinine", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
             1.1, "mg/dL", 0.6, 1.3, None),
    ]),
    "OPConsultRecord": ("opd_note", {"class": "AMB", "period_start": WHEN}, [
        _fact(1, "symptom", "Fever for 3 days", code="386661006", code_system=R.SNOMED,
              code_display="Fever"),
        _fact(2, "symptom", "Headache", code="25064002", code_system=R.SNOMED,
              code_display="Headache"),
        _cond(3, "Dengue", "38362002", "Dengue"),
        _fact(4, "allergy", "Penicillin", value_text="Rash"),
        _fact(5, "advice", "Oral fluids, review after 2 days with CBC"),
    ]),
    "DischargeSummaryRecord": ("discharge_summary", {"class": "IMP", "period_start": WHEN}, [
        _cond(1, "Acute appendicitis", "85189001", "Acute appendicitis"),
        _fact(2, "procedure", "Appendicectomy", code="80146002", code_system=R.SNOMED,
              code_display="Appendectomy", effective_time=WHEN),
        _med(3, "Tab Paracetamol 650 mg SOS x 5 days", None, None, 650, 3, 5),
        _fact(4, "advice", "Follow up in surgical OPD after 1 week"),
    ]),
    "WellnessRecord": ("vitals_sheet", {"class": "AMB", "period_start": WHEN}, [
        _vital(1, "Pulse", "8867-4", "Heart rate", 78, "/min"),
        _vital(2, "Temperature", "8310-5", "Body temperature", 98.4, "[degF]"),
        _vital(3, "SpO2", "59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", 98,
               "%"),
        _vital(4, "Weight", "29463-7", "Body weight", 72, "kg"),
        _vital(5, "Height", "8302-2", "Body height", 158, "cm"),
    ]),
    "DiagnosticReportRecord-Imaging": ("radiology", {"class": "AMB", "period_start": WHEN}, [
        _fact(1, "diagnostic_report", "Chest X-ray PA view",
              value_text="Lung fields clear. Cardiac size normal. Impression: normal study."),
    ]),
    "HealthDocumentRecord": ("advice_slip", {"class": "AMB", "period_start": WHEN}, [
        _fact(1, "advice", "Review in OPD after 2 weeks with reports"),
    ]),
}  # fmt: skip


def build(artifact: str) -> dict[str, Any]:
    """The golden bundle for ``artifact``, through the real S9 assembly, deterministic."""
    doc_type, enc, facts = CASES[artifact]
    document_id = _id("document", artifact)
    doc = {"filename": f"{doc_type}_golden.png", "mime_type": "image/png", "byte_size": 68,
           "storage_key": f"documents/golden/{doc_type}/original.png",
           "patient_id": _id("patient")}  # fmt: skip
    enc = {**enc, "id": _id("encounter", artifact)}
    out = assemble_document_bundle(document_id, doc, doc_type, enc, PATIENT, "golden-fixture",
                                   facts, data_b64=PNG_1X1, url=None, now=NOW)  # fmt: skip
    if out["artifact_type"] != artifact.split("-")[0]:  # "<artifact>-<variant>"
        raise AssertionError(f"{doc_type} projected as {out['artifact_type']}, not {artifact}")
    bundle: dict[str, Any] = out["bundle"]
    bundle["id"] = _id("bundle", artifact)  # R.bundle() draws a random id per projection
    return bundle


def path_for(artifact: str) -> Path:
    return GOLDEN_DIR / f"{artifact}.json"


def dumps(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check() -> list[str]:
    """Artifacts whose rebuilt bundle differs from the committed golden."""
    return [a for a in ARTIFACTS
            if not path_for(a).exists()
            or path_for(a).read_text(encoding="utf-8") != dumps(build(a))]  # fmt: skip


BASELINE = GOLDEN_DIR / "validation-baseline.json"
_REF = re.compile(r"urn:uuid:[0-9a-f-]{36}|/\*[A-Za-z]+/[0-9a-f-]{36}\*/|[0-9a-f]{8}-[0-9a-f-]{27}")


def signature(issue: dict[str, str]) -> str:
    """An error, stable across runs: generated ids stripped from its location and message."""
    loc = _REF.sub("<id>", issue.get("location", ""))
    msg = _REF.sub("<id>", issue.get("message", ""))
    return f"{loc} :: {msg}"


def error_signatures(files: dict[str, list[dict[str, str]]]) -> dict[str, list[str]]:
    """``{artifact: sorted error signatures}`` from a validator result keyed by golden path."""
    out = {}
    for a in ARTIFACTS:
        issues = files.get(str(path_for(a)), [])
        out[a] = sorted(signature(i) for i in issues if i["severity"] in ("error", "fatal"))
    return out


def compare(found: dict[str, list[str]], baseline: dict[str, list[str]]
            ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:  # fmt: skip
    """(new errors, fixed errors) per artifact, counting duplicates (multiset difference)."""
    new, fixed = {}, {}
    for a in ARTIFACTS:
        have, known = Counter(found.get(a, [])), Counter(baseline.get(a, []))
        if extra := sorted((have - known).elements()):
            new[a] = extra
        if gone := sorted((known - have).elements()):
            fixed[a] = gone
    return new, fixed


def read_baseline() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(BASELINE.read_text(encoding="utf-8"))
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--validate", action="store_true")
    ap.add_argument("--ig", help="IG package id#version (default: CDI_IG_PACKAGE)")
    ap.add_argument("--package-home", help="validator package home (default: CDI setting)")
    ap.add_argument("--report", type=Path, help="write the validation result as JSON")
    ap.add_argument(
        "--write-baseline",
        action="store_true",
        help="with --validate: record the current errors as the known baseline",
    )
    args = ap.parse_args(argv)

    if args.write:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for a in ARTIFACTS:
            path_for(a).write_text(dumps(build(a)), encoding="utf-8", newline="\n")
            print(f"wrote {path_for(a).relative_to(ROOT)}")
        return 0
    if args.check:
        stale = check()
        for a in stale:
            print(f"DIFF: {a} no longer matches {path_for(a).relative_to(ROOT)}")
        print("golden bundles match" if not stale else "run --write only for a deliberate change")
        return 1 if stale else 0

    from cdi_adapter.fhir.validate_abdm import validate_files

    result = validate_files([path_for(a) for a in ARTIFACTS], ig=args.ig,
                            package_home=args.package_home)  # fmt: skip
    if args.report:
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    found = error_signatures(result["files"])
    if args.write_baseline:
        BASELINE.write_text(json.dumps({
            "_comment": "Known ABDM IG errors in the golden bundles (backlog E6-S4). The target is "
            "an empty list per artifact; a fix shrinks it (--write-baseline), nothing adds to it.",
            "ig": result["ig"], "validator": result["validator"], "errors": found,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")  # fmt: skip
        print(f"baseline: {sum(map(len, found.values()))} known errors -> {BASELINE.name}")
        return 0
    new, fixed = compare(found, read_baseline()["errors"])
    for a in ARTIFACTS:
        state = "NEW ERRORS" if a in new else ("valid" if not found[a] else "known errors only")
        print(f"{'FAIL' if a in new else 'ok  '} {a}: {len(found[a])} error(s), {state}")
        for sig in new.get(a, []):
            print(f"      new: {sig}")
        if a in fixed:
            print(f"      {len(fixed[a])} baseline error(s) fixed: run --write-baseline")
    valid = sum(not found[a] for a in ARTIFACTS)
    print(f"validator {result['validator']} · IG {result['ig']} · {valid}/{len(ARTIFACTS)} fully "
          f"valid · {'no new errors' if not new else 'NEW ERRORS'}")  # fmt: skip
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
