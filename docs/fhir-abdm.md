# FHIR output: valid ABDM records for every kind of upload

Owner request (2026-09-28): "if I upload a new image, build the FHIR format correctly, with a
valid format taken from the official source, for all kinds of uploads (prescriptions, scans,
diagnosis …)".

**Standard:** the ABDM FHIR Implementation Guide from NRCeS, `ndhm.in#6.5.0`
(<https://nrces.in/ndhm/fhir/r4/>, FHIR R4 4.0.1), the current release (checked 2026-09-28). The
package is pinned by sha256 in `infra/runpod/versions.env` and checked by the official HL7 FHIR
validator 6.10.4, offline (`fhir/validate_abdm.py`, backlog E6-S1).

## Which upload becomes which record

| Upload (classified `doc_type`) | ABDM record (Composition profile) | What the one-or-more sections hold |
|---|---|---|
| prescription | **PrescriptionRecord** | ONE section: every `MedicationRequest` + the scan as `Binary`; the written advice as the section's narrative |
| lab report | **DiagnosticReportRecord** | ONE section: a `DiagnosticReportLab` (results → lab `Observation`s) + the scan as `DocumentReference` |
| radiology / scan report | **DiagnosticReportRecord** | ONE section: a `DiagnosticReportImaging` (the scan as `Media`, the report's impression as its conclusion) + `DocumentReference` |
| OPD note, referral | **OPConsultRecord** | complaints, diagnoses, findings, vitals, medications, allergies, procedures, the scan |
| discharge summary, operative note | **DischargeSummaryRecord** | as OPConsult, inpatient encounter |
| vitals sheet | **WellnessRecord** | vital signs, the scan |
| anything else (advice slip, admin page …) | **HealthDocumentRecord** | ONE section: the scan as `DocumentReference` |

## The rules each record now follows (all taken from the IG's StructureDefinitions)

* `Bundle.meta.versionId` (DocumentBundle).
* Patient, Organization and Practitioner **identifiers carry a coded type**
  (`ndhm-identifier-type-code`): MR for the CareFlow MPI id, ABHA for the ABHA number/address,
  "Provider number" for the facility (its HFR id once registered, backlog E8-S1), "Other
  identifier" for the practitioner until the HPR id is known. No registration number is guessed.
* The single-section records have exactly one section with typed entries (closed slicing).
* `DiagnosticReport`: SNOMED category (Pathology / Radiology), a coded report type (LOINC
  11502-2 / 18748-4 when the document names none), `resultsInterpreter`, and a `conclusion`
  (the report's own impression; else a fixed sentence saying none is stated, never an
  invented finding).
* The scan's bytes are in the bundle (`data` 1..1 in DocumentReference / Binary / Media).
* Condition ICD-10 codings carry a display; MedicationRequest always has `authoredOn`; blood
  pressure is two LOINC components (no string value); SpO2 carries LOINC 2708-6 as the FHIR
  vital-signs profile requires.

## Proof

`tests/golden/fhir/` holds one golden bundle per record type (7, incl. the imaging variant and
the HealthDocumentRecord fallback), built by the real assembly code. The HL7 validator reports
**0 errors** on all seven; `validation-baseline.json` is empty and a test keeps it empty.

```bash
python scripts/fhir_golden.py --check      # the builders still produce the goldens
python scripts/fhir_golden.py --validate   # the real validator (on a host with the E6-S1 stack)
```

Remaining validator **warnings** are advisory: "resources should have narrative" (dom-6),
"observations should have a performer", unit codes not checked without a terminology server,
and a few prescription resources (diagnoses, vitals) that are in the bundle but not referenced
from its single section.

Two bundles are built from the same resource builders: the per-document projection
(`fhir/service.py`, the preview and S9) and the canonical bundle stored when a review item is
approved (`fhir/canonical.py`); both use `document_parts` for the artifact-specific pieces.
