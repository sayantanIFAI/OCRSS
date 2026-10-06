# Epic 1 production readiness assessment

Scope: E1-S1 through E1-S7 from the supplied story. Starting revision:
`fd64fa1984dcd11b988cf0a5c2f8c336311ad3df`.

**Release decision: NOT READY.** Passing automated checks alone does not satisfy the deployment,
credential rotation, hosted merge protection, and exact golden-count requirements.

| Story | Implementation / review findings | Remaining acceptance evidence |
|---|---|---|
| E1-S1 | Push/PR gates, README badge, full-history checkout for the secret scan. Fixed clean-checkout sample fixtures, GPU dependency in a mocked unit test, stale schema/Compose assertions and formatting. | Required unit/integration checks on the destination branch; deliberately red PR must be unmergeable; hosted CI pass and PR merge. |
| E1-S2 | Real Postgres + MinIO + Redis service containers, stub integration tests, mandatory-infrastructure failures, and pipeline-run audit. MinIO built from a verified source commit. | Local final results below; hosted CI evidence. |
| E1-S3 | Added webapp and model gateway, file-mounted credentials, startup ordering and health checks. Three-document golden runner no longer ingests both PDF and PNG copies. | Final container checks below; real GPU/backend fallback deployment remains unverified. Default image is synthetic stub only. |
| E1-S4 | Existing package locks and boot drift manifests. Bootstrap now rejects unsafe secret storage and stops on restore errors. | Two unattended boots on a fresh RunPod pod, matching manifests, LIVE URL, /healthz and metrics. No pod access was supplied. |
| E1-S5 | Reject world-readable source store, symlinks, missing/weak configured credentials; mounted store wins over credential environment values. Secret scan includes history and redacts entire findings. Known loopback RabbitMQ examples are scoped exceptions. | Actual vault provisioning, external RunPod key revocation/rotation, proc-permission misuse test on the target, least-privilege DB roles and owner sign-off. Local files are not proof of a deployed vault. |
| E1-S6 | Fixed duplicate sample ingestion, document ordering, reset target validation, extra-document drift detection and error alerts for nightly failures. | **FAIL:** general stub baseline is 21 facts / 21 provenance, while the story requires 15 / 15. Retained the richer extractor rather than discard clinical facts to make counts pass. Real model nightly and alert delivery still require GPU evidence. |
| E1-S7 | Existing Locust 30-patient × 5–10-document harness, percentiles and histogram. Failed jobs/logins/timeouts now fail the process even without SLA enforcement; empty/invalid bundles cannot count as ready; bad timing arguments rejected and histogram boundary fixed. | Full 30-patient run against the intended real-model environment and recorded sizing/SLA result. Contract tests do not prove production capacity. |

## Initial observed failures

The first local gate reported formatting failure, RabbitMQ guest-example scanner false
positives, and 16 unit failures. Some later test expectations reflected this task's concurrent
edits, so this run is diagnostic evidence rather than a clean baseline comparison. Root causes
included missing ignored sample files, a GPU import in a unit test, a schema parser that included
later migration groups, and framework-added function-name tracing metadata.

## Additional verified CI defects

- FHIR cache digests used host-dependent `Path` ordering. Linux disagreed with Windows-generated
  pins for nine packages. Explicit case-insensitive ordering makes **all ten existing hashes
  match**; no package versions or trusted digests were relaxed.
- Current FastAPI enables implicit tracing. Disabled it in all three apps to preserve the
  explicit CDI trace attribute policy; the privacy regression test remains strict.
- Integration migration tests inherited unfinished jobs/runs intentionally left by other
  tests. Their fixture now ends those scratch records before asserting global queue counts.
- The prescription test assumed the old string BP/DocumentReference representation. It now
  checks numeric BP components and the embedded Binary required by the current valid FHIR output.
- Printed OCR joined `TabAmlodipine5mg`. The synthetic stub now handles this without dropping
  the medication. The nine-line test prescription contains seven facts, not the stale eight.
- Canary integration fixtures now deterministically exercise both primary and canary paths;
  random UUID selection occasionally exercised only the primary.
- DR subprocesses use their own private credential store, preserving isolation from the live
  database and object store even with an authoritative vault configured.
- Container installation must preserve the source-relative schema assets; its editable install
  is retained. Native bootstrap uses SCRAM host authentication for newly initialized clusters,
  and persists restore intent so a failed restore cannot be skipped on the next boot.

## Release checklist

1. Resolve the 15-versus-21 golden contract explicitly; do not silently alter the baseline.
2. Complete target deployment/rotation and two-boot evidence with the account owner.
3. Exercise real nightly alerts and the actual SLA load shape.
4. Configure and verify required GitHub checks, then merge through a passing PR.
5. Publish the accepted commit to `Dev_Main` using a workspace authorized for that branch.
   This workspace permits pushes only to `coderabbit/epic-one-production-readiness/ac733714`.

## Validation results

Measured in this sandbox using Python 3.12.15, PostgreSQL 16, real source-built MinIO,
Redis, CPU OCR and a synthetic stub model:

- Full unit stage: **1,110 passed, 30 optional skips**. Later affected tests passed separately:
  secret/FHIR/parser/privacy (148 passed, 1 optional skip), bootstrap restore (17 passed),
  CI/bootstrap/golden configuration (58 passed), and DR isolation (15 passed).
- Lint, formatting, mypy, tree/history secret scan, workflow actionlint and shell syntax: **pass**.
- Pipeline audit: **pass**, all expected pipeline stages successful.
- Full three-PDF golden: **21 facts, 21 provenance, 37 resources, 3 bundles, zero structural
  errors**; passes the current stub baseline, **fails the story's exact 15/15 requirement**.
- Real HL7 validator 6.10.4 with pinned ABDM packages: **7/7 bundles valid, zero errors**.
- Compose build/startup, migrations, bucket initialization, health/readiness, browser login
  and container golden run: **pass**. Live container checks confirm a non-root PID1, private
  0700/0600 secrets and no credential environment values. No real GPU fallback or target
  deployment was exercised.
- Final complete integration stage: **326 passed, zero failures** (231.9 seconds). One
  collection-level skip is the unrelated optional de-identification unit module. Real
  PostgreSQL restore, corruption detection, failover/rejoin and canary/rollback drills passed.

The optional unit skips concern unavailable de-identification models/libraries, xgrammar and
promtool. The real FHIR validator was exercised separately. A clean pattern-based secret scan
does not establish that an exposed external credential has been revoked. No full production
capacity run, deployed vault test, real-model nightly or two-boot RunPod verification occurred.
