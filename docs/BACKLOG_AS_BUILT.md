# BACKLOG — As Built (derived from the code)

> **What this is:** a backlog written *from the code in this repository*, not from the plan. Every
> item names the files that implement it, the tests that cover it (counts are what `pytest
> --collect-only` finds), and what is still open. It complements the planning backlogs
> (`backlog 1.md`, `backlog infra 1.md`), whose Phase 0 (Epic E0) it describes in detail.
>
> **Snapshot:** 2026-09-21 · Python 3.12 · PostgreSQL 16.14 (native, local) · no Docker.
> **Reproduce the numbers:** `python -m pytest --collect-only -q` · `python -m pytest -rs`.

## Status legend

| Status | Meaning |
|---|---|
| **Done** | Implemented, tested and verified here, including against real PostgreSQL where it applies |
| **Done — pod check open** | Implemented and verified here; one or more tests need a component this machine cannot run (MinIO, Redis, GPU). Those tests exist and are skipped, never faked |
| **Open** | Not built yet |

## 1. Summary

| Measure | Value |
|---|---|
| Application code | 6,205 lines across 49 modules in `src/cdi_adapter/` |
| Database | 20 per-document tables + `patient_registry` + 15 canonical tables, 5 views, 4 Alembic migrations (770 lines) |
| JSON schemas | 8 (`schemas/`) |
| HTTP routes | 32 (web app 28, model gateway 2, ingest API 2) |
| Tests | 506 collected · **495 pass** · **11 skipped** (all need real MinIO) |
| Quality gates | `ruff check` + `ruff format --check` + `mypy` clean |
| Items | 16 · **8 Done** · **8 Done — pod check open** |

## 2. Items

### AB-01 · Project foundation — **Done**
*Plan block E0-S1.*
- **Code:** `pyproject.toml`, `config.py` (typed settings, `CDI_` env prefix, every ARCHITECTURE §9 key plus gate, OCR and FHIR settings), `logging.py` (structlog JSON), `_cpu.py` (cgroup-aware thread caps), `.env.example`, `.env.runpod`.
- **Tests:** `test_foundation_unit.py` (19).

### AB-02 · Database schema, migrations, data access — **Done**
*Plan block E0-S2.*
- **Code:** `db/schema.sql`, `db/views.sql`, `db/alembic/versions/0001`–`0004`, `db.py`, `repo.py`.
- **Tests:** `test_schema_unit.py` (12, checks the schema against ARCHITECTURE §4), `test_schema_integration.py` (7), `test_repo_integration.py` (5).

### AB-03 · Object storage and S1 ingest — **Done — pod check open**
*Plan block E0-S3.*
- **Code:** `storage.py` (MinIO via boto3), `ingest/pages.py` (render, deskew 0.15°–15°, denoise, CLAHE, thumbnails, content sniffing), `ingest/service.py` (`ingest_bytes`: race-safe SHA-256 dedupe; uses the caller's `mime_type`, sniffs only when none is given).
- **Tests:** `test_pages.py` (35).
- **Open:** `test_ingest_integration.py` (4 skipped): needs real MinIO.

### AB-04 · Model gateway and structured output — **Done**
*Plan block E0-S4.*
- **Code:** `schemas/*.json`, `ml/client.py` (`vlm_json`: extract → repair → validate → one retry → `_partial`), `mlserve/app.py`, `mlserve/backends.py` (deterministic stub; Qwen2.5-VL `hf` backend with lazy load and 7B→3B fallback).
- **Tests:** `test_ml_unit.py` (77). Also run live as a native process over HTTP.
- **Open:** one real Qwen2.5-VL call on the GPU pod (tracked in AB-16).

### AB-05 · S3 OCR — **Done — pod check open**
*Plan block E0-S5.*
- **Code:** `ocr/rapid.py` (RapidOCR, bounding boxes, reading order), `ocr/vlm_ocr.py` (handwriting pass, page-level box, confidence 0.55), `ocr/service.py`.
- **Tests:** `test_ocr_unit.py` (15, includes real RapidOCR on a generated page).
- **Open:** `test_ocr_integration.py` (2 skipped): needs real MinIO.

### AB-06 · S2 classify — **Done — pod check open**
*Plan block E0-S6.*
- **Code:** `classify/service.py` (scored keyword heuristic: score ≥ 5, lead ≥ 3, legible page only), `classify/prompt.py` (VLM fallback, 11 document types).
- **Tests:** `test_classify_unit.py` (13), `test_classify_integration.py` (1 of 3 run on PostgreSQL).
- **Open:** 2 integration tests skipped: need real MinIO.

### AB-07 · Patient registry and identity (MPI) — **Done**
*Plan block E0-S7.*
- **Code:** `mpi/registry.py` (lookup by CareFlow id, ABHA or mobile; `ensure_identity`; upsert), `mpi/service.py` (name/sex/age parsing; ABHA → fuzzy → mint `CFP-YYYY-NNNNNN`; evidence merge that never overwrites a registry identity), `dates.py`.
- **Tests:** `test_identity_unit.py` (44), `test_identity_integration.py` (19).

### AB-08 · S4 extract — **Done — pod check open**
*Plan block E0-S8.*
- **Code:** `extract/prompt.py`, `extract/facts.py` (per-document-type handlers, medication parsing, blood pressure as one fact), `extract/service.py` (`write_extraction`: facts, provenance with boxes, confidence fusion, encounter, identity, wrong-patient guard, purge on re-extract), `terminology/parse.py` (frequency, duration, UCUM units).
- **Tests:** `test_extract_unit.py` (51), `test_extract_integration.py` (7 of 8 run on PostgreSQL).
- **Open:** 1 end-to-end test skipped: needs real MinIO.

### AB-09 · S5 terminology — **Done**
*Plan block E0-S9.*
- **Code:** `terminology/seed.py` (129 concepts: SNOMED CT, LOINC, ICD-10, routes), `terminology/service.py` (exact → alias → brand token → fuzzy 0.86; never force-codes; `bind_document`).
- **Tests:** `test_terminology_unit.py` (28), `test_terminology_integration.py` (2).
- **Open (quality):** seed codes other than those quoted in ARCHITECTURE §8.2 have not been checked against licensed releases → plan block E3-S1.

### AB-10 · S6 validation and governance gate — **Done**
*Plan block E0-S10.*
- **Code:** `validate/rules.py` (range, units, dose ceiling, dates, evidence, terminology), `validate/service.py` (calibration, gate, cross-document conflicts, review tasks, audit sampling at `audit_sample_rate`; BP checked per component).
- **Tests:** `test_validate_rules_unit.py` (50), `test_validate_integration.py` (6).

### AB-11 · S9 FHIR projection and ABDM conformance — **Done — pod check open**
*Plan block E0-S11.*
- **Code:** `fhir/resources.py` (builders, artifact map, structural lint), `fhir/service.py` (asserts governed facts only; `ready_to_share` vs `draft`; stable ids; persistence), `fhir/validate_abdm.py` (switches to the HL7 validator when Java + jar exist; otherwise Python checks).
- **Tests:** `test_fhir_unit.py` (27), `test_fhir_integration.py` (6 of 7 run on PostgreSQL).
- **Open:** 1 skipped (inlining the scan needs real MinIO). The real HL7 validator is plan block E6-S1.

### AB-12 · S8 review console — **Done**
*Plan block E0-S12.*
- **Code:** `webapp/review.py` (tasks, fact detail, accept / correct (supersedes chain) / reject, reviewer-signed provenance, audit, task closure), `webapp/review_page.py`, `webapp/theme.py`.
- **Tests:** `test_review_integration.py` (10).

### AB-13 · Web app and job model — **Done — pod check open**
*Plan block E0-S13.*
- **Code:** `webapp/app.py` (upload, jobs, facts, generate, FHIR, documents, registry, `/healthz`), `webapp/jobs.py` (Phase 1 parallel, Phase 2 serial, review / mismatch states), `webapp/page.py`.
- **Tests:** `test_jobs_unit.py` (7), `test_webapp_integration.py` (11 of 12 run on PostgreSQL). Also run live as a native process.
- **Open:** 1 end-to-end job test skipped (needs real MinIO); `/pages/{n}` reads from MinIO.

### AB-14 · Canonical EMR, reviewer workbasket, admin — **Done**
*Plan block E0-S14.*
- **Code:** `canon/repo.py`, `canon/promote.py` (claim; approve a claimed item; governed facts promoted into `cn_*` with the `ai_*` provenance chain; held facts are not promoted), `fhir/canonical.py`, `webapp/reviewer.py` + `reviewer_page.py`, `webapp/admin.py` + `admin_page.py` (server-rendered `/admin`, allow-listed tables).
- **Tests:** `test_reviewer_integration.py` (7), plus the enqueue test in `test_jobs_unit.py`.

### AB-15 · Folder-watch path: Celery, watcher, ingest API — **Done — pod check open**
*Plan block E0-S15.*
- **Code:** `worker.py` (the 5 Celery tasks of TECH_STACK §5 on Redis, with retry and backoff), `ingest/watcher.py` (`cdi-ingest-watch`), `api.py` (`POST /ingest`).
- **Tests:** `test_folder_watch_unit.py` (15, includes a real watchdog Observer).
- **Open:** a real Redis round trip on the pod.

### AB-16 · Operations and alternative deployment — **Done — pod check open**
*Plan block E0-S16.*
- **Code:** `infra/runpod/*.sh` (bootstrap with MinIO built from source, `start_all`, `start_mlserve`, snapshot), `infra/compose/` (8 services, MinIO images on quay.io), `Makefile`, `scripts/` (sample documents, smoke run, batch ingest, `verify_s3.sql`), `scripts/dev/pg_local.ps1`.
- **Tests:** `test_ops_unit.py` (15), `test_ops_integration.py` (1).
- **Open:** run `start_all.sh` on the pod, then the pod checks in §3.

## 3. Open work derived from the code

### 3.1 Pod verification — closes all 8 "pod check open" items together

| # | Task | Closes |
|---|---|---|
| P-1 | `bash infra/runpod/start_all.sh` on the pod: PostgreSQL, Redis, MinIO (built from source), gateway, web app | AB-16 |
| P-2 | `CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000 python -m pytest -m integration` → the 11 skipped tests run | AB-03, 05, 06, 08, 11, 13 |
| P-3 | Start `mlserve` with `CDI_MLSERVE_BACKEND=hf` and make one real Qwen2.5-VL call | AB-04 (real model) |
| P-4 | Run a Celery worker and drop a file into the inbox; it should reach `validated` | AB-15 |

### 3.2 Limits in the current code, and the plan block that removes each

| Limit (where in the code) | Plan block |
|---|---|
| ~~Job state is in memory (`webapp/jobs.py` `_jobs`); a restart loses running jobs~~ Removed by E10-S9: jobs in `web_job`, run on the `cdi-web` queue with a lease, resumed after a crash | E9-S1 (E10-S9) |
| ~~One worker pool runs the CPU stages and the GPU stage, so Phase-1 capacity cannot be bought without GPU capacity~~ Removed by E9-S2: `cdi` / `cdi-web` are the CPU pool, `cdi-gpu` the GPU pool, each sized on its own; a web job crosses the boundary once, repeating no Phase-1 work (ARCHITECTURE §7.5). The throughput figure itself is E9-S6's load test | E9-S2 (measured: E9-S6) |
| No login: the reviewer id comes from the request body (`review.py`, `reviewer.py`) | E11-S1, E7-S1 |
| Seed terminology codes are not verified against licensed releases (`terminology/seed.py`) | E3-S1…S5 |
| Conformance uses Python checks until Java + the HL7 jar are deployed (`fhir/validate_abdm.py`) | E6-S1, E6-S2 |
| Calibration is a placeholder and thresholds are global constants (`validate/service.py _calibrate`) | E4-S2, E4-S3 |
| Organization and Practitioner carry no HFR / HPR ids (`fhir/resources.py`) | E8-S1 |
| No SLA timer on the review queue | E7-S2 |
| No Customer-360 view | E7-S8 |
| vLLM backend not built; `hf` only (`mlserve/backends.py`) | E2-S1…S3 |
| `cn_*` / `ops_*` / `fin_*` columns are a minimal reconstruction (ARCHITECTURE §4.5 gives none) | E1-S8 |
| Compose now has webapp / mlserve and startup health gates; real-model fallback evidence remains pending | E1-S3 |
| A 4-digit blood-pressure misread is not captured (it is dropped, never guessed) | — noted, no change planned |

## 4. Decisions made while building

| Decision | Where |
|---|---|
| Phase 0 added: rebuild the as-built system from ARCHITECTURE.md before Phase 1 | owner, 2026-09-21 |
| No Docker; native processes only; no stand-in servers for MinIO or Redis | owner, 2026-09-21 |
| Test database is reset per session instead of dropped (Windows makes DROP DATABASE slow) | `tests/conftest.py` |

## 5. Reverted to match the architecture (2026-09-21, owner request)

Every place the code had gone beyond or against `docs/ARCHITECTURE.md` (sections 4–6 of the
boundary review) now follows the document:

| Now (as the architecture says) | Before |
|---|---|
| Blood pressure is **one** fact ("138/86", LOINC panel 85354-9); FHIR carries 8480-6 / 8462-4 as its components (§8.2); S6 checks each component's window | two facts |
| Scans over **700 KB** referenced by URL (§7.2), a constant; URL is the `s3://` location (§5 S1); facility name is the §7 branding | settings `CDI_FHIR_INLINE_MAX_BYTES`, `CDI_FACILITY_NAME`; `/original` URL |
| Celery chain is the **five** tasks of TECH_STACK §5 | + `validate_document` |
| An uncoded Condition is a lint **error** (§5 S9 "Conditions coded") | warning |
| A same-day contradiction forces **both** facts `in_review` (§5 S6) | a clinician-confirmed fact was left alone |
| `ingest_bytes` uses the caller's `mime_type` and sniffs only when none is given (§5 S1 signature) | always sniffed |
| Reviewer approves a claimed item (§4.5); held facts are not promoted and get no verification record | claimer-only, refused while held |
| Endpoints are only those of §7.1: `/api/documents/{id}/original` and `/api/admin/*` removed; `/admin` is server-rendered (`?table=`, `?mpi=`) | extra JSON routes |
| Storage client with boto3 defaults; audit sampling a random draw at `audit_sample_rate`; API returns DB values as-is; S2 anchors without OCR-tolerance | bounded timeouts, deterministic sampling, number conversion, OCR tolerance |

**Consequences to be aware of** (these return because the behaviour now matches the text):
an executable uploaded with a PDF content type is stored as given; Postgres `numeric` values reach
API clients as JSON strings; `/healthz` can take a long time when MinIO is down; a noisy vitals
scan whose heading OCRs as "VITALSIGNSCHART" goes to the VLM instead of the heuristic.

## 6. Defects found by the tests and fixed

| Defect | Fixed in |
|---|---|
| Cardiology prescription scored 8, below ARCHITECTURE §5's 9+ | `classify/service.py` |
| Age in months computed to the wrong birth year | `mpi/service.py` |
| "(55/M)" left in parsed patient names | `mpi/service.py` |
| Partial registry update failed on a NOT NULL check | `mpi/registry.py` |
| "TWICE IN A YEAR" gave no frequency; pen fill volume leaked into drug names; schedule text stuck to drug names | `terminology/parse.py`, `extract/facts.py` |
| Two tests intermittently failed due to random audit-sample tasks | `tests/` |
| MinIO image tags written from memory did not exist (moved to quay.io and confirmed) | `infra/compose/docker-compose.yml` |
