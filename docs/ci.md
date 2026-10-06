# CI gate (backlog E1-S1)

## What runs

`scripts/ci.py` is the single gate. It runs locally and in `.github/workflows/ci.yml`, with the same
stages in the same order:

| Stage | Command | Fails when |
|---|---|---|
| `lint` | `python -m ruff check src tests db scripts` | any ruff rule (E, F, W, I, B, UP) |
| `format` | `python -m ruff format --check src tests db scripts` | any file not formatted |
| `types` | `python -m mypy src` | any type error |
| `secrets` | `python scripts/secret_scan.py` | a secret in the tree or available history |
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

`python scripts/ci.py --suite integration`, run by the `integration-gate` job after `unit-gate` and `grammar-gate`:

| Stage | What | Fails when |
|---|---|---|
| `integration` | `pytest -m integration` with `CDI_TEST_REQUIRE_INFRA=1`, `CDI_MLSERVE_BACKEND=stub` | any test fails, **or PostgreSQL / MinIO is missing** (the fixtures fail instead of skipping) |
| `pipeline-audit` | `scripts/ci_pipeline_audit.py`: generates the §8.2 sample set, drives it S1→S9 on the stub VLM | any `pipeline_run` row of those documents is not `ok`, or any of the 7 stages has no run |

The audit looks only at the documents it ingested: failure-path integration tests leave `failed`
runs behind on purpose.

**Service containers:** `infra/compose/docker-compose.yml` plus `infra/compose/ci.yml` start
PostgreSQL 16, Redis 7 and MinIO built from its pinned, commit-verified source. Disposable
credentials are generated outside the checkout. `scripts/ci_integration.sh` supplies those
credentials only to the local test process. Missing infrastructure fails the suite.
PostgreSQL 16 server/client tools (including `pg_trgm`) are installed on the runner for isolated
restore and failover drills; the host server is stopped before container startup.

## Golden path (backlog E1-S6)

`scripts/golden_path.py` runs the ARCHITECTURE §8.2 "Anjali Das" documents and compares the counts
in PostgreSQL (facts, provenance, FHIR resources, bundles, artifact, status, structural errors)
with `tests/golden/anjali_das.json`. Any difference exits 1.

| Where | Command | Baseline | Needs |
|---|---|---|---|
| every test run | `tests/test_golden_integration.py` (seeded: S2→S9, §8.2 text as OCR lines) | `stub` | PostgreSQL + MinIO |
| CI `integration-gate` | `ci.py --suite integration` stage `golden` (`--mode full`, S1→S9) | `stub` | PostgreSQL + MinIO |
| nightly on the pod | `infra/runpod/golden_nightly.sh` (cron 02:30) | `real_vlm` (= §8.2) | pod + GPU |

* Always on a **fresh** database (`--fresh-db NAME`, never the configured one): shared actors
  (Patient, Organization) are attributed to the first document, so a reused database gives fewer
  resource rows.
* The nightly run uses database `cdi_golden` and bucket `cdi-golden`: live data is never touched.
* **Alert on drift:** exit 1 plus one line in `/workspace/logs/golden_alerts.log`; the last
  measurement is in `/workspace/logs/golden_last.json`. (A real alert route arrives with E10.)
* `stub` ≠ `real_vlm` on the vitals sheet (7 vs 1 facts) by design — see ARCHITECTURE §8.2 note.

## Blocking merges on red

GitHub Actions cannot configure branch protection through a YAML file. For the destination
branch, require **unit-gate**, **grammar-gate**, and **integration-gate** (select the actual emitted check names
in GitHub), require up-to-date branches, and remove bypass permissions as appropriate.
Verify with a deliberately failing PR: merging must remain blocked. A README badge or a green
local run is not proof that protection is enabled.

Both jobs fetch full history so secret scanning can inspect previous commits. Workflow token
permissions are limited to repository reads. See `epic-1-readiness.md` for release blockers.

## Epic 2 grammar gate

`grammar-gate` uses CPU torch 2.8.0, XGrammar 0.1.25 and transformers 4.57.6, matching the
serving grammar versions. It downloads the public Qwen tokenizer at the immutable revision in
`tests/test_grammar_unit.py`, then runs that module with `CDI_TEST_REQUIRE_GRAMMAR=1`.
Unavailable dependencies or tokenizer files fail this job; they cannot silently skip token-mask
checks. The normal unit job can still skip these optional dependencies. Token-level validation
proves the supported schema constraints, not model accuracy or GPU throughput. See
[the Epic 2 readiness assessment](epic-2-readiness.md) for the remaining release evidence.
