# CI gate (backlog E1-S1)

## What runs

`scripts/ci.py` is the single gate. It runs locally and in `.github/workflows/ci.yml`, with the same
stages in the same order:

| Stage | Command | Fails when |
|---|---|---|
| `lint` | `python -m ruff check src tests db scripts` | any ruff rule (E, F, W, I, B, UP) |
| `format` | `python -m ruff format --check src tests db scripts` | any file not formatted |
| `types` | `python -m mypy src` | any type error |
| `unit` | `python -m pytest -m "not integration"` | any unit test fails |

Every stage runs even after an earlier one fails, so one run lists every red stage. The exit code
is `0` only when all stages pass. No GPU and no infrastructure are needed: the unit tests use the
stub VLM backend (TECH_STACK "Model gateway pattern").

```bash
.venv/Scripts/python scripts/ci.py            # whole gate
.venv/Scripts/python scripts/ci.py --only unit
.venv/Scripts/python scripts/ci.py --list
```

## Integration suite (backlog E1-S2)

`python scripts/ci.py --suite integration`, run by the `integration-gate` job after `unit-gate`:

| Stage | What | Fails when |
|---|---|---|
| `integration` | `pytest -m integration` with `CDI_TEST_REQUIRE_INFRA=1`, `CDI_MLSERVE_BACKEND=stub` | any test fails, **or PostgreSQL / MinIO is missing** (the fixtures fail instead of skipping) |
| `pipeline-audit` | `scripts/ci_pipeline_audit.py`: generates the §8.2 sample set, drives it S1→S9 on the stub VLM | any `pipeline_run` row of those documents is not `ok`, or any of the 7 stages has no run |

The audit looks only at the documents it ingested: failure-path integration tests leave `failed`
runs behind on purpose.

**Native services, no containers.** The backlog text says "service containers"; the owner decision
"no docker" and ARCHITECTURE §3.3 (native processes) take precedence, so the job installs
PostgreSQL 16 from apt and builds MinIO from the same pinned tag (`RELEASE.2025-10-15T17-29-55Z`)
and Go version as `infra/runpod/bootstrap_pod.sh`.

Locally the suite needs a real MinIO (`CDI_TEST_S3_ENDPOINT`, `CDI_S3_ENDPOINT_URL`). On the Windows
dev box Smart App Control blocks a locally built MinIO, so the suite is red here by design until it
runs on the pod or a CI host.

## Golden path (backlog E1-S6)

`scripts/golden_path.py` runs the ARCHITECTURE §8.2 "Anjali Das" documents and compares the counts
in PostgreSQL (facts, provenance, FHIR resources, bundles, artifact, status, structural errors)
with `tests/golden/anjali_das.json`. Any difference exits 1.

| Where | Command | Baseline | Needs |
|---|---|---|---|
| every test run | `tests/test_golden_integration.py` (seeded: S2→S9, §8.2 text as OCR lines) | `stub` | PostgreSQL |
| CI `integration-gate` | `ci.py --suite integration` stage `golden` (`--mode full`, S1→S9) | `stub` | PostgreSQL + MinIO |
| nightly on the pod | `infra/runpod/golden_nightly.sh` (cron 02:30) | `real_vlm` (= §8.2) | pod + GPU |

* Always on a **fresh** database (`--fresh-db NAME`, never the configured one): shared actors
  (Patient, Organization) are attributed to the first document, so a reused database gives fewer
  resource rows.
* The nightly run uses database `cdi_golden` and bucket `cdi-golden`: live data is never touched.
* **Alert on drift:** exit 1 plus one line in `/workspace/logs/golden_alerts.log`; the last
  measurement is in `/workspace/logs/golden_last.json`. (A real alert route arrives with E10.)
* `stub` ≠ `real_vlm` on the vitals sheet (7 vs 1 facts) by design — see ARCHITECTURE §8.2 note.

## Blocking merges on red (needs a git host)

The project folder is not yet in a git repository or on a git host (owner instruction 2026-09-21:
"save the code in folder only"). When it is pushed to GitHub:

1. The workflow runs on every push and pull request (`on: push, pull_request`).
2. Settings → Branches → add a protection rule for `main`: *Require status checks to pass before
   merging* → select **`ci / unit-gate`**; also tick *Require branches to be up to date*.
3. Add the badge to `README.md` (the commented line under "CI").
4. Prove it: open a PR containing a deliberately failing test — the merge button must stay
   disabled; remove the test — it must turn green and become mergeable.

Steps 2–4 are what keep E1-S1 **Blocked** until the repository exists on a host.
