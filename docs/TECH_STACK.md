# TECH_STACK — CDI-Adapter

> Every technology that is **actually present** in this repo (`pyproject.toml`, `src/`, `db/`,
> `infra/`, `tests/`, `scripts/`) or in `docs/ARCHITECTURE.md` as deployed. For each one:
> **What** it is, **Why** we use it, **Where** it lives, and **When** it runs.
>
> Checked against the code at commit `fdd4aeb` (2026-09-19). Items that are only planned
> (Snowstorm, FAISS, SapBERT, Temporal, Prometheus/Grafana, etc.) are **not** listed; see §15.
>
> **Updated 2026-09-20:** §17 fits this stack to the target combined Voice + CDI architecture
> (Blackwell 32 GB baseline, ₹75,000/month, India-resident host). The code checks above are
> unchanged; §15's new rows and §17 are **planning statements, not code**.

**Status tags:** **Active** = runs in the deployed system · **Dormant** = code exists, not the
active path · **Optional** = used only if installed · **Alt-deploy** = only for the Docker
Compose deployment · **Declared only** = listed as a dependency but never imported.

---

## Overview

| Layer | Technologies |
|---|---|
| Language & packaging | Python 3.11+ (3.12 on the pod), setuptools |
| Web / API | FastAPI, Uvicorn, python-multipart, Pydantic, pydantic-settings, server-rendered HTML + vanilla JavaScript |
| Database | PostgreSQL 16 (`jsonb`), SQLAlchemy 2 (Core), psycopg 3, Alembic |
| Object storage | MinIO (S3-compatible), boto3 / botocore |
| Background processing | Redis 7, Celery, watchdog, `concurrent.futures` thread pools |
| Document imaging | PyMuPDF, OpenCV, NumPy, Pillow |
| OCR | RapidOCR, ONNX Runtime |
| AI / model serving | Qwen2.5-VL-7B-Instruct (+3B fallback), PyTorch + CUDA, Hugging Face transformers, accelerate, qwen-vl-utils, einops, sentencepiece, httpx; vLLM + XGrammar (dormant) |
| Structured-output safety | JSON Schema (`jsonschema` + `referencing`) |
| Clinical standards | HL7 FHIR R4, ABDM / NRCeS FHIR IG, ABHA, SNOMED CT, LOINC, ICD-10, UCUM; HL7 FHIR Validator (optional, Java) |
| Terminology matching | Python `difflib` |
| Logging | structlog |
| Infrastructure | RunPod GPU pod (NVIDIA Blackwell 32 GB; **production GPU baseline** per the 2026-09-20 decision), bash lifecycle scripts, cron, `pg_dump`/`pg_restore`, Docker + Docker Compose (alt-deploy), Make. Production host: an India-resident cloud with a Blackwell-class GPU (planned, E1-S10) |
| Dev & test | pytest, pytest-cov, ruff, mypy |

---

## 1. Language & packaging

### Python (≥ 3.11; 3.12 on the pod) — Active
- **What:** The programming language the whole system is written in.
- **Why:** The ML, OCR, imaging and web libraries this project needs are all first-class in
  Python, so a single language covers the pipeline end to end.
- **Where:** Everything in `src/cdi_adapter/`, `scripts/`, `tests/`, `db/alembic/`.
  `requires-python = ">=3.11"` in `pyproject.toml`; the pod runs 3.12 (ARCHITECTURE App. B);
  the Docker image uses `python:3.11-slim`.
- **When:** Always. Every service process is a Python process.

### setuptools (+ wheel) — Active
- **What:** Python's standard build/packaging backend.
- **Why:** Installs `cdi_adapter` as an editable package (`pip install -e ".[dev]"`) and defines
  the dependency groups `dev`, `ocr` and `ml`, plus the `cdi-ingest-watch` console script.
- **Where:** `[build-system]` and `[project]` in `pyproject.toml`; the `src/` layout.
- **When:** At install time: local venv, pod bootstrap (`bootstrap_pod.sh`), Docker build.

---

## 2. Web / API layer

### FastAPI — Active
- **What:** A Python web framework for HTTP APIs, with automatic request validation.
- **Why:** Lightweight, async-capable, and gives typed endpoints for JSON APIs and HTML pages
  from one framework.
- **Where:** Three separate apps:
  - `webapp/app.py` (+ `review.py`, `reviewer.py`, `admin.py` routers): the main web app on
    **:8888**: upload page, `/api/jobs`, `/review`, `/reviewer`, `/admin`, FHIR download.
  - `mlserve/app.py`: the **model gateway** on **:8077** (`POST /vlm/generate`, `GET /healthz`).
  - `api.py`: the original ingest-only API (`POST /ingest`), used in the compose stack on :8080.
- **When:** Whenever a user uploads documents, polls a job, reviews facts or downloads bundles,
  and whenever a pipeline stage calls the model gateway.

### Uvicorn — Active
- **What:** An ASGI server that runs FastAPI apps.
- **Why:** FastAPI needs an ASGI server to accept network connections.
- **Where:** Launched from `webapp/app.py` and `mlserve/app.py` (`python -m cdi_adapter.webapp` /
  `.mlserve`); the `uvicorn cdi_adapter.api:app` command in the Dockerfile, compose and Makefile.
- **When:** For the whole lifetime of each web process (started by `start_all.sh` on the pod).

### python-multipart — Active
- **What:** Parser for `multipart/form-data` HTTP bodies.
- **Why:** FastAPI needs it for `UploadFile` / `Form(...)` parameters.
- **Where:** `POST /api/jobs` in `webapp/app.py` (fields `files[]`, `abha`, `patient_ref`).
- **When:** Every time scanned documents are uploaded through the web UI.

### Pydantic — Active
- **What:** Data validation using Python type hints.
- **Why:** Validates the model gateway's request body so malformed calls fail fast.
- **Where:** `mlserve/app.py` (the `/vlm/generate` request model). FastAPI also uses it internally.
- **When:** On every request to the model gateway.

### pydantic-settings — Active
- **What:** Pydantic extension that loads typed settings from environment variables / `.env`.
- **Why:** One typed `Settings` object for all configuration, overridable per environment with
  the `CDI_` prefix, no hardcoded values.
- **Where:** `config.py` (`Settings`, e.g. `CDI_DATABASE_URL`, `CDI_MLSERVE_BACKEND`,
  `CDI_IG_PACKAGE`); the values come from `.env.example` / `.env.runpod`.
- **When:** At process start-up, in every service.

### Server-rendered HTML + vanilla JavaScript — Active
- **What:** HTML pages generated as Python strings and served by FastAPI, with plain browser
  JavaScript (`fetch()`) and no front-end framework or CDN dependency.
- **Why:** Keeps the UI self-contained on-prem: no build step, no Node toolchain, no external
  scripts.
- **Where:** `webapp/page.py` (Generate EMR upload page, `/`), `review_page.py` (`/review`
  fact console), `reviewer_page.py` (`/reviewer` workbasket/worklist + Customer 360),
  `admin_page.py` (`/admin` data inspector), `theme.py` (shared styling).
- **When:** Whenever staff open the UI; the pages call the JSON endpoints with `fetch()`.

---

## 3. Database layer

### PostgreSQL 16 — Active
- **What:** Open-source relational database, with `jsonb` for semi-structured data.
- **Why:** The system of record for all derived data: documents, pages, OCR blocks,
  classifications, extractions, EAV clinical facts, provenance, review tasks, FHIR resources and
  bundles, MPI, and the canonical EMR (`cn_*`, `ops_*`, `fin_*`, `ai_*`, `rv_*`). `jsonb` holds
  raw model output, bboxes and FHIR JSON without extra tables.
- **Where:** DDL in `db/schema.sql` + `db/views.sql` (read-model views); migrations `0001`–`0004`
  in `db/alembic/versions/`. On the pod it runs natively on the overlay disk (:5432); in compose
  it's the `postgres:16` image.
- **When:** Every pipeline stage reads/writes it; every API call and review decision touches it.

### SQLAlchemy 2 (Core, `text()`) — Active
- **What:** Python SQL toolkit. The project uses its **Core** layer with explicit SQL, not the ORM.
- **Why:** Connection pooling and transactions, while keeping queries as plain, reviewable SQL.
- **Where:** `db.py` (engine/sessions), `repo.py`, `canon/repo.py`, `canon/promote.py`,
  `ingest/service.py`, `mpi/*`, `terminology/service.py`, `validate/service.py`,
  `fhir/service.py`, `fhir/canonical.py`, `webapp/review.py`, `reviewer.py`, `admin.py`,
  `db/alembic/env.py`.
- **When:** Every database access.

### psycopg 3 — Active
- **What:** The PostgreSQL driver for Python.
- **Why:** SQLAlchemy's DB-API driver for Postgres (URL scheme `postgresql+psycopg://`).
- **Where:** Not imported directly; selected via `CDI_DATABASE_URL` in `config.py` /
  `.env.example`.
- **When:** Underneath every SQLAlchemy connection.

### Alembic — Active
- **What:** Database schema migration tool for SQLAlchemy.
- **Why:** Versioned, repeatable schema changes, so any environment can reach the current schema.
- **Where:** `alembic.ini`; `db/alembic/env.py`; `versions/0001_initial_schema.py`,
  `0002_patient_mpi.py`, `0003_patient_registry.py`, `0004_canonical_emr.py`.
- **When:** `alembic upgrade head` on every pod boot (`bootstrap_pod.sh`), in the compose
  `migrate` service, and via `make migrate`.

---

## 4. Object storage

### MinIO — Active
- **What:** Self-hosted, S3-compatible object storage.
- **Why:** Stores the **immutable original scans** and rendered page images, the "irreplaceable
  bytes". S3 semantics without any cloud dependency (on-prem).
- **Where:** Bucket `cdi-documents`, key layout `documents/<sha[:2]>/<sha>/original.<ext>`. On the
  pod: a native binary storing to `/workspace/minio-data` (persistent), :9000/:9001. In compose:
  the `minio/minio` image plus a `minio/mc` job that creates the bucket.
- **When:** At ingest (write original + pages), during OCR/extraction (read page images), in the
  review console (page image), and when a FHIR `DocumentReference` points at the scan.

### boto3 / botocore — Active
- **What:** The AWS SDK for Python (botocore is its low-level core).
- **Why:** Standard S3 client, pointed at MinIO through `CDI_S3_ENDPOINT_URL`.
- **Where:** `storage.py` (`boto3` client, `botocore.client.Config`, `ClientError`).
- **When:** Every put/get of a scan or page image.

---

## 5. Background processing & concurrency

### Redis 7 — Active (folder-watch path)
- **What:** In-memory data store, used here as a message broker.
- **Why:** Celery's broker and result backend for the queued pipeline.
- **Where:** `CDI_REDIS_URL` in `config.py`; `worker.py`. On the pod `redis-server` runs natively
  with AOF persistence in `/workspace/redis` (:6379); in compose it's `redis:7-alpine`.
- **When:** When documents come in through the folder-watch / Celery path. **The web app's
  `/api/jobs` path doesn't use Redis today** (ARCHITECTURE §3.1, §13).

### Celery (`celery[redis]`) — Active (folder-watch path)
- **What:** Distributed task queue for Python.
- **Why:** Runs the pipeline stages as retryable background tasks, separate from HTTP requests.
- **Where:** `worker.py`: `celery_app` with tasks `cdi.ingest_file`, `cdi.classify_document`,
  `cdi.ocr_document`, `cdi.extract_document`, `cdi.bind_document` (with `max_retries`); the compose
  `worker` service (`celery -A cdi_adapter.worker.celery_app worker -Q cdi`); `make worker`.
- **When:** For documents dropped into the inbox folder (or queued by the ingest API).

### watchdog — Active (folder-watch path)
- **What:** Python library that watches the filesystem for new/changed files.
- **Why:** Auto-ingests scans dropped into an inbox folder, one of the intake channels.
- **Where:** `ingest/watcher.py` (`Observer`, `FileSystemEventHandler`); the console script
  `cdi-ingest-watch`; the compose `watcher` service. Folders: `CDI_INBOX_DIR`,
  `CDI_PROCESSED_DIR`, `CDI_FAILED_DIR`.
- **When:** Continuously while the watcher runs; fires when a file lands in the inbox.

### `concurrent.futures.ThreadPoolExecutor` (standard library) — Active
- **What:** Python's built-in thread pool.
- **Why:** The web app runs a job's documents **in parallel for Phase 1** (ingest → OCR →
  classify) and has a separate pool for **Phase 2** (extract), without needing a broker.
- **Where:** `webapp/jobs.py` (`_pool`, `_stage2_pool`, sized by `CDI_JOB_MAX_WORKERS`); job state
  is held in memory (`_jobs` dict).
- **When:** Every web-app upload job. (It isn't durable; moving onto the durable queue is planned.)

---

## 6. Document imaging (S1 ingest & normalize)

### PyMuPDF (`pymupdf`) — Active
- **What:** Python bindings for MuPDF, a fast PDF/document renderer.
- **Why:** Turns PDFs and multi-page files into page images at a fixed DPI for OCR and the VLM.
- **Where:** `ingest/pages.py` (`import pymupdf as fitz`); `CDI_PAGE_DPI` (default 200).
- **When:** Once per uploaded document during S1 ingest.

### OpenCV (`opencv-python-headless`) — Active
- **What:** Computer-vision / image-processing library (headless build, no GUI).
- **Why:** Cleans scans before OCR: deskew (min-area-rect), denoise, CLAHE contrast. Also used for
  OCR image prep.
- **Where:** `ingest/pages.py`, `ocr/rapid.py`; thread count capped in `_cpu.py`
  (`cv2.setNumThreads`).
- **When:** Every page at ingest; page preparation during OCR.

### NumPy — Active
- **What:** N-dimensional array library.
- **Why:** The image data format shared by OpenCV, RapidOCR and Pillow.
- **Where:** `ingest/pages.py`, `ocr/rapid.py`, `scripts/make_sample_docs.py`,
  `tests/test_pages.py`.
- **When:** During ingest and OCR image processing.

### Pillow (PIL) — Active
- **What:** Python imaging library.
- **Why:** Image I/O and conversion: thumbnails, TIFF frames, and the image objects passed to
  the VLM.
- **Where:** `ingest/pages.py`, `mlserve/backends.py`, `scripts/make_sample_docs.py`,
  `tests/test_pages.py`.
- **When:** At ingest, and on every model-gateway call that decodes an image.

---

## 7. OCR (S3)

### RapidOCR (`rapidocr-onnxruntime`) — Active
- **What:** An OCR engine (PaddleOCR-derived models) running on ONNX Runtime.
- **Why:** Fast, CPU-only printed-text OCR that returns **bounding boxes, polygons, confidence
  and reading order**, which provenance needs (every fact traces to OCR blocks). No GPU and no
  heavyweight Paddle install.
- **Where:** `ocr/rapid.py`, `ocr/service.py`; `CDI_OCR_ENGINE=rapidocr`; the `ocr` extra in
  `pyproject.toml`.
- **When:** On **every page, before classification** (execution order S1 → S3 → S2 → S4), so the
  classifier reads real OCR text and the page is OCR'd only once.

### ONNX Runtime — Active
- **What:** Microsoft's inference engine for ONNX-format models.
- **Why:** Runs RapidOCR's models efficiently on CPU; its thread pools are capped to the container's
  real CPU quota.
- **Where:** The `ocr` extra; configured through RapidOCR in `ocr/rapid.py` and `_cpu.py`
  (thread caps).
- **When:** Whenever RapidOCR runs.

---

## 8. AI / model serving (S2 fallback, S3 handwriting, S4 extraction)

### Qwen2.5-VL-7B-Instruct (fallback: Qwen2.5-VL-3B-Instruct) — Active
- **What:** An open-weights (Apache-2.0) vision-language model that reads page images and text.
- **Why:** Does the tasks OCR text alone can't: **schema-locked structured extraction** (S4),
  **handwriting transcription** (S3), and **classification of ambiguous pages** (S2). Open weights
  mean on-prem inference with no per-document API cost.
- **Where:** `CDI_VLM_MODEL_ID` / `CDI_VLM_FALLBACK_MODEL_ID` in `config.py`; loaded by
  `mlserve/backends.py`; weights cached in `/workspace/hf-cache` (~16 GB, bf16, sdpa attention,
  `CDI_VLM_MAX_PIXELS_OCR` caps VRAM).
- **When:** On every S4 extraction; on S3 for pages classified as handwritten
  (`CDI_HANDWRITTEN_USES_VLM`); on S2 only when the keyword heuristic is inconclusive
  (`CDI_FAST_CLASSIFY`). Lazy-loaded on first request (~35 s); falls back 7B → 3B on out-of-memory.

### PyTorch (+ CUDA) — Active
- **What:** Deep-learning framework; CUDA is NVIDIA's GPU compute platform.
- **Why:** The runtime transformers uses to run Qwen2.5-VL on the GPU.
- **Where:** `mlserve/backends.py`; torch 2.8.0+cu128 is preinstalled on the RunPod image and
  inherited via `venv --system-site-packages` (ARCHITECTURE App. B); CUDA 13.0 / driver 580.
- **When:** Whenever the `hf` backend loads or runs the model.

### Hugging Face transformers — Active
- **What:** Library for loading and running pretrained models.
- **Why:** The current serving backend (`hf`): `AutoModelForImageTextToText` runs Qwen2.5-VL.
- **Where:** `mlserve/backends.py` (the `hf` backend); the `ml` extra; `CDI_MLSERVE_BACKEND=hf`.
- **When:** Every `/vlm/generate` call while the backend is `hf`. It does one forward pass at a time,
  which is why Phase 2 (extract) is serialised.

### accelerate, qwen-vl-utils, einops, sentencepiece — Active (support libraries)
- **What:** Hugging Face device placement (`accelerate`); Qwen's vision-input preprocessing
  helpers (`qwen-vl-utils`); tensor-reshaping utility (`einops`); tokenizer library
  (`sentencepiece`).
- **Why:** Needed to load and prompt Qwen2.5-VL correctly through transformers.
- **Where:** The `ml` extra in `pyproject.toml`; `bootstrap_pod.sh` installs them; `qwen_vl_utils`
  is imported in `mlserve/backends.py`; the others are used by transformers under the hood.
- **When:** Whenever the `hf` backend loads/runs the model.

### Model gateway pattern (`cdi_adapter.mlserve`) + stub backend — Active
- **What:** A single long-lived FastAPI process that owns the model, with swappable backends:
  `stub` | `hf` | `vllm`.
- **Why:** Keeps pipeline workers light (no model in each worker) and lets the serving backend change
  without touching pipeline code. The `stub` backend is a deterministic keyword responder, so tests
  and CI need no GPU.
- **Where:** `mlserve/app.py`, `mlserve/backends.py`; client side in `ml/client.py`
  (`HttpMLClient`, `StubMLClient`); `CDI_MLSERVE_BACKEND` (code default `stub`, pod `hf`).
- **When:** Every model call from S2/S3/S4 goes through `POST /vlm/generate` on :8077.

### httpx — Active
- **What:** Modern HTTP client for Python.
- **Why:** Pipeline stages call the model gateway over HTTP, and the gateway's `vllm` backend calls
  the vLLM server.
- **Where:** `ml/client.py` (`HttpMLClient`), `mlserve/backends.py` (`VLLMBackend`).
- **When:** Every model call from the pipeline.

### vLLM (with XGrammar guided decoding) — Dormant
- **What:** High-throughput LLM serving engine (paged KV cache, continuous batching), exposing an
  OpenAI-compatible API. XGrammar is its grammar-constrained decoder (`guided_json`).
- **Why:** Aimed to raise Phase-2 throughput and make extraction JSON schema-valid by construction.
  **An A/B test on the pod showed no throughput gain (72–74 s), so the active backend was reverted
  to `hf`.** The code is kept.
- **Where:** `mlserve/backends.py` (`VLLMBackend`, an HTTP client to a separate `vllm serve`
  process); `config.py` (`vllm_url`, `vllm_guided`, `vllm_guided_backend="xgrammar"`,
  `extract_concurrency`); `infra/runpod/start_vllm.sh` (installs vLLM in its own venv
  `/workspace/vllm-venv`); a commented-out `vllm` service in `docker-compose.yml`.
- **When:** Only if `start_vllm.sh` is run **and** `CDI_MLSERVE_BACKEND=vllm` is set.

---

## 9. Structured-output safety

### JSON Schema (`jsonschema` + `referencing`) — Active
- **What:** A standard for describing JSON structure, plus a Python validator and a `$ref`
  resolver registry.
- **Why:** Every model output is checked against a versioned schema per document type. Invalid
  output is repaired (`repair_payload`) or marked `_partial`, and never trusted blindly.
- **Where:** Schemas in `schemas/` (`classification.v1`, `common.defs`, `prescription.v3`,
  `lab_report.v3`, `vitals.v3`, `opd_note.v3`, `discharge_summary.v3`, `radiology.v3`);
  validation in `ml/client.py` (`referencing.Registry`, `vlm_json`, `repair_payload`,
  `validate_schema`); tests in `tests/test_ml_and_classify_unit.py`.
- **When:** After every VLM generation (classification and extraction).

---

## 10. Clinical & interoperability standards

### HL7 FHIR R4 — Active
- **What:** The international standard for exchanging healthcare data as typed resources
  (Patient, Condition, Observation, MedicationRequest, DocumentReference, Provenance,
  Composition, Bundle…).
- **Why:** The **interchange format at the edge**. Governed facts are projected into FHIR so other
  systems (and ABDM) can consume them. The internal model stays our own schema.
- **Where:** `fhir/resources.py` (deterministic fact → resource mapping), `fhir/service.py`
  (gated projection from staging facts, bundle status), `fhir/canonical.py` (bundles from the
  canonical `cn_*` rows); stored in `fhir_resource` / `fhir_bundle` / `cn_fhir_bundle`.
- **When:** After S6 governance / human review: `project_patient`, the live re-projection on
  `GET /api/jobs/{id}/fhir`, and bundle generation after reviewer approval.

### ABDM / NRCeS FHIR Implementation Guide (`nrces.fhir.r4.ndhm#6.5.0`) — Active
- **What:** India's National Digital Health Mission FHIR profiles: document bundles such as
  `PrescriptionRecord`, `DiagnosticReportRecord`, `OPConsultRecord`, `DischargeSummaryRecord`,
  `WellnessRecord`, `HealthDocumentRecord`.
- **Why:** The target is ABDM/ABHA readiness. Bundles must be `Bundle(type=document)` with
  `Composition` first and the ABDM artifact types.
- **Where:** `CDI_IG_PACKAGE` in `config.py`; the `ARTIFACT` map in `fhir/resources.py`;
  `fhir/canonical.py`; Python conformance checks in `fhir/validate_abdm.py`.
- **When:** Whenever a bundle is assembled or validated.

### ABHA (Ayushman Bharat Health Account) identifier — Active
- **What:** India's national health ID, emitted as a Patient identifier with system
  `https://healthid.ndhm.gov.in`.
- **Why:** Required for ABDM exchange. It's kept as **one identifier among many**, always
  alongside the legacy MRN (`urn:cdi:legacy-mrn`), never as the primary key.
- **Where:** `mpi/` (registry lookup by ABHA / CareFlow id / mobile), `fhir/resources.py` and
  `fhir/canonical.py` (Patient.identifier), the `abha` form field on `POST /api/jobs`.
- **When:** At patient lookup/creation and on every Patient resource emitted.

### SNOMED CT, LOINC, ICD-10, UCUM — Active (seed map)
- **What:** Clinical code systems: **SNOMED CT** (conditions, drugs, allergies, procedures),
  **LOINC** (lab tests and vital signs), **ICD-10** (diagnoses, secondary), **UCUM** (units).
- **Why:** Facts must carry standard codes to be interoperable and ABDM-conformant.
- **Where:** `terminology/seed.py`: a curated **~120-concept** map (system URIs
  `http://snomed.info/sct`, `http://loinc.org`, `http://hl7.org/fhir/sid/icd-10`,
  `http://unitsofmeasure.org`); applied by `terminology/service.py` (`bind_fact`,
  `bind_document`, `to_ucum`, `freq_per_day`); emitted by `fhir/resources.py` /
  `fhir/canonical.py`. The package label is `CDI_TERMINOLOGY_PACKAGE=in-snomed-loinc-icd10`.
- **When:** S5 terminology binding after extraction; codes are then carried into FHIR.

### HL7 FHIR Validator (`validator_cli.jar` on Java) — Optional
- **What:** The official HL7 validator for FHIR resources against core specs and IG profiles.
- **Why:** Real profile-level validation beyond the Python structural/conformance checks.
- **Where:** `fhir/validate_abdm.py`: if `java` is on the path **and** the jar exists at
  `CDI_FHIR_VALIDATOR_JAR` (default `/workspace/tools/validator_cli.jar`), it runs
  `java -jar validator_cli.jar <bundle> -version 4.0.1 -ig nrces.fhir.r4.ndhm`; otherwise it falls
  back to the Python checker.
- **When:** On bundle validation. **Currently only the Python fallback runs**, because neither
  Java nor the jar is deployed.

---

## 11. Terminology & identity matching

### Python `difflib` (standard library) — Active
- **What:** Built-in fuzzy string matching (`SequenceMatcher`).
- **Why:** Cheap fuzzy matching with no extra dependency: tolerates OCR/typing variation in
  clinical terms and patient names.
- **Where:** `terminology/service.py` (exact → alias → fuzzy match to the seed map) and
  `mpi/service.py` (fuzzy name + birth-year patient matching).
- **When:** S5 binding for every fact; patient identity resolution at job start.

### SHA-256 (`hashlib`, standard library) — Active
- **What:** Cryptographic hash function.
- **Why:** Content-addressed **dedupe** of uploads (race-safe: a concurrent duplicate reuses the
  existing row) and a stable hash per FHIR bundle.
- **Where:** `ingest/service.py` (document SHA → MinIO key + unique row), `fhir/service.py`
  (`bundle_hash`).
- **When:** Every ingest; every bundle build.

---

## 12. Logging

### structlog — Active
- **What:** Structured (key-value / JSON) logging for Python.
- **Why:** Machine-readable pipeline events (`classified`, `ocr_done`, `extracted`,
  `terminology_bound`, `projected`, `vlm_json_partial`, `job_doc_failed`).
- **Where:** `logging.py`; used across modules. On the pod it writes to `/workspace/logs/`
  (`webapp.log`, `mlserve.log`, `bootstrap.log`).
- **When:** Continuously, for every stage and request.

---

## 13. Infrastructure & operations

### RunPod GPU pod (NVIDIA RTX PRO 4500 Blackwell, 32 GB; Ubuntu 24.04) — Active
- **What:** The rented cloud GPU machine the system is deployed on today.
- **Why:** Provides the GPU for Qwen2.5-VL. It runs native processes (**no Docker** on this pod).
  RunPod's proxy gives the browser HTTPS on port 8888.
- **Where:** ARCHITECTURE §3.3 / §3.5 / App. B; `infra/runpod/`; `.env.runpod`. Persistent
  `/workspace` volume (MooseFS) holds the repo, venv, model cache, MinIO data, Redis AOF, DB dumps
  and logs. The overlay disk (wiped on restart) holds Postgres.
- **When:** All the time in the current deployment.

### Bash lifecycle scripts — Active
- **What:** Shell scripts that bring the whole stack up.
- **Why:** One command rebuilds a pod after any restart, because the overlay (including Postgres)
  is wiped.
- **Where:** `infra/runpod/start_all.sh` (entry point) → `bootstrap_pod.sh` (apt packages,
  `initdb`, restore, `alembic upgrade head`, venv, Redis, MinIO) → `start_mlserve.sh` → webapp;
  `start_vllm.sh` (opt-in); `bootstrap.sh` (older Docker-based bootstrap). Processes are started
  with `setsid nohup`.
- **When:** First boot and after every pod restart.

### cron + `pg_dump` / `pg_restore` — Active
- **What:** Unix job scheduler, and PostgreSQL's backup/restore tools.
- **Why:** Postgres lives on the ephemeral overlay, so it's **rebuilt from a dump on every boot**.
  cron keeps that dump fresh.
- **Where:** `infra/runpod/snapshot.sh` (`pg_dump -Fc` → `/workspace/backup/`, keeps the last
  10); crontab entry `*/15 * * * *` installed by `bootstrap_pod.sh`; `pg_restore` in
  `bootstrap_pod.sh`.
- **When:** Snapshot every 15 minutes (and manually before a planned stop); restore on every boot.

### Docker + Docker Compose — Alt-deploy
- **What:** Container runtime and multi-container orchestration.
- **Why:** A portable, non-RunPod way to run the backing services and the ingest path.
- **Where:** `infra/compose/Dockerfile` (`python:3.11-slim` + libGL/glib + `postgresql-client`);
  `infra/compose/docker-compose.yml` with services `postgres`, `redis`, `minio`, `createbuckets`,
  `migrate`, `api`, `worker`, `watcher` (no `webapp`/`mlserve` service; an optional `vllm` service
  is commented out).
- **When:** Local development or a non-pod host (`make up`). **Not used on the RunPod pod.**

### Make — Active (developer convenience)
- **What:** Build-automation tool with named targets.
- **Why:** Short commands for common developer tasks.
- **Where:** `Makefile`: `venv`, `up`, `down`, `nuke`, `logs`, `migrate`, `api`, `worker`, and more.
- **When:** Local development.

---

## 14. Development & testing

### pytest — Active
- **What:** Python testing framework.
- **Why:** Unit tests (no infra) and integration tests (`-m integration`, needs
  Postgres/MinIO + stub VLM).
- **Where:** `tests/` (`conftest.py`, `test_pages.py`, `test_ingest_unit.py`,
  `test_ml_and_classify_unit.py`, `test_ocr_order_unit.py`, `test_validate_rules_unit.py`,
  `test_ingest_integration.py`, `test_pipeline_integration.py`; 33 test functions);
  `[tool.pytest.ini_options]` in `pyproject.toml`.
- **When:** During development and before changes are merged.

### pytest-cov, ruff, mypy — Active (dev extra)
- **What:** Test coverage reporting (`pytest-cov`); a fast Python linter/formatter (`ruff`, line
  length 100, target py311); a static type checker (`mypy`).
- **Why:** Code quality gates for keeping production code clean.
- **Where:** The `dev` extra in `pyproject.toml`; `[tool.ruff]` config. No CI pipeline runs them yet.
- **When:** Locally, on demand.

### Sample-data & smoke scripts — Active (dev tooling)
- **What:** Python/SQL helper scripts.
- **Why:** Generate synthetic rotated/noisy prescriptions, lab reports and vitals sheets, and drive
  the pipeline end to end for a quick check.
- **Where:** `scripts/make_sample_docs.py`, `scripts/pipeline_smoke.py`, `scripts/ingest_batch.py`,
  `scripts/verify_s3.sql`.
- **When:** Manual testing and demos.

---

## 15. Declared but not used / planned only

| Item | Status | Note |
|---|---|---|
| `tenacity` | **Declared only** | In `pyproject.toml` dependencies, but not imported anywhere in `src/`, `scripts/` or `tests/`. Retries are done by Celery (`max_retries`) and custom code in `ml/client.py`. |
| Snowstorm, SapBERT/BioLORD, FAISS | Planned (ARCHITECTURE §14 #2) | Not in the code; terminology uses the seed map + `difflib`. |
| Temporal, Prometheus, Grafana, Loki, OpenTelemetry, KMS, WORM store, Presidio | Planned (§12–§14) | Not in the code. |
| ABDM gateway / Fidelius | Planned (§14 #7) | Only the DB tables (`abdm_care_context`, `abdm_consent`, `abdm_transfer`) exist. |
| Fine-tuned `cdi-dslm` (QLoRA) | Planned (§14 #1d) | Not in the code. |
| AWQ / INT4 quantization, `peft`, `trl` | Planned, **optional / deferred** (E2-S5, E2-S6) | No longer needed to start: the production baseline is the current 7B (bf16) on the Blackwell 32 GB GPU. Only a cost lever if the ₹75,000/month envelope needs headroom. |
| CPU-first extraction (native PDF text + template parsers, confidence gate 1) | Planned (E2-S9) | Not in the code: the VLM currently runs for every document. This is the main cost lever. |
| Confidence gate 2 (post-VLM risk check) and CPU crop-verification | Planned (E4-S4, E12-S3) | Crop check runs on CPU (RapidOCR on the cropped bbox) by default. |
| WhatsApp Business API intake | Planned (E15-S1) | Not in the code; uploads today are the web form and folder-watch only. |
| Clinic/HIS API client (patient master read, governed write-back) | Planned (E15-S2) | Not in the code; ARCHITECTURE §13 lists write-back as not built. System of record for governed facts is an open decision. |
| OIDC/SSO identity provider, secrets vault, TLS ingress + stable DNS | Planned (E11-S1, E1-S5, E1-S9) | None present; the web UI has no auth. |
| k6 / Locust load harness | Planned (E1-S7) | Dev/test tool for the 30 × 5–10 burst. |
| India-resident production host (IaC, private network, residency evidence) | Planned (E1-S10) | RunPod is the pilot host only. |

---

## 16. Where each technology sits in the pipeline

| Stage | Runs on | Technologies |
|---|---|---|
| Upload / job | web app (:8888) | FastAPI, Uvicorn, python-multipart, ThreadPoolExecutor, PostgreSQL |
| Folder-watch intake | watcher + worker | watchdog, Celery, Redis |
| S1 Ingest & normalize | CPU | PyMuPDF, OpenCV, NumPy, Pillow, SHA-256, MinIO/boto3, PostgreSQL |
| S3 OCR (runs before S2) | CPU (+GPU for handwriting) | RapidOCR, ONNX Runtime; Qwen2.5-VL via the gateway for handwriting |
| S2 Classify | CPU heuristic (+GPU fallback) | Python keyword scoring; Qwen2.5-VL when ambiguous; JSON Schema |
| S4 Extract | GPU | Model gateway (FastAPI) → transformers/PyTorch → Qwen2.5-VL; httpx; JSON Schema repair |
| S5 Terminology | CPU | Seed map (SNOMED CT / LOINC / ICD-10 / UCUM), `difflib` |
| S6 Validate + gate | CPU | Deterministic Python rules, PostgreSQL (`review_task`, `fact_conflict`) |
| S8 Human review | web app | Server-rendered HTML + vanilla JS, FastAPI, PostgreSQL, MinIO (page image) |
| Promotion to canonical EMR | web app | SQLAlchemy Core, PostgreSQL (`cn_*`, `ai_*`, `rv_*`) |
| S9 FHIR | CPU | HL7 FHIR R4, ABDM IG, ABHA; `validate_abdm.py` (Python; HL7 Validator if Java present) |
| Operations | pod | bash scripts, cron, `pg_dump`/`pg_restore`, structlog logs, `/healthz` |

---

## 17. Target architecture (combined Voice + CDI) fitted to this stack

Planning view added 2026-09-20 from the target architecture diagram. **Status** says what exists in
this repo today; the block IDs are in `backlog 1.md` (features) and `backlog infra 1.md` (infra).

| Diagram box | Technology / where | Status |
|---|---|---|
| Carrier SIP trunk, SBC pair, voice orchestrator, ASR, fast-path + semantic cache, Qwen, TTS | Separate Voice project | Not in this repo |
| OPD front desk: web upload | FastAPI `POST /api/jobs` | **Active** |
| OPD front desk: WhatsApp upload | WhatsApp Business API | Planned (E15-S1) |
| Canonical intake API: validate, persist, enqueue, ACK | FastAPI + MinIO + Postgres | Partly built: persists and ACKs today, but jobs run on an in-process pool (E9-S1, E9-S3, E1-S9) |
| Object storage + queue | MinIO / boto3; Celery + Redis | MinIO **Active**; Celery/Redis only on the folder-watch path (E9-S1) |
| CDI CPU workers: preprocess + OCR | PyMuPDF, OpenCV, RapidOCR, ONNX Runtime | **Active** |
| CDI CPU workers: extract | Deterministic/template parser | Planned (E2-S9) |
| Confidence gate 1 | Per-field OCR/extract confidence, per-class thresholds | Planned (E2-S9, thresholds E4-S3) |
| CDI escalation GPU | Qwen2.5-VL-7B (bf16) via mlserve on the **Blackwell 32 GB** | **Active** (used for every document today) |
| Confidence gate 2 | S6 rules + grounding check | S6 **Active**; grounding check planned (E4-S4, E12-S3) |
| HITL workbasket | `/reviewer` workbasket → worklist, Customer 360 | **Built**; SLA timer, auth, dual review open (E7-S1, S2, S5) |
| Clinic / HIS API | HIS client | Planned (E15-S2) |
| PostgreSQL | Postgres 16 | **Active**; holds workflow, audit **and facts + provenance** today (the diagram says "not facts" — open decision, E15-S2) |
| Human contact centre | PBX | Not cloud compute |

**Decisions recorded 2026-09-20**

- **GPU:** Blackwell 32 GB for production; the current 7B runs unquantized. No T4 and no quantization prerequisite.
- **Budget:** ₹75,000/month for Voice + CDI combined. Worked envelope, both scenarios and sensitivity: `backlog infra 1.md` §2.2 (GPU rate is an assumption until a quote exists).
- **Reviewers:** a small pool is acceptable and is not a go-live blocker; unreviewed facts stay held with an ETA.
- **Open:** system of record for governed facts; WhatsApp Business approval; India-region provider and residency sign-off; Voice-project sign-off on the two-GPU topology.
