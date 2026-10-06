# Tests advised on a prescription (`lab_order`)

Owner request, 2026-09-28: *retrieve the lab test suggestions from the prescription* — the tests the
doctor wrote on the page (not tests an AI thinks of). Owner's choice: tests written on the Rx only.

## What it does

| Step | What happens | Where |
|---|---|---|
| Extract (S4) | The prescription and OPD-note schemas have `investigations_advised`. The model is told: tests under *Investigations / Ix / Tests advised*, or a test under *Adv* (`Adv: CBC, LFT`), one per test, exactly as written; a test with a result is a result; never a test the page does not name. A model that writes `investigations`, `tests`, `lab_tests`… is repaired to the key. | `schemas/prescription.v3.json`, `schemas/opd_note.v3.json`, `extract/prompt.py` (`TESTS_ADVISED_RULES`), `ml/client.py` (`_ALIASES`) |
| Facts | One `lab_order` fact per test: "CBC, LFT, RFT" and "T3 + T4 + TSH" are split on `,` `;` `+` and the word *and*; never on `/` ("Urine R/M" is one test). Each keeps the evidence of its line. | `extract/facts.py` (`split_orders`, `_order_facts`) |
| Code (S5) | LOINC for panels (CBC 58410-2, LFT 24325-3, RFT/KFT 24362-6, lipid profile 57698-3, urine routine 24356-8, electrolytes 24326-1, blood group 882-1, urine culture 630-4) and single analytes (the existing lab table: HbA1c, TSH, creatinine, FBS…); SNOMED CT for ordered studies (ECG, USG abdomen, chest X-ray, echo). Words around a test are ignored for matching only ("S. Creatinine", "Vitamin D level"). Anything else stays as written, `local_only` — e.g. "TFT" (no verified thyroid-panel code yet). | `terminology/seed.py` (`LAB_PANELS`, `ORDERED_STUDIES`), `terminology/service.py` (`order_key`) |
| Check (S6) | Gated like every fact (evidence, confidence); an uncoded test is not a finding. | unchanged |
| Review | Reviewer → **Tests advised** section (after Medications): test as written, its code, Accept / Reject / Edit (text and code). | `webapp/reviewer_page.py` |
| EMR | On approval each governed test is promoted into `cn_lab_order` (test as written, code, status `active`). | migration `0011`, `canon/promote.py` |
| FHIR | The record's section narrative carries each governed test: `Test advised: KFT (Renal function panel - Serum or Plasma)`. ABDM 6.5.0 gives a PrescriptionRecord one section whose entries are closed to MedicationRequest / Binary, so a ServiceRequest there would fail validation; the narrative is where the written advice already goes. The approved (canonical) record does the same from `cn_lab_order`. | `fhir/service.py` (`advised_test`), `fhir/canonical.py` |

## Not done (yet)

* A structured `ServiceRequest` in the OPConsultRecord **Investigation Advice** section (the profile
  allows it) — to be added with the in-progress IG-validation work on `fhir/`, then checked with the
  HL7 validator.
* Verified codes for a thyroid panel, dengue NS1 and other tests outside the seed list (backlog
  E3-S1, licensed LOINC).
* Accuracy on real handwritten prescriptions is not measured (the tests use the stub model).
