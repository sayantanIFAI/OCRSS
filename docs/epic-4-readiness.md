# Epic 4 readiness assessment

Scope: E4-S1, E4-S2, E4-S3, E4-S5, E4-S7, E4-S8 and E4-S11 from the supplied image.
Base: `d54667801c8387ead0d8345d4d191a73b25c6c77`, local `Dev_Main`.

**Production: NO-GO.** Code and database protections are strengthened, but the original-image
trace requirement, real calibration/threshold evidence and independent-engine integration are
not complete. Passing synthetic tests does not satisfy those requirements.

## Story-by-story findings

| Story | Verified implementation and fixes | Remaining acceptance work |
|---|---|---|
| E4-S1 — adjudication export | Existing nightly cron and versioned immutable training tables. Export now requires signed reviewer provenance. Migration 0025 captures complete proposed/corrected training snapshots in the S8 transaction; older signed decisions are captured before re-extraction deletes their clinical projections. Latest decisions remain exportable afterward. Export is content-addressed and idempotent. | Exercise the scheduled job on the intended environment with the approved clinician dataset. Decisions lost before this patch cannot be recovered from missing clinical projections. |
| E4-S2 — per-field calibration | Existing medication medicine/strength/dose/frequency/duration slices, lab-test and diagnosis slices, reliability diagrams. Fixed incorrect interpolation across pooled isotonic blocks; ties are grouped before fitting and pooled blocks stay flat. Dose-unit corrections now count as errors. Split by document within each field to avoid sharing a document between training and evaluation. Invalid scores/fit arguments are rejected; concurrent fit publication is serialized. | Approved tolerance and representative adjudications are missing. Existing tolerance 0.05 remains explicitly a placeholder. Reliability verdict currently uses ECE; the owner must define whether the agreed band also requires per-bin/max-gap limits. No production fitted version is selected by default. Refit old models with method `isotonic_pav_v2`. |
| E4-S3 — derived thresholds | Existing class/field/evidence-state derivation and JSON export. Held-out acceptance now uses the Wilson upper bound. Failed/absent held-out verdicts cannot enable a class. Missing classes/field scores hold for review; disagreement always holds, including a malformed enabling configuration. Thresholds use double precision and scores are not rounded up across cutoffs. Duplicate/non-finite/out-of-range configuration entries fail. | No clinically approved NFR, measured production threshold file or held-out cohort is supplied. Existing targets 2% default/0.5% numeric are placeholders, not approval of the story's example 0.1% target. Doctor/exemplar and independent-reader cohorts remain unavailable. |
| E4-S5 — no evidence, no auto-accept | Existing deferred SQL constraint protects insertion/state changes. Migration 0024 additionally prevents deletion, reassignment or truncation of the last evidence after auto-acceptance. Parent-row writes serialize competing removals; direct SQL and concurrent deletion regressions exercise the invariant. Reverting a fact to review in the same transaction remains possible. | Target migration and audit of pre-existing facts are required. Existing violations are not silently rewritten. |
| E4-S7 — immutable evidence | Existing raw observation, candidate and verified-fact immutability, supersession, compatibility view, reviewer append behavior and tracing tests. Migration 0024 validates observation references and candidate/verified-fact chain consistency. Durable adjudication history also preserves full training correction values across re-extraction. Recognition schemas still exclude codes; RapidOCR engine code is unchanged. | **FAIL: crop hashes currently use normalized page images**, not original-image crops. Original coordinates/crop lineage require implementation. Extracted-text-only provenance can also produce an empty observation chain; complete raw-pixel traceability is not proven for that path. `verified_fact` still stores aggregate confidence; the required complete per-field final evidence representation is not established. Existing Epic1 story 15/15 golden mismatch remains. |
| E4-S8 — evidence hierarchy | Versioned tiers/levels, negative elimination, prior-only reranking, trace logging and S8 display exist. Fixed cross-tier conflicts and contradictions against a nonleading candidate. Multiple attached recognizers no longer imply agreement without a field comparison. Policies cannot remove tier 0/1 conflict rules. An `assumed` validation basis cannot short-circuit a real backend; the synthetic stub uses an explicitly labeled fixture basis to retain its golden regression. | Default real-backend facts now escalate until a measured field/cohort policy is supplied. Grounding, independent readers, exemplars and L4–L7 remain unbuilt and are logged as skipped. Clinical validation of any enabled cheap level is still required. |
| E4-S11 — concordance storage | Existing columns/FKs and append-only behavior. Migration 0024 enforces distinct-engine readings of the same handwritten crop/field and rejects blank algorithm versions. Unordered pair uniqueness prevents a duplicate result by swapping A/B. Direct SQL cannot bypass crop identity checks. | Real E2-S11 recognizers/comparison do not yet write pipeline rows. Synthetic database tests validate the persistence contract only. The candidate-table join remains outside this story's stated scope. |

## Corrected failure modes

1. Adjudications could disappear before scheduled export when re-extraction purged live facts.
2. Auto-accepted facts could lose all provenance after the original deferred check had passed.
3. Two concurrent deletions could each believe another evidence row remained.
4. Candidate observation arrays could point to missing observations or a different document.
5. Agreement records could compare different crops on one page, or duplicate a pair in reverse.
6. Isotonic block-centre interpolation changed fitted values within a pooled block.
7. Dose-unit changes could be mislabeled as correct, and document facts could cross fit/evaluation.
8. Threshold configuration ignored failed validation, omitted fields and uncovered classes.
9. Rounded scores/cutoffs could change the decision boundary.
10. Cross-tier conflicts or contradictions of a nonleading candidate could escape review.
11. Multiple attached engines were presented as supporting the same value without a comparison.
12. The default policy treated an explicitly assumed validation basis as permission to accept.

## Validation

Fresh local results:

- Full CI: **1,165 unit tests passed**, 31 optional skips; lint, format, types and secret scan passed.
- Full integration gate: **337 passed**, one optional collection skip; pipeline audit passed.
- Final compatibility/retention checks after the last guards: **60 focused unit tests** and
  **45 focused integration tests passed**. These overlap the broad suites; do not add the counts.
- Final static gates passed again after all application/test edits.
- Golden stub/full: **21 facts, 21 provenance rows, 37 resources, 3 bundles, 0 structural errors**.
  This preserves the current baseline; it does not meet the earlier story's exact 15/15 counts.
- Reused prior real CPU grammar results (45 passed) and actual FHIR validator results (7/7):
  relevant source/artifact hashes and external dependency versions are unchanged.
- Remaining optional skips concern de-identification models and promtool; grammar/FHIR checks
  are covered by the reused results. A Starlette/httpx deprecation warning remains.

Commands: `python scripts/ci.py`, `bash scripts/ci_integration.sh`, plus the focused Epic4,
calibration, thresholds, resolution, schema and S8 review modules. Tests use synthetic clinical
fixtures, PostgreSQL 16, real MinIO and CPU OCR. No clinician dataset or clinical approval is
represented by these results. Hosted CI and a deployment on the intended target were not run.

**Development: suitable for isolated migration and manual-review testing after applying 0024
and 0025.** This is not approval for clinical automatic acceptance or production deployment.

## Deployment and migration notes

- Apply migrations 0024 and 0025 before using the new application. 0024 adds evidence/crop checks
  and double-precision thresholds; 0025 adds immutable adjudication snapshots.
- Existing reversed agreement duplicates cause the unique-index migration to fail; inspect and
  resolve legacy anomalies through an approved data migration. Do not delete immutable evidence
  merely to force an upgrade through. Triggers protect new writes; they do not certify old rows.
- Refit calibrators and regenerate threshold files. Old curves were fitted by the flawed block
  interpolation and are refused by the gate. Regenerated files carry
  `validation_method: held_out_wilson_v2`; legacy files are refused. Classes lacking
  `meets_target: true` hold for review.
- With `hf` or `vllm`, the shipped assumed cascade policy now sends facts to human review. Supply
  measured field/cohort policy evidence before enabling automatic acceptance. The `stub` fixture
  policy is for synthetic regression testing.
- Original-image crop lineage and full per-field final evidence still require implementation.
  These are code gaps in addition to the missing clinical measurements.
- The immutable adjudication table retains signed correction history; rollback that drops it
  would destroy that history. Export/retain the data before a rollback past 0025.
- Remote publication and hosted CI remain unverified. This workspace can push only the assigned
  task branch, and the earlier workflow-permission rejection has not been resolved.
