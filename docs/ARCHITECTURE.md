# CDI-Adapter — As-Built Design, Architecture & Implementation

**Clinical Document Intelligence Adapter** — turns a legacy hospital's *scanned-only*
records into a structured EMR and **ABDM/ABHA FHIR R4** record bundles.

- **Status:** working end-to-end prototype with a governance gate, deployed and live
  on a RunPod GPU pod. Not production-hardened (see §13).
- **Live URL:** `https://g1a7lswmb5t79o-8888.proxy.runpod.net` — `/` upload → FHIR,
  `/review` the human adjudication queue.
- **Last perf pass (2026-09-09):** heuristic classifier (no VLM for clean printed
  pages), OCR runs once (before classify, not twice), schema relaxation + one
  retry, patient-mismatch guard, and native thread-pool caps sized to the
  container's real CPU quota. A 3-document job now completes in **~74 s wall**
  (was ~140 s); extract is 86 % of that. See §7.3–§7.4 for the measured breakdown
  and the vLLM/XGrammar plan for the rest.
- **Update note (later in the same development cycle, pod since recycled — see
  caveat below):** two further changes landed after the above snapshot.
  (1) **vLLM `AsyncLLMEngine` + XGrammar grammar-locked decoding were built,
  deployed and A/B tested** against `hf` on the same 3-document job —
  **no measured throughput improvement** (72–74 s wall, unchanged). `hf` was
  restored as the active backend; the `vllm` code path is merged and kept in
  the tree, dormant. See §3.4 and §7.4 for the root-cause analysis.
  (2) **A canonical EMR schema + a dedicated `/reviewer` workbasket/worklist
  console + a `/reviewer/c360` Customer-360 read-model + a `/admin` data
  inspector were built and verified on-pod**, including a live promotion of a
  real dual-identifier (ABHA + legacy MRN) patient through to a
  `PrescriptionRecord` bundle. See §4.5, §7.1 and §13.
  > **Caveat:** the pod this work was verified on has since been recycled
  > (RunPod reassigns IP/port on every restart — see §3.3) and was not
  > reachable to re-verify live when this note was written. What follows is
  > recorded from that development cycle's own implementation record — file
  > names, function names, endpoint paths and one specific bug fix — not a
  > fresh schema dump. Treat it as **MEASURED-but-not-current**, and re-run a
  > schema/endpoint reconciliation pass the next time the pod is live.
- **Companion docs:** [`DESIGN.md`](DESIGN.md) is the north-star design + 10-country
  research; this document is **what is actually built and running**.
- **Governing principle** (adopted verbatim from external review):
  > *AI proposes clinical facts. Evidence proves them. Deterministic validation
  > governs them. Human adjudication resolves uncertainty. FHIR represents the
  > governed result.*
  The VLM is never the system of record. Every clinical fact is bound to a pixel
  region, an OCR span, a model version and a confidence score; a deterministic
  rules engine (S6) then routes each fact to **auto-accept** or **human review**,
  and FHIR projection (S9) **only asserts governed facts**.

---

## 0. What exists today (one-paragraph truth)

A pipeline (`ingest → OCR → classify → extract → terminology → **validate/gate** →
FHIR projection`, with a **human review** loop) runs on one GPU pod. Extraction
uses a **real open-source vision-language model (Qwen2.5-VL-7B-Instruct)** served
by an in-house model gateway. **Classification** is a scored keyword heuristic over
the page OCR text that only calls the VLM when the type is genuinely ambiguous or
the page is handwritten. Printed-text OCR uses RapidOCR (ONNX, CPU) and runs
*before* classify so the same page is never OCR'd twice; handwriting gets a second
VLM-OCR pass that supersedes it. Terminology binding uses a
**curated seed map** (SNOMED CT / LOINC / UCUM), not a full terminology server.
**S6** runs deterministic clinical-plausibility rules (physiological ranges, unit
sanity, dose/frequency ceilings, impossible dates, evidence-present,
duplicate/contradiction detection) and a **governance gate** that routes every
fact to `auto_accepted` or `in_review` — a `_partial`/malformed or low-confidence
extraction is **never** auto-trusted. **S8** is a web review console
(image → highlighted bbox → OCR → fact → code → confidence → accept/correct/reject)
that writes reviewer-signed provenance. FHIR projection is deterministic, **asserts
only governed facts**, and emits NRCeS/ABDM `Composition`-based
`Bundle(type=document)` artifacts (`status = ready_to_share` only when fully
governed and structurally clean, else `draft`) with the original scan attached and
a `Provenance` chain. **Still not built:** the fine-tuned domain SLM,
Snowstorm/embedding terminology, HAPI IG validation, S7 longitudinal
reconciliation (a canonical data **foundation** for it now exists — see §4.5 —
but the temporal-merge/supersedes **engine** does not), ABDM gateway,
auth/DPDP, HA/DR — see §13. **S8 human review has since been substantially
hardened** (dedicated `/reviewer` app, workbasket→worklist assignment,
Customer-360 read-model, admin inspector — §4.5, §7.1) though auth and
SLA-timer alerting on the queue are still open.

---

## 1. Context & problem

The hospital's legacy HMS stores **no discrete clinical data** — every patient
record is a pile of scanned images / PDFs (prescriptions, lab reports, radiology,
discharge summaries, advice slips, vitals sheets). To join ABDM as a Health
Information Provider the hospital must expose FHIR R4 record artifacts under
patient consent. The adapter synthesizes the missing structured record from the
scans **without modifying the legacy database**.

Constraints that shaped the build:

| Constraint | Consequence |
|---|---|
| Open-source only, no per-document API cost | On-prem models: Qwen2.5-VL (Apache-2.0), RapidOCR, seed terminology |
| Runs on a single RunPod pod, **no Docker** | Native processes (Postgres, Redis, MinIO, gateway, web app) |
| Pod GPU is an **RTX PRO 4500 Blackwell 32 GB** (workstation-class, 165 W) | 7B VLM in bf16 fits (~15 GB); ~12–14 GB left for a KV cache once vLLM lands; no fine-tuned DSLM yet |
| `/workspace` is MooseFS (persistent, but `chown`/`fallocate` fail, dirs forced 0777) | Postgres cluster runs on the ephemeral overlay; a `pg_dump` on `/workspace` + auto-restore is the persistence mechanism |
| Pod is recreated often (new IP/port each time) | One idempotent `start_all.sh`; nothing derived stored only in the cluster |

---

## 2. Solution overview — the adapter principle

```
┌───────────────────────────────┐        read-only         ┌────────────────────────┐
│  LEGACY HMS  +  its database   │ ───────────────────────▶ │      CDI-Adapter        │
│  (scanned documents only)      │   folder / API / batch   │  own PostgreSQL + MinIO │
└───────────────────────────────┘                          └───────────┬────────────┘
                                                                       │
                          ┌────────────────────────────────────────────┴───────────┐
                          ▼                                                         ▼
                  rows in tables                                        ABDM FHIR R4 JSON
        clinical_fact · encounter · fhir_resource                Bundle(type=document) per
        (+ provenance, confidence, review_state)                 source doc → HIP/HRP gateway
```

- The legacy DB is **never written**. The adapter has its own store.
- Two synchronized outputs per document: **relational rows** and a **FHIR bundle**.
- Portability: every jurisdiction difference is isolated into two swappable
  packages — a **Terminology Package** and a **Profile/IG Package** (`CDI_IG_PACKAGE`,
  `CDI_TERMINOLOGY_PACKAGE`). India (`nrces.fhir.r4.ndhm#6.5.0`) is the default.

---

## 3. Architecture

### 3.1 Component view (as deployed)

```
                       ┌──────────────────────────────────────────────────────────┐
   browser ── HTTPS ──▶ │  RunPod edge proxy  :8888  →  pod localhost:8888         │
                       └───────────────────────────┬──────────────────────────────┘
                                                   ▼
                         ┌──────────────────────────────────────────┐
                         │  cdi_adapter.webapp   (FastAPI, uvicorn)  │   port 8888
                         │  - GET /                upload page       │
                         │  - POST /api/jobs       start a job       │
                         │  - GET  /api/jobs/{id}[ /fhir | /download]│
                         │  - GET  /api/patients/{id}/fhir           │
                         │  ThreadPool ── runs the pipeline ─────────┼──┐
                         └──────────────────────────────────────────┘  │
                                                                       │ in-process calls
   ┌───────────────────────────────────────────────────────────────────┘
   ▼
 S1 ingest ─▶ S2 classify ─▶ S3 OCR ─▶ S4 extract ─▶ S5 terminology ─▶ S9 FHIR projection
   │             │              │          │              │                  │
   │             │  HTTP        │  HTTP    │  HTTP        │ (in-proc)        │ (in-proc)
   │             ▼              ▼          ▼              ▼                  ▼
   │      ┌───────────────────────────────────────┐   seed.py map      resources.py
   │      │ cdi_adapter.mlserve  (FastAPI)  :8077  │   (SNOMED/LOINC)   deterministic
   │      │  backend = hf → transformers          │
   │      │  Qwen/Qwen2.5-VL-7B-Instruct (bf16)   │  ~16 GB VRAM, lazy-load ~35 s
   │      │  POST /vlm/generate {image_b64,prompt,│
   │      │        json_schema} → {text}          │
   │      └───────────────────────────────────────┘
   │
   ▼ (all stages)
 ┌─────────────┐   ┌──────────────┐   ┌─────────────────────────────┐
 │ PostgreSQL  │   │ MinIO (S3)   │   │ Redis (Celery broker;       │
 │ 16          │   │ scans+pages  │   │ used by the folder-watch    │
 │ (overlay)   │   │ (/workspace) │   │ path, not the web app)      │
 └─────────────┘   └──────────────┘   └─────────────────────────────┘
```

> *Reconciled (E1-S8):* the job pool is `max(2, CDI_JOB_MAX_WORKERS)` threads for Phase 1 and one
> serial thread for Phase 2 (extract), as §7.2 describes; the earlier "ThreadPool(1)" label was stale.

Optional / not on the hot path: `cdi_adapter.worker` (Celery) chains the same
stages for the **folder-watch** ingestion path; `cdi_adapter.api` is the original
ingest-only API; `cdi_adapter.ingest.watcher` watches a drop folder.

### 3.2 The 9-stage pipeline (as built)

| # | Stage | Module | Model / method | Writes |
|---|---|---|---|---|
> **Execution order** is `S1 → S3 → S2 → S4 …` — OCR runs before classify so the
> classifier reads real OCR text and the page is never OCR'd twice. The S-numbers
> below are stage identities, not run order.

| S1 | Ingest & normalize | `ingest/pages.py`, `ingest/service.py` | PyMuPDF render; OpenCV deskew (min-area-rect) + denoise + CLAHE; SHA-256 dedupe (race-safe: `IntegrityError` → reuse existing row) | `source_document`, `document_page`, original+pages → MinIO |
| S2 | Classify | `classify/service.py`, `classify/prompt.py` | **scored keyword heuristic** over the S3 OCR text (`_heuristic_classify`): a type only when it scores ≥5 *and* leads the runner-up by ≥3 on a legible page (≥180 chars, ≥75% alnum); otherwise Qwen2.5-VL, schema `classification.v1` | `doc_classification` |
| S3 | OCR / layout | `ocr/service.py`, `ocr/rapid.py`, `ocr/vlm_ocr.py` | **runs before classify.** printed → RapidOCR (boxes+conf+reading order); a page the classifier later calls handwritten gets a second VLM-transcription pass that replaces the blocks. `ocr_document(force_engine="rapidocr"|"vlm")` | `ocr_block` |
| S4 | Extract | `extract/service.py`, `extract/prompt.py` | Qwen2.5-VL, schema-locked JSON per `doc_type`, evidence = OCR block ids; **repair + one retry + lenient fallback** (marks `_partial`); re-extract purges the prior attempt's rows first | `extraction`, `clinical_fact` (+ `medication_detail`), `fact_provenance`, `patient_identity`, `encounter` |
| S5 | Terminology | `terminology/service.py`, `terminology/seed.py` | curated exact + alias + `difflib` fuzzy → SNOMED CT / LOINC; UCUM unit parse; frequency parse | updates `clinical_fact.code_*`, `medication_detail` |
| **S6** | **Validation + gate** | `validate/rules.py`, `validate/service.py` | **deterministic rules** (value ranges, unit sanity, dose/frequency ceilings, impossible dates, evidence-present, unmapped-critical, duplicate & cross-document contradiction) + confidence calibration + **routing** | `clinical_fact.review_state`, `review_note`, calibrated `confidence_overall`, `fact_conflict`, `review_task` |
| S7 | Longitudinal reconciliation | — | **not built** (S6 does per-patient duplicate/contradiction; no temporal merge / summary graph) | — |
| **S8** | **Human adjudication** | `webapp/review.py`, `webapp/review_page.py` | review console: page image + bbox highlight + OCR span + fact + code + confidence + findings → accept / correct / reject; closes `review_task` | `clinical_fact.review_state` (`clinician_confirmed`/`corrected`/`rejected`), reviewer `fact_provenance`, `audit_log` |
| **S9** | **FHIR projection (gated)** | `fhir/service.py`, `fhir/resources.py` | deterministic map → resources → ABDM `Composition` bundle; **asserts only governed facts**; held facts reported, not asserted; structural lint | `fhir_resource`, `fhir_bundle` (`status = ready_to_share` \| `draft`) |

### 3.3 Deployment topology & persistence model

```
POD  (RunPod, RTX PRO 4500 Blackwell 32 GB, Ubuntu 24.04, torch 2.8+cu128, NO Docker)

/  overlay  (30 GB, WIPED on every restart)          /workspace  (MooseFS, PERSISTENT)
├── apt: postgresql-16, redis, cron   ← reinstalled  ├── cdi/            repo + .venv (system-site-packages)
├── /var/lib/postgresql/16/cdi        ← PGDATA,      ├── hf-cache/       Qwen2.5-VL weights (~16 GB)
│      rebuilt + pg_restore on boot                  ├── minio-data/     original scans + page PNGs  ← the irreplaceable bytes
└── running processes (mlserve,webapp)               ├── redis/          AOF
                                                     ├── backup/cdi.dump pg_dump, refreshed by cron every 15 min
                                                     ├── data/inbox…     folder-watch drop dirs
                                                     └── logs/           mlserve.log, webapp.log, bootstrap.log
```

**Why Postgres is on the overlay:** `/workspace` (MooseFS) forces mode 0777 and
rejects `chown`; Postgres refuses such a `PGDATA`. No loop devices, no `/dev/fuse`,
no `CAP_SYS_ADMIN`, so a userspace ext4 image is impossible. The cluster is
therefore ephemeral and **reconstructed from `/workspace/backup/cdi.dump` on each
boot**. Nothing unique is lost: the scan bytes live in MinIO on `/workspace`, and
every derived row is reproducible from those by re-running the pipeline *and* is
captured in the 15-minute dump.

**Fallback host (E1-S3, owner decision 2026-09-21: native, no Docker):** the same scripts run on
any Ubuntu 24.04 host with an NVIDIA GPU. The only differences are handled in the scripts: torch
2.8.0+cu128 is installed when it is not inherited (App. B), and the printed address is the host's
own (`infra/runpod/lib.sh`). Runbook: `docs/fallback-host.md`.

**One-command lifecycle:** `bash /workspace/cdi/infra/runpod/start_all.sh`
→ `bootstrap_pod.sh` (apt, initdb, restore, `alembic upgrade head`, venv) → start
`mlserve` (:8077) → start `webapp` (:8888) → print the live URL.

### 3.4 Model gateway (`cdi_adapter.mlserve`)

A single long-lived process holding the VLM so pipeline workers stay light and the
serving backend is swappable without touching pipeline code.

```
POST /vlm/generate  { image_b64, prompt, max_tokens, json_schema? } → { text, backend, model, usage }
GET  /healthz       → { status, backend, model, device, loaded, configured_backend }
```

| Backend (`CDI_MLSERVE_BACKEND`) | Use |
|---|---|
| `hf` *(current; to be deprecated)* | transformers `AutoModelForImageTextToText` = Qwen2.5-VL-7B, bf16, `attn_implementation="sdpa"`, `max_pixels` capped (2 MP) to bound VRAM; **lazy-load on first request**, 7B→3B fallback on OOM. One forward pass at a time — this is why Phase 2 (extract) is serialised. |
| `vllm` *(built + A/B tested, then reverted — see below)* | `AsyncLLMEngine`, Qwen2.5-VL, paged KV cache + continuous batching, `guided_json` / XGrammar grammar-locked decoding. 32 GB ⇒ ~15 GB weights + ~12–14 GB KV ⇒ **3–4 concurrent page extractions**. Long image prefill still serialises on compute, so the projected Phase-2 gain was 2–3×, not 5×. **Measured result: no improvement** — the same 3-doc job stayed at 72–74 s wall. Root cause: the per-request compute floor (§7.4) is unchanged by batching when documents arrive too sparsely to actually batch, XGrammar adds token-level decode overhead, and the Phase-2 fan-out (`_extract_sem`/`_stage2_pool` in `webapp/jobs.py`) only overlapped 2 of the 3 documents in practice. `CDI_MLSERVE_BACKEND` was reverted to `hf`; the `vllm` code stays in the tree, dormant, not the active path. |
| `stub` | deterministic keyword responder — CI / no-GPU |

Client side (`cdi_adapter.ml.client`):
- `HttpMLClient` / `StubMLClient` chosen by env.
- `vlm_json(image, prompt, schema)` = generate → `extract_json` (tolerates fences /
  prose) → **`repair_payload`** (rehomes values put in the wrong key, fills required
  confidence, drops stray keys, resolves `cdi:common.defs` `$ref`) → validate
  against a `referencing.Registry` of all local schemas → on repeated failure
  returns the repaired best-effort object with `_partial: true` **instead of
  raising**. A document is never lost to a formatting nit.

### 3.5 Runtime processes & ports

| Process | Port | Exposure | Notes |
|---|---|---|---|
| `cdi_adapter.webapp` | 8888 | **public** via `https://<pod-id>-8888.proxy.runpod.net` | 8888 is the only RunPod-proxied HTTP port on this pod (was Jupyter; Jupyter stopped) |
| `cdi_adapter.mlserve` | 8077 | localhost only | GPU model gateway |
| PostgreSQL | 5432 | localhost | overlay `PGDATA`, `-k /tmp` |
| Redis | 6379 | localhost | Celery broker (folder-watch path) |
| MinIO | 9000 / 9001 | localhost | bucket `cdi-documents` |

---

## 4. Data model (as built)

PostgreSQL 16, `jsonb` throughout. DDL: [`db/schema.sql`](../db/schema.sql);
read-model views: [`db/views.sql`](../db/views.sql); migration:
[`db/alembic/versions/0001_initial_schema.py`](../db/alembic/versions/0001_initial_schema.py).
20 tables in 13 groups; DAL is raw SQL in
[`src/cdi_adapter/repo.py`](../src/cdi_adapter/repo.py) (no ORM drift).

### 4.1 Table groups

| Group | Tables | Purpose |
|---|---|---|
| Ingestion evidence (immutable) | `source_document`, `document_page`, `pipeline_run` | the scan, its normalized pages, every stage run with model name/version + metrics |
| Classification | `doc_classification` | doc_type, specialty, languages, handwritten, page spans, confidence |
| OCR evidence | `ocr_block` | line/word/cell text + `bbox int[]` + `ocr_conf` + reading order + table refs; GIN full-text index |
| Raw extraction | `extraction` | schema-locked VLM payload (`jsonb`) + `evidence_map` (block-id map), pre-normalization, auditable |
| Identity (MPI) | `patient_identity`, `patient_identity_alias` | one durable identity; ABHA / legacy MRN; every name spelling seen |
| Encounters | `encounter` | derived per document; class AMB/IMP; date precision (year/month/day) |
| **Clinical facts (EAV core)** | `clinical_fact`, `medication_detail` | see §4.2 |
| **Provenance** | `fact_provenance` | see §4.3 |
| Conflicts | `fact_conflict` | 3-valued evidence state (`SUPPORTED` / `CONTRADICTED` / `UNKNOWN_NOT_MENTIONED`) — table exists, engine not built |
| FHIR projection | `fhir_resource`, `fhir_bundle` | see §4.4 |
| Human review | `review_task` | queue — one task per validated document, written by S6 (§5 S6 step 5) *(reconciled E1-S8)* |
| ABDM | `abdm_care_context`, `abdm_consent`, `abdm_transfer` | linking / consent / encrypted push — schema only |
| Audit | `audit_log` | every create/update/read with actor + detail |
| Read model (views) | `v_problem_list`, `v_medication_list`, `v_allergy_list`, `v_results_grid`, `v_encounter_timeline` | flattened lists clinicians read |

> *Reconciled (E1-S8):* both models are authoritative — §4.1–§4.4 for the per-document pipeline and
> §4.6 (re-derived from the live schema) for the canonical / review layer. Live totals: 37 tables
> (20 per-document, listed above and created by `db/schema.sql` / migration 0001; plus
> `patient_registry`, the clinic's own master list from migration 0003 (§5 "Patient registry"); plus
> 15 canonical/review tables (§4.6); plus `alembic_version`) and 5 views.
>
> This table describes the **per-document** schema (S1–S9, unchanged). A second,
> **longitudinal/review** schema (`cn_*`, `ops_*`/`fin_*`, `ai_*`, `rv_*`) was
> added on top of it later in this cycle — see §4.5 for what it is and the
> caveat on how current this section is.

### 4.2 The EAV clinical-fact core

`clinical_fact` is an OpenMRS-`obs` / Cerner-`CLINICAL_EVENT` lineage: one row per
atomic assertion, only the relevant value column filled.

```
clinical_fact
  patient_id, encounter_id
  fact_type        condition | symptom | finding | lab_result | vital_sign |
                   procedure | medication | allergy | immunization |
                   diagnostic_report | advice | care_plan | referral | document
  code_system, code, code_display, code_status   -- unmapped | candidate | bound | local_only
  local_text                                     -- verbatim clinical phrase
  value_kind  value_num  value_unit_ucum         -- quantity
  value_code_system/code/display                 -- codeable value
  value_text  value_bool  value_low/high_num
  ref_range_low/high/text  abnormal_flag         -- lab context
  clinical_status  verification  onset  effective_time  asserted_time
  extraction_id  source_doc_ids[]
  confidence_overall / _ocr / _extract / _terminology   -- fused
  review_state     pending | auto_accepted | in_review | clinician_confirmed | corrected | rejected
  supersedes  is_current  dedup_key

medication_detail (1:1 with a medication fact)
  drug_text, rxlike_system/code, form, strength_num/unit,
  dose_num/dose_unit_ucum, route, frequency_code, frequency_per_day,
  duration_days, prn, instructions, intent (order | record)
```

### 4.3 Provenance (first-class)

Every `clinical_fact` gets ≥1 `fact_provenance` row:

```
fact_provenance
  fact_id → clinical_fact
  source_doc_id, page_id
  ocr_block_ids uuid[]          -- the exact OCR spans that support the fact
  bbox_union   int[]            -- highlight rectangle for a reviewer UI
  extracted_text               -- the substring the value came from
  pipeline_run_ids uuid[]       -- classify+ocr+extract chain
  model_stack  jsonb            -- {classifier, ocr, extractor, terminology, projector} names+versions
  agent        'system' | <clinician id>
  recorded_at
```

Enables: clinician review with pixel highlight, medico-legal traceability, model
evaluation, and reprocessing when a better model ships.

### 4.4 FHIR projection tables

```
fhir_resource   (resource_type, fhir_id, version_id) UNIQUE
  patient_id, encounter_id, profile text[], resource jsonb,
  derived_from_facts uuid[], validation_status (pending|valid|warning|error), validation_issues jsonb

fhir_bundle
  patient_id, encounter_id, care_context,
  artifact_type   PrescriptionRecord | DiagnosticReportRecord | OPConsultRecord |
                  DischargeSummaryRecord | WellnessRecord | HealthDocumentRecord | ...
  bundle jsonb    -- Bundle(type=document): Composition + all referenced resources + Provenance
  bundle_hash char(64), fhir_version '4.0.1', ig_package 'nrces.fhir.r4.ndhm#6.5.0',
  validation_status, status (draft|validated|ready_to_share|shared|superseded)
```

### 4.5 Canonical EMR + review-workbasket layer (added, later in this cycle)

> Recorded from this development cycle's implementation record (see the update
> note at the top of this document and its caveat). Not re-derived from a live
> `pg_dump --schema-only` for this revision.

A canonical, longitudinal EMR schema was added on top of §4.1–§4.4's
document-scoped model, migration `db/alembic/versions/0004_canonical_emr.py`,
organized in four table groups:

| Layer | Prefix | Purpose |
|---|---|---|
| Clinical (canonical) | `cn_*` | Longitudinal, patient-scoped clinical record — e.g. `cn_lab_result` — read by the Customer-360 view rather than per-document `clinical_fact` rows |
| Operations / finance | `ops_*` / `fin_*` | Counter-side and billing-adjacent state |
| AI provenance | `ai_*` | `ai_event` → `ai_suggestion` → `ai_clinical_verification` chain — a longitudinal analogue of §4.3's per-fact `fact_provenance` |
| Review / workbasket | `rv_*` | `rv_item`, `rv_item_element`, `rv_action` — the queueing/assignment model behind the `/reviewer` workbasket (below) |

**Promotion path:** `canon/repo.py` + `canon/promote.py` —
`promote_review_item(rv_item_id, reviewer)` moves an adjudicated workbasket
item into the canonical layer; `fhir/canonical.py`'s `build_bundle(sess,
rv_item_id)` and `fhir/validate_abdm.py`'s `validate(bundle, artifact)`
generate and check the resulting bundle. **Verified on-pod**: a real patient
("Ramesh Kumar", dual ABHA + legacy-MRN identifiers) promoted end-to-end to a
`PrescriptionRecord` bundle passing the Python-conformance validator (§4.4's
`_lint` is the structural check; this is a separate, more thorough conformance
pass, still **not** the real HAPI validator — that gap in §13 is unchanged).
One bug found and fixed during this build: `cn_lab_result`'s test-code column
is `test_code`, not `code` — both `webapp/reviewer.py`'s `c360()` and
`webapp/admin.py`'s `patient_graph()` had queried the wrong name.

**Workflow (workbasket → worklist → promote → bundle):** items land in a
shared **workbasket**; a reviewer claims one into their **worklist**, works
the held facts, and approves/rejects; on approve, `promote_review_item` runs
and the FHIR bundle is (re)generated. This is the assignment/queueing
mechanism §13's S8 gap row asked for (below) — a queue-age/SLA-timer layer on
top of it is not yet built.

This addition is a genuine **schema/architecture change**, not just a UI
change, and is not yet reflected in §4.1's table-group list above or in the
ER-level detail §4.2–§4.4 give the document-scoped model. Treat §4.1–§4.4 as
authoritative for the **per-document** pipeline (S1–S9, unchanged) and this
section as the **longitudinal/review** layer built alongside it.

### 4.6 Canonical EMR + review layer — re-derived from the live schema (E1-S8)

> Source: `pg_dump --schema-only` of a live database at Alembic head `0004`, taken 2026-09-21 and
> kept at [`docs/schema/cdi_schema.sql`](schema/cdi_schema.sql). This replaces the caveated,
> recorded-not-dumped description in §4.5 as the authoritative column list. It describes the
> rebuilt system in this repository; the reconciliation against the original pod (§16) is still
> scheduled.

| Table | Columns (live catalogue) |
|---|---|
| `ai_clinical_verification` | id, ai_suggestion_id, reviewer, decision, final_value, decided_at |
| `ai_event` | id, rv_item_id, source_document_id, kind, model_stack, created_at |
| `ai_suggestion` | id, ai_event_id, fact_id, element_type, suggested, confidence, created_at |
| `cn_allergy` | id, patient_id, rv_item_id, source_fact_id, encounter_id, review_state, recorded_by, recorded_at, code_system, code, substance, reaction, clinical_status |
| `cn_clinical_note` | id, patient_id, encounter_id, note_type, body, author, source, created_at |
| `cn_condition` | id, patient_id, rv_item_id, source_fact_id, encounter_id, review_state, recorded_by, recorded_at, code_system, code, display, clinical_status, verification, onset |
| `cn_fhir_bundle` | id, patient_id, rv_item_id, artifact_type, bundle, bundle_hash, validation, status, created_at |
| `cn_lab_result` | id, patient_id, rv_item_id, source_fact_id, encounter_id, review_state, recorded_by, recorded_at, test_system, test_code, test_display, value_num, value_text, unit_ucum, ref_range_low, ref_range_high, ref_range_text, abnormal_flag, effective_time |
| `cn_medication` | id, patient_id, rv_item_id, source_fact_id, encounter_id, review_state, recorded_by, recorded_at, code_system, code, drug_text, strength_num, strength_unit, dose_num, dose_unit_ucum, route, frequency_code, frequency_per_day, duration_days, instructions, status |
| `cn_vital_sign` | id, patient_id, rv_item_id, source_fact_id, encounter_id, review_state, recorded_by, recorded_at, code_system, code, display, value_num, value_text, unit_ucum, effective_time |
| `fin_billing_item` | id, patient_id, visit_id, source_cn_table, source_cn_id, description, amount, status, created_at |
| `ops_patient_visit` | id, patient_id, visit_date, counter, status, created_at |
| `patient_registry` | patient_id, name, mobile, dob, gender, address, abha_id, created_at, updated_at |
| `rv_action` | id, rv_item_id, actor, action, detail, at |
| `rv_item` | id, patient_id, source_document_id, job_id, doc_type, status, assigned_to, claimed_at, decided_at, created_at |
| `rv_item_element` | id, rv_item_id, fact_id, element_type, payload, state, created_at |

**Promotion path (as built):** `/reviewer` workbasket → claim into a worklist → reviewers decide
held facts with the S8 endpoint → approve → `canon.promote.promote_review_item` copies every
**governed** fact into `cn_*` (a fact still held is not promoted and gets no verification record),
records `ai_event → ai_suggestion → ai_clinical_verification`, builds the bundle with
`fhir.canonical.build_bundle`, checks it with `fhir.validate_abdm.validate`, and stores it in
`cn_fhir_bundle`. `ops_patient_visit`, `fin_billing_item` and `cn_clinical_note` exist but are not
fed yet (backlog E5-S4, E7-S7). The §4.5 bug fix holds: `cn_lab_result`'s test-code column is
`test_code`.

**Pages / endpoints of this layer:** `GET /reviewer`, `GET /api/reviewer/workbasket`,
`GET /api/reviewer/worklist`, `POST /api/reviewer/items/{id}/claim|approve|reject`,
`GET /api/reviewer/items/{id}/elements|bundle`, `GET /admin` (server-rendered; `?table=`, `?mpi=`).
Customer 360 (`/reviewer/c360`) is backlog E7-S8.

---

## 5. Pipeline implementation — stage by stage

### S1 — Ingest & normalize · `cdi_adapter.ingest`

`ingest_bytes(raw, filename, mime_type, source_channel, legacy_ref, legacy_patient_ref, …)`:

1. SHA-256 → if a `source_document` with that hash exists, **return it (idempotent
   dedupe)** and audit a read.
2. Store original bytes → `s3://cdi-documents/documents/<sha[:2]>/<sha>/original.<ext>`.
3. Insert `source_document` (status `received`), audit `create`.
4. Open `pipeline_run(stage='ingest')`.
5. `render_pages(raw, mime)` — PyMuPDF for PDF (one PNG/page at `CDI_PAGE_DPI`=200),
   Pillow for images, multi-frame TIFF supported.
6. `normalize_image(png)` per page: grayscale → **deskew** (`cv2.minAreaRect` on
   Otsu-thresholded ink pixels; rotate if `0.15° < |skew| ≤ 15°`) → `fastNlMeansDenoising`
   → CLAHE. `preproc` jsonb records `skew_deg` + `steps`. Original bytes are never
   altered — these are derived render artifacts.
7. Store `pages/NNNN.png` + `pages/NNNN.thumb.png`; insert `document_page`.
8. `status='pages_rendered'`, finish run, audit, enqueue S2 (Celery path) / return.

Failure → `pipeline_run` `failed`, `source_document.status='error'`, error detail
persisted, exception re-raised.

### S2 — Classify · `cdi_adapter.classify`

Runs **after S3**, so the full page OCR text is already on hand.

- **`_heuristic_classify(text)`** — a scored keyword classifier (`_SIGNALS`: ~35
  weighted word-boundary patterns across the 8 doc types, weight 3 = near-unique
  anchor, 2 = strong, 1 = corroborating). It returns a type **only** when the
  winner scores **≥ 5** *and* leads the runner-up by **≥ 3** on a page that is
  ≥ 180 chars and ≥ 75 % alphanumeric (i.e. cleanly OCR'd, almost certainly
  printed). Every other page — weak signal, tie, sparse/garbled text, likely
  handwriting — returns `None` and falls through to the VLM. Deliberately
  conservative: it trades coverage for *never* handing S4 the wrong schema.
  - This is the fix for the earlier mis-label bug: the old substring matcher hit
    *"lab"* inside *"Cardiac Cath **Lab**oratory"* on a prescription. The scored
    version needs *"laboratory report"* / *"reference range"* as phrases; a
    cardiology prescription scores 9+ for `prescription`, ~0 for `lab_report`.
- **Fallback path (ambiguous / handwritten):** `build_classification_prompt` (11
  `doc_type` values + `specialty` / `is_handwritten` / `languages` / `page_spans` /
  `confidence` rules) → `client.vlm_json(page1_image, prompt, classification.v1)`.
- The hint text is taken from the persisted `ocr_block` rows (S3 already ran);
  `_ocr_hint(page1)` — a one-off RapidOCR pass — is kept only as a fallback when
  no blocks exist (the Celery path, which still classifies before OCR).
- Insert `doc_classification`, finish `pipeline_run(stage='classify')`,
  `status='classified'`, audit. If `is_handwritten`, S1's orchestrator triggers the
  VLM-OCR pass before S4.
- Observed (2026-09-09, 3 real doc types): heuristic classified prescription,
  radiology and lab report **correctly in ~0 s each** — no VLM call. `config.fast_classify`
  now defaults **ON** (it gates the heuristic, not the old substring matcher).

### S3 — OCR / layout · `cdi_adapter.ocr`

- **Runs before classify.** The orchestrator calls `ocr_document(doc,
  force_engine="rapidocr")` first — a fast printed-text pass whose blocks feed the
  S2 heuristic. If S2 then marks the page handwritten, the orchestrator calls
  `ocr_document(doc, force_engine="vlm")`, a second pass that `delete_ocr_blocks`
  then re-inserts, so the VLM transcription cleanly supersedes the RapidOCR one.
  This replaces the old "OCR the page once for a classify hint, once for real"
  double pass.
- `ocr_document(document_id, *, force_engine=None)`: `force_engine` pins
  `"rapidocr"` | `"vlm"`; without it the engine is chosen from the classification
  (`is_handwritten AND CDI_HANDWRITTEN_USES_VLM` → **VLM transcription**
  `vlm_ocr.run_vlm_transcription`, page-level bbox, conf 0.55; else **RapidOCR**).
- RapidOCR (`rapidocr-onnxruntime`, CPU): returns `[quad, text, score]`; kept if
  `score ≥ CDI_OCR_MIN_CONF` (0.30); quad → `bbox [x0,y0,x1,y1]`; `polygon`
  retained.
- `order_reading()` — sort by horizontal band (`round(y_center / (0.7·median_height))`)
  then `x0` → natural top-to-bottom, left-to-right within a line.
- One `pipeline_run(stage='ocr')`; `delete_ocr_blocks_for_document` first
  (idempotent re-run); bulk `insert_ocr_blocks`; `status='ocr_done'`; audit; enqueue S4.
- Observed: printed lab report → 23–39 blocks, line confidences 0.94–1.00.

### Patient registry — look up an existing patient first · `cdi_adapter.mpi.registry`

`patient_registry` (migration 0003, seeded with 4 dummy patients) is the clinic's
own master list: `patient_id` (CareFlow id), `name`, `mobile`, `dob`, `gender`,
`address`, `abha_id`; the composite key is `(mobile, lower(name), dob)`.

- **`GET /api/registry/lookup?q=`** resolves an existing patient by **CareFlow id
  / ABHA id / mobile** (digits-only match, tolerant of dashes/spaces).
- When the upload form's lookup matches, `create_job(patient_ref=…)` calls
  `registry.ensure_identity` (creates the `patient_identity` row from the registry
  data if it doesn't exist yet) and **all documents attach to that patient** —
  document-driven identity resolution is skipped, no new CareFlow id is minted.
- For a **new** patient the flow is unchanged (identity read from the documents,
  `CFP-<YYYY>-<seq>` minted). After processing, the UI shows a **"Complete patient
  details"** form; **`POST /api/registry`** persists `mobile / address / abha …`
  so the patient is found by lookup next time.

### Identity — MPI, fetched from the documents · `cdi_adapter.mpi`

The uploader supplies **only the files** (ABHA optional). Identity is read from the
documents themselves:

- `candidate_from_payload` pulls `name` / `sex` / `age_text` / `dob` from each
  document's extracted `patient` block; `parse_name` strips titles
  (`Dr`/`Mr`/`S/o`/…), `parse_sex` normalises to `M`/`F`/`O`, `parse_age` →
  approximate birth year.
- `resolve_identity` (called by S4 for the first document of a job):
  1. **ABHA** match → existing patient;
  2. else **fuzzy**: `SequenceMatcher` on the normalised name + birth-year within 1
     → existing patient at ratio ≥ 0.90;
  3. else **mint** a new record with `mpi_id = CFP-<YYYY>-<seq>` (Postgres
     `patient_mpi_seq`).
- Every document's raw reading is stored in `patient_identity_alias`.
- After all of a job's documents: `merge_identity_evidence` picks the
  most-supported name / sex / DOB (mode, longest on ties) and sets
  `identity_confidence` from how strongly the documents agreed.
- The FHIR `Patient` carries the CareFlow id as an identifier
  (`system: https://careflow.clinic/mpi`, `use: usual`) alongside ABHA, plus
  `gender` and `birthDate`.

Verified: 3 sample documents, no manual entry → `CFP-2026-000001`, name "Anjali
Das", sex M, DOB 1978-01-01, `identity_confidence 0.99`.

### S4 — Extract · `cdi_adapter.extract`

- Schema by `doc_type` (`extract/prompt.SCHEMA_FOR_DOC_TYPE`): `prescription.v3`,
  `lab_report.v3`, `vitals.v3`, `opd_note.v3` (also referral), `discharge_summary.v3`
  (also operative_note), `radiology.v3`. Unknown type → skip, `status='normalized'`.
- `build_extraction_prompt(doc_type, ocr_blocks)` presents blocks as `[b1] text …`,
  `[b2] …`; `block_id_map` maps `bN → ocr_block uuid`.
- `client.vlm_json(image, prompt, schema, max_tokens=max_tokens_for(doc_type),
  retries=1)` — with repair + `_partial` fallback (see §3.4). **Per-doc-type token
  budget** (prescription / discharge 2600, opd 2400, lab 2000, …) plus doc-type
  guidance ("list EVERY medication line — 5–15 drugs — don't stop early"; radiology
  "copy every finding line") so long list-heavy documents are captured in full.
  Payload stored verbatim in `extraction`.
- **Schemas relaxed** so real VLM output validates on the first pass (retries were
  burning ~10–20 s each on constraints the 7B could never satisfy):
  - `evidence` optional everywhere (`minItems 0`); `quantity` accepts a bare
    number/string (`"value": 150`); list-item objects allow `additionalProperties`.
  - `patient` is **not** root-required on any v3 schema (handwritten notes often
    have no legible name; identity code handles an absent block).
  - `patient.sex` is free text — the model returns `"Male"`; `parse_sex` maps
    `Male/Female/Other → M/F/O` downstream.
  - `radiology.findings_list` / `impression_concepts` / `diagnoses` accept a bare
    string as well as a `{text,system,code}` object.
  Same 10-drug prescription that once yielded **1 fact now yields 19** (10
  medications with dose + frequency + route, 3 conditions, 3 advice, 3 vitals),
  `_partial = false`, no retry.
- **Re-extraction supersedes**: if a document already has an `extraction`,
  `purge_document_facts` clears its facts / extraction / encounter / aliases before
  the new pass writes — re-processing the same file never doubles rows.
- **Medication detail** (`_add_medication`): strength is parsed out of the drug
  name (`METPURE XL 50 MG → Metpure XL / 50 / mg`, preferring a dose unit over a
  pen's fill volume); frequency from the dose-slot column (`1-0-1 → 2/day`,
  `BD → 2/day`, `TWICE IN A YEAR → 0.006/day`) with `"x 15 days"` correctly read as
  *duration*, not frequency; SC/injection route inferred for pens/`Inj`.
- **Existing-patient mismatch guard**: when the job was started against a
  registry patient, every document's extracted name/DOB is checked against that
  patient (`names_match` token-subset + fuzzy ≥ 0.72; `dob_match` ±1 yr). On a
  mismatch the job **stops** (`state = mismatch`), the facts this document just
  wrote are purged, and the UI shows a red card naming both parties — no
  patient id / identity is surfaced.
- **Identity & encounter**: `patient_id` is passed in by the web-app job (created
  once from the form); otherwise `get_or_create_patient` from the payload's
  `patient` block / legacy MRN. One `encounter` per document, class `IMP` for
  discharge/operative else `AMB`, dated from `encounter_date` / `reported_at` /
  `study_date` / `discharge_date` / `recorded_at` (partial-date aware).
- **Payload → facts**: per-`doc_type` handlers (`_facts_prescription`, `_facts_lab`,
  `_facts_vitals`, `_facts_discharge`, `_facts_radiology`, `_facts_opd_note`) using
  **tolerant readers**:
  - `_qty(x)` accepts a `{value,unit,evidence}` object, a bare number, or a string
    like `"500 mg"` / `"138/86"`.
  - `_coded_text(x)` accepts `{text|code|display|name}` or a plain string.
  - `_parse_date(s)` handles `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, `DD-Mon-YYYY`, `DD/MM/YYYY`.
  - Vitals are de-duplicated within a document and only emitted when a numeric value
    was actually read (`_VITAL_ALIAS` normalizes pulse→heart_rate, etc.).
- Each fact: `insert_clinical_fact` (+ `insert_medication_detail`), then
  `insert_fact_provenance` resolving the evidence `bN` ids to `ocr_block` uuids and
  computing `bbox_union`. Fused `confidence_overall = 0.5·(extract_conf + mean OCR conf)`.
- `pipeline_run(stage='extract')` ok with `facts` count; `status='extracted'`; audit;
  enqueue S5.
- Observed (real 7B, 2026-09-09): prescription → 19 facts, angiogram report → 14
  (8 findings + 3 dx + report + procedure + advice), lab report → 6/6 analytes.
  No `_partial`, no retry. Extract is ~14–29 s/doc on this GPU (see §7.3).

### S5 — Terminology · `cdi_adapter.terminology`

- `seed.py`: hand-curated maps — ~35 conditions, ~10 symptoms, ~4 procedure
  families, ~30 lab analytes → LOINC, ~14 vital signs → LOINC, ~15 drugs → SNOMED,
  ~6 allergens → SNOMED, plus `FREQ_PER_DAY`, `ROUTE_SCT`, `UCUM_ALIASES`.
- `bind_fact(fact)`: normalize `local_text` (lowercase, fix `hba1c`, strip
  punctuation) → exact hit → alias → `difflib.get_close_matches(cutoff=0.86)`.
  Medications: strip `tab/cap/syrup/…` then match any token. Sets
  `code_system/code/code_display`, `code_status = bound | local_only`,
  `confidence_terminology = 0.9 | 0.4`. Units → UCUM; `frequency_per_day` parsed
  from `1-0-1` / `BD` / `TDS` / …
- `bind_document(document_id)` updates every fact from that document; `status='normalized'`.
- Observed: labs 5/5 → LOINC; meds 3/3 → SNOMED; allergy → SNOMED; conditions
  bound when in the seed map, else `local_only` (text preserved).

### S6 — Clinical validation + the governance gate · `cdi_adapter.validate`

**No LLM.** `rules.py` holds deterministic checks; each returns
`(severity ∈ {blocker, warn, info}, code, message)`:

| Rule | What it checks |
|---|---|
| `check_value_range` | lab/vital value against an absolute physiological window per LOINC (`LAB_RANGE` / `VITAL_RANGE`); auto-normalizes temperature °F→°C; flags unit mismatch |
| `check_unit_present` | numeric lab/vital has a unit |
| `check_medication` | dose/strength present, frequency present, `0 < freq/day ≤ 6`, total-daily-dose ≤ 1.5× an adult ceiling (`DOSE_MAX_MG_DAY` per SNOMED substance) |
| `check_dates` | `effective_time`/`onset`/`asserted_time` not in the future, not < 1900, within ±400 d of the encounter |
| `check_evidence` | fact has a `fact_provenance` row with ≥1 OCR block id **or** extracted text |
| `check_terminology` | condition/medication/allergy/procedure is `bound` (else a `warn`) |

`service.validate_document(document_id)`:
1. Reads the fact set + the extraction row → `partial = payload._partial`.
2. Per fact: run all rules; `_calibrate(conf, partial, n_warn, n_block)` —
   an honest placeholder: subtract `CDI_GATE_PARTIAL_PENALTY` (0.30) when the
   extraction was `_partial`, cap at 0.5 on any blocker, 0.9 on any warn.
   *(roadmap: fitted isotonic regression per `doc_type × fact_type`.)*
3. **Gate → `review_state`:**
   | Condition | Result |
   |---|---|
   | any `blocker` **or** `_partial` **or** `conf < CDI_GATE_REVIEW_FLOOR` (0.85) **or** medication with a `med-*` finding | `in_review` |
   | `conf ≥ CDI_GATE_AUTO_ACCEPT_CONF` (0.985) **and** zero findings | `auto_accepted` |
   | `conf ≥ CDI_GATE_AUDIT_CONF` (0.95) **and** zero blockers | `auto_accepted` (+ `audit_sample_rate` → a low-priority audit `review_task`) |
   | otherwise | `in_review` |
4. Cross-document: `find_similar_current_facts` (same patient, `fact_type`, `code`)
   → same-day different value ⇒ `fact_conflict(value_mismatch, CONTRADICTED,
   blocker)` and both facts forced `in_review`; same-day same value ⇒
   `fact_conflict(duplicate, SUPPORTED, info, auto_resolution=keep_a)`.
5. One `review_task` per document listing every held fact id, `kind` inferred
   (`dose_check` / `unmapped_terminology` / `low_confidence`), `payload` carries
   the structured findings.
6. `pipeline_run(stage='validate')` with counts; `status='validated'`; audit.

Observed: a `_partial` prescription extraction → **0 auto-accepted, all facts
`in_review`** — exactly the intended behaviour ("malformed/uncertain extraction
never silently becomes trusted clinical data").

### S8 — Human adjudication · `cdi_adapter.webapp.review`

- `GET /review` — HTML queue (`review_page.py`): open `review_task`s grouped by
  document, each held fact shown with confidence + proposed code.
- `GET /api/review/facts/{fact_id}` (`review.fact_detail`) assembles: the fact, its
  `medication_detail`, every `fact_provenance` row → resolved `ocr_block` texts +
  the `bbox_union` + the page image URL (`/api/documents/{id}/pages/{n}`) + the
  rule findings.
- The page renders the **page image with the evidence bbox drawn over it**, the OCR
  span, the extracted value, the proposed code, the confidence, the findings, and
  three actions:
  - **accept** → `review_state='clinician_confirmed'`
  - **correct** → `apply_fact_correction` (whitelisted fields: text, code(+system),
    value/unit, status, ranges) → `review_state='corrected'`, `supersedes` chain
  - **reject** → `review_state='rejected'`, `is_current=false`,
    `clinical_status='entered-in-error'`
- Every decision writes a `fact_provenance` row with `agent = <reviewer>` and
  `model_stack = {"reviewer": "human"}`, and closes the `review_task` once none of
  its facts remain `in_review`. Then `GET /api/jobs/{id}/fhir` **re-projects live**,
  so the bundle now asserts the newly-governed facts.

### S9 — FHIR projection (gated) · `cdi_adapter.fhir`

- `resources.py` — pure dict builders, references as `urn:uuid:` full-urls (ABDM
  document-bundle style). Profiles asserted in `meta.profile` against
  `https://nrces.in/ndhm/fhir/r4/StructureDefinition/…`.
- `ARTIFACT` map: `doc_type → (profile, Composition.type SNOMED coding, title)`:
  prescription→`PrescriptionRecord`, lab/radiology→`DiagnosticReportRecord`,
  opd_note/referral→`OPConsultRecord`, discharge/operative→`DischargeSummaryRecord`,
  vitals→`WellnessRecord`, else `HealthDocumentRecord`.
- `project_document(document_id)`:
  1. Load facts + med details + patient + encounter; base64 the original scan
     (≤ 12 MB) for the `DocumentReference.content.attachment.data`.
  2. Emit core actors: `Patient` (ABHA identifier `https://healthid.ndhm.gov.in`),
     `Organization` (HFR), `Practitioner` (HPR), `Encounter`.
  3. `fact_type → resource`:
     | fact_type | resource | binding |
     |---|---|---|
     | condition | `Condition` | SNOMED CT + clinical/verification status |
     | symptom | `Observation` (exam) | SNOMED CT |
     | finding | `Observation` (exam) | — |
     | lab_result | `Observation` (laboratory) | LOINC + UCUM `valueQuantity`, `referenceRange`, `interpretation` |
     | vital_sign | `Observation` (vital-signs) | LOINC + UCUM |
     | medication | `MedicationRequest` | SNOMED CT drug, `dosageInstruction` (dose, timing `frequency/period`, route), `dispenseRequest` duration |
     | procedure | `Procedure` | SNOMED CT, `performedDateTime`, note = findings |
     | allergy | `AllergyIntolerance` | SNOMED CT substance + reaction manifestation |
     | diagnostic_report | `DiagnosticReport` | LOINC/SNOMED, `result[]` → the lab Observations, `conclusion` |
  4. `DocumentReference` (the scan) + `Provenance` (`target` = every emitted
     clinical resource + the DocumentReference; `entity` = source; `agent` =
     assembler with the model stack string).
  5. `Composition` with sections (Chief complaints / Diagnosis / Investigations /
     Medications / Procedures / Allergies / Vital signs / Document reference /
     Advisory notes) → `Bundle(type=document)`.
  6. `_lint(bundle)` — structural checks: first entry is a Composition; **no
     dangling `urn:uuid:` references**; Observations have a value; Conditions coded.
     Errors/warnings recorded on every `fhir_resource` + `fhir_bundle` row.
  7. Persist `fhir_resource` rows + one `fhir_bundle` (`validation_status`,
     `status='validated'` when clean); `pipeline_run(stage='project')`;
     `source_document.status='projected'`; audit.
- `project_patient(patient_id)` — every document for that patient → its bundle;
  returns `{patient, artifact_count, bundles:[{filename, doc_type, artifact_type,
  issues, bundle}]}`.
- Observed last clean run: 3 bundles, **0 structural error issues**, meds and labs
  fully coded.

---

## 6. FHIR / ABDM mapping notes

- **Bundle** = `type: "document"`, `identifier` `urn:cdi:bundle`, `timestamp`, first
  `entry` is the `Composition`, all references are `urn:uuid:`.
- **Composition.type** uses SNOMED CT (e.g. Prescription record `440545006`,
  Diagnostic studies report `721981007`, Discharge summary `373942005`).
- **Patient.identifier** carries the ABHA number (`https://healthid.ndhm.gov.in`)
  and, when present, ABHA address and the legacy MRN (`urn:cdi:legacy-mrn`).
- **The gate is enforced here:** `project_document` splits facts into `governed`
  (`review_state ∈ {auto_accepted, clinician_confirmed, corrected}`) and `held`.
  Only governed facts become FHIR resources and Composition entries. Held facts are
  returned in `held_facts` and added as `info` issues; the bundle is
  `status='draft'` (`validation_status='warning'`) until they are all adjudicated,
  then `status='ready_to_share'` (`'valid'`). Structural errors → `status='draft'`,
  `'error'`.
- **Provenance** is always included — the record is never worse than "the scan,
  shared digitally"; even a fully-held extraction yields a valid, `draft`
  `HealthDocumentRecord` carrying the image.
- **FHIR content validation today = structural lint only** (`_lint`: Composition
  first, no dangling `urn:uuid:`, Observations valued, Conditions coded). Full HAPI
  + `nrces.fhir.r4.ndhm` IG profile validation is a named gap (§13).

---

## 7. Web application (`cdi_adapter.webapp`)

Branded **CareFlow Polyclinic** — white + blue, inline-SVG logo, system fonts, no
build step / no CDN. `theme.py` holds the palette tokens, type scale, spacing,
component styles and the shared header; `page.py` (upload + results) and
`review_page.py` (S8 console) render from it. The upload form has **no name/sex
fields** — a drag-and-drop dropzone, an optional ABHA input, and a note that
identity is read from the documents. After processing, a **patient banner** shows
the generated `CFP-…` id, name, sex, DOB and identity confidence.

### 7.1 Endpoints

| Method / path | Purpose |
|---|---|
| `GET /` | single-page upload UI (`page.py`, dark theme, no build step) |
| `GET /healthz` | `{db, s3, mlserve}` checks |
| `POST /api/jobs` | multipart: `abha?` + `files[]` (≤10) → `{job_id}` (202). No name/sex — read from the documents. |
| `GET /api/jobs/{id}` | job + `patient` (`mpi_id`, name, sex, DOB, identity_confidence) + per-document `{status, step, doc_type, facts, accepted, in_review}` |
| `GET /api/jobs/{id}/fhir` | `project_patient` result (all bundles) |
| `GET /api/jobs/{id}/fhir/download` | same as a downloadable `fhir_<job>.json` |
| `GET /api/patients/{id}/fhir` | bundles for an existing patient |
| `GET /api/documents/{id}/bundle` | single-document bundle (non-persisting) |
| `GET /api/documents/{id}/evidence` | doc_type + OCR block count + facts table |
| `GET /api/documents/{id}/pages/{n}` | the normalized page PNG (used by the review UI) |
| `GET /review` | S8 human review console (page) — document-scoped, per §5's S8 |
| `GET /api/review/tasks` | open `review_task`s with their held facts |
| `GET /api/review/facts/{fact_id}` | fact + provenance + OCR blocks + bbox + page image url + findings |
| `POST /api/facts/{fact_id}/review` | `{action: accept\|correct\|reject, corrections?, reviewer?, note?}` |
| `GET /reviewer` *(added — §4.5, caveat applies)* | workbasket/worklist console — the human-in-the-loop entry point moved here; the upload page (`/`) no longer embeds an inline editor, only a "sent to the reviewer" confirmation panel |
| `GET /api/reviewer/workbasket`, `/worklist` | shared queue, and the current reviewer's claimed items |
| `POST /api/reviewer/items/{id}/claim\|approve\|reject` | move an item from workbasket to a reviewer's worklist; approve promotes it (§4.5) |
| `GET /api/reviewer/items/{id}/elements`, `/bundle` | the item's held facts/elements; the generated FHIR bundle |
| `GET /reviewer/c360`, `/api/reviewer/c360/{mpi}` | Customer-360 read-model: one patient's canonical facts + dual identifiers across documents |
| `GET /admin` *(added — §4.5, caveat applies)* | data inspector over the canonical/ops/ai/review tables, for verifying they're populated correctly |

`GET /api/jobs/{id}/fhir` re-runs `project_patient` **live** each call, so it always
reflects the latest review decisions.

### 7.2 Job model (`webapp/jobs.py`) — parallel, then human-edit, then generate

- **Phase 1 (parallel).** `ThreadPoolExecutor(max_workers=max(2, job_max_workers))`,
  `job_max_workers = 6` — every document runs `ingest → OCR(rapidocr) → classify →
  [OCR(vlm) if handwritten]` concurrently. `classify` is the scored heuristic
  (`config.fast_classify` ON); it makes **no VLM call** for a clean printed
  prescription / lab report / vitals / radiology report.
- **Phase 2 (serial).** `extract → terminology → validate` per document, serialised
  on the single GPU **because the `hf` backend does one forward pass at a time** —
  firing them concurrently would OOM, not speed up. This lock is removed only once
  the `vllm` backend lands (Roadmap #1 / §7.4). Extract uses one retry
  (`config.extract_retries = 1`). No document ever shows "queued" — the grid shows
  each cell `pending → running → done`.
- The job then stops at **`state = review`** (it does **not** auto-project).
- **`GET /api/jobs/{id}/facts`** → every fact grouped by document, each with the
  page-image URL and its evidence `bbox` — the inline editor renders the **scanned
  image on the left, an editable fact table on the right** (Type / Text / Value /
  Unit / Code; untick a row to drop it; row hover highlights the bbox on the scan).
- **`POST /api/jobs/{id}/generate`** `{edits:[{fact_id, action:keep|drop, corrections?}]}` —
  applies the reviewer's decisions (`keep`→`clinician_confirmed`, `corrections`→
  `corrected`, `drop`→`rejected`) then re-projects. Because scans over 700 KB are
  referenced by URL rather than base64-inlined, **bundle generation is ~140 ms for
  3 documents**. All kept facts are governed → the bundles come back
  `ready_to_share`.
- The front page (`/`) header is **"Intelligent OCR driven Agentic EMR
  generation"**; the "Generate EMR" card shows a stage-tab row (Ingest…Validate) and
  a document × stage grid with a green tick per completed cell.

### 7.3 Timing — measured (RTX PRO 4500 Blackwell 32 GB, 7B bf16, hot model)

Same 3-document job (`prescription`, `angiogram`, `lab report`), one patient:

| stage | 2026-09-08 | + classifier / OCR-once | + CPU thread caps | change |
|---|--:|--:|--:|---|
| ingest ×3 (parallel) | 9.2 / 0.3 / 0.9 | 1.4 / 16.4 / 16.4 | **7.4 / 4.2 / 7.2** | thread caps kill the render/OCR thrash |
| **classify ×3** | 21.6 / 31.8 / 36.2 (VLM) | 0.0 / 0.0 / 0.0 | **0.0 / 0.0 / 0.0** (heuristic) | no VLM call |
| **OCR ×3** | 9.8 / 4.6 / 23.1 **+ hidden 2nd pass** | 10.4 / 8.8 / 7.0 | **2.1 / 2.6 / 1.8** | one pass; RapidOCR ~3× faster once it stops thrashing 128 threads on 13 cores |
| **extract ×3 (serial, GPU)** | 17.6 / 30.5 / 31.6 | 28.8 / 13.9 / 21.0 | **28.8 / 13.8 / 21.0** | angiogram 3 passes → **1**; no `"Male"` retry; GPU-bound, unchanged by CPU work |
| terminology + validate + project | < 0.1 | < 0.1 | < 0.1 | — |
| **total wall (3 docs)** | **~140 s** | ~90 s | **74.2 s** | **−47 %** |

Extraction facts unchanged (19 / 14 / 6); zero `_partial`, zero schema retries.
**Extract is now 86 % of wall time** — the only remaining lever is the GPU
(§7.4 #1–3). First job after a restart still pays a one-off ~35 s model load.

### 7.4 Performance plan & the latency floor

**The floor is one VLM forward pass per distinct document.** On this GPU a
schema-locked Qwen2.5-VL-7B pass over a full-page image + ~2 k output tokens is
**~8–15 s** — arithmetic (FLOPs ÷ throughput), not removable waste. "Almost zero"
latency is **not** reachable with a generalist 7B; it needs a distilled/quantized
`cdi-dslm` (Roadmap #1), or *not calling the VLM* for documents a layout model +
deterministic parser can handle, or precompute/cache.

Planned changes and their honest effect:

| # | Change | Effect | Notes / risk |
|---|---|---|---|
| 1 | **`hf` → `vllm` async engine** (`AsyncLLMEngine`, paged KV, continuous batching) | *Projected* 2–3× throughput on Phase 2. **Built, deployed and A/B tested later this cycle — measured effect was zero** (72–74 s, unchanged); reverted to `hf`. See §3.4 for the finding and root-cause analysis; the code stays in the tree, dormant. | 32 GB ⇒ ~15 GB weights + ~12–14 GB KV ⇒ 3–4 concurrent page extractions, not 5 (large image prefill still serialises on compute). |
| 2 | **Remove the Phase-2 serialisation lock** in `webapp/jobs.py` | Built alongside #1 (`_extract_sem`/`_stage2_pool` fan-out) — only overlapped 2 of 3 documents in the measured run, part of why #1 showed no gain. Dormant with #1 since the revert to `hf`. | Phase 1 is *already* parallel (`ThreadPoolExecutor`, 6 workers); the "`max_workers=1`" premise is not the current code. Celery for web uploads is a *scaling/durability* change (Roadmap #8), not a latency lever. |
| 3 | **Grammar-locked decoding** (vLLM `guided_json` / XGrammar) | Built and tested as part of the same #1 effort — guarantees JSON *validity* but adds token-level decode overhead; contributed to, not against, the flat result. **Not clinical fidelity** either way — S6 + human review stay load-bearing. | Needs the v3 schemas *re-tightened* (they were loosened to stop retries) so the grammar actually constrains. Currently dormant with #1. |
| 4 | **CPU thread hygiene** — *done (2026-09-09).* `cdi_adapter/_cpu.py` reads the cgroup CPU quota (pod: ~13.6, not the 128 the host reports) and caps `OMP`/`OPENBLAS`/`MKL`/`RAYON`/ORT threads to `(budget-1) ÷ job_max_workers` before NumPy/OpenCV/ONNXRuntime load; `cv2.setNumThreads` + `RapidOCR(intra_op_num_threads=…)`. | ingest 16 s → ~7 s, RapidOCR 7–23 s → ~2 s (it was thrashing 128 threads on 13 cores). 3-doc wall **90 s → 74 s**. | shipped. |

**Realistic target with #1–3 also done:** a 5-document job in **~20–35 s wall**,
~12–18 s perceived per document, `_partial` effectively gone — a solid **3–4×**
on top of today's 74 s, "review starts in well under a minute". Not "almost zero".

---

## 8. End-to-end flow

### 8.1 Sequence (web-app path)

```
User        webapp            jobs(thread)     mlserve(VLM)   RapidOCR    Postgres/MinIO
 │  POST /api/jobs (5 files)   │                │              │           │
 │ ───────────────────────────▶│ create patient ───────────────────────────▶ patient_identity
 │  202 {job_id}               │                │              │           │
 │                             │ for each file: │              │           │
 │                             │  ingest ───────────────────────────────────▶ source_document, pages→MinIO
 │                             │  ocr (rapidocr) ───────────────▶ blocks     │
 │                             │  ─────────────────────────────────────────▶ ocr_block
 │                             │  classify (heuristic on OCR text; VLM only  │
 │                             │            if ambiguous/handwritten)        │
 │                             │  [ocr (vlm) if handwritten] ───▶ blocks     │
 │                             │  extract ─────▶ /vlm/generate (schema)      │
 │                             │        ◀──────  JSON (repair/_partial)      │
 │                             │  ─────────────────────────────────────────▶ extraction, clinical_fact, fact_provenance
 │                             │  terminology (seed map, in-proc) ─────────▶ update clinical_fact.code_*
 │  GET /api/jobs/{id} (poll)  │                │              │           │
 │ ◀───────────────────────────│ per-doc status │              │           │
 │                             │ project_patient ─────────────────────────▶ fhir_resource, fhir_bundle
 │  GET /api/jobs/{id}/fhir    │                │              │           │
 │ ◀───────────────────────────│ bundles JSON   │              │           │
```

### 8.2 Worked example — "Anjali Das", 3 documents (last verified run)

| Upload | S2 | S4 facts | S5 | S9 artifact | Result |
|---|---|---|---|---|---|
| `prescription_00.pdf` | `prescription` 0.95 | 9 (2 conditions, 3 meds, BP, weight) | T2DM→SCT 44054006, HTN→59621000; Metformin→372567009, Amlodipine→386864001, Atorvastatin→373444002; BP→LOINC 8480-6/8462-4; weight→29463-7 | `PrescriptionRecord`, 15 resources | **0 errors** |
| `lab_report_01.pdf` | `lab_report` 0.95 | 5 lab results | HbA1c→LOINC 4548-4 (7.8 %), FBS→1558-6 (142 mg/dL), creatinine→2160-0, cholesterol→2093-3, LDL→2089-1 | `DiagnosticReportRecord`, 12 resources | **0 errors** |
| `vitals_sheet_02.pdf` | `vitals_sheet` 0.95 | 1 (Penicillin allergy) | Penicillin→SCT 373270004 | `WellnessRecord`, 8 resources | **0 errors** |

Output: `GET /api/jobs/{id}/fhir` → 3 `Bundle(type=document)`; each downloadable;
15 `clinical_fact` rows + 15 `fact_provenance` rows + 35 `fhir_resource` rows in
Postgres.

> *Golden path (E1-S6):* this table is the recorded run of the real 7B model and is the nightly
> `real_vlm` baseline in `tests/golden/anjali_das.json`. The deterministic stub backend follows §5
> S4 and emits all six numeric vitals on the vitals sheet plus the allergy (7 facts), so the CI
> `stub` baseline is 9 + 5 + 7 = 21 facts, 37 resources, 3 bundles, 0 structural errors — the
> difference is the 7B vitals weak spot recorded in §13. Counts are on a fresh database: the
> Patient and Organization are attributed to the first document that creates them.

---

## 9. Configuration (`cdi_adapter.config.Settings`, env prefix `CDI_`)

| Key | Default (pod) | Meaning |
|---|---|---|
| `CDI_DATABASE_URL` | `postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi` | adapter DB |
| `CDI_REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker |
| `CDI_S3_ENDPOINT_URL` / `_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` | MinIO on `127.0.0.1:9000`, `cdi-documents` | object store |
| `CDI_SECRETS_DIR` | `/run/cdi-secrets` (pod) | secrets store for the four credentials above (DB/Redis URLs, S3 keys): one 0600 file each, loaded by `get_settings()`; the defaults above are local-dev only — see `docs/secrets.md` *(added E1-S5)* |
| `CDI_INBOX_DIR` / `_PROCESSED_DIR` / `_FAILED_DIR` | `/workspace/data/*` | folder-watch |
| `CDI_PAGE_DPI` | 200 | render resolution |
| `CDI_WEBAPP_PORT` | **8888** | the RunPod-proxied port |
| `CDI_MLSERVE_URL` / `_PORT` | `http://127.0.0.1:8077` / 8077 | model gateway |
| `CDI_MLSERVE_BACKEND` | `hf` | `hf` \| `stub`; `vllm` was built, A/B tested and reverted (§3.4) — dormant, rebuilt under backlog E2-S1 *(reconciled E1-S8)* |
| `CDI_VLM_MODEL_ID` / `_FALLBACK_MODEL_ID` | `Qwen/Qwen2.5-VL-7B-Instruct` / `…-3B-…` | VLM + OOM fallback |
| `CDI_VLM_MAX_PIXELS_OCR` | 2 000 000 | processor cap (VRAM bound) |
| `CDI_FAST_CLASSIFY` | `true` | S2 scored heuristic first; VLM only on ambiguous/handwritten |
| `CDI_EXTRACT_RETRIES` | `1` | S4 VLM re-tries on schema failure (schemas relaxed → rare) |
| `CDI_JOB_MAX_WORKERS` | 6 | Phase-1 documents in flight concurrently |
| `CDI_OCR_ENGINE` / `CDI_HANDWRITTEN_USES_VLM` | `rapidocr` / `true` | S3 routing |
| `CDI_IG_PACKAGE` | `nrces.fhir.r4.ndhm#6.5.0` | Profile/IG package |
| `CDI_TERMINOLOGY_PACKAGE` | `in-snomed-loinc-icd10` | Terminology package |

Pod file: `/workspace/cdi/.env` (copied from `.env.runpod`).

---

## 10. Operations / runbook

### 10.1 First boot on a fresh pod
```bash
# sync the repo to /workspace/cdi  (scp a tarball or git clone), then:
cd /workspace/cdi && cp .env.runpod .env
bash infra/runpod/start_all.sh
# → prints:  LIVE URL: https://<POD_ID>-8888.proxy.runpod.net
```

### 10.2 After every restart (nothing lost)
```bash
bash /workspace/cdi/infra/runpod/start_all.sh
```
`bootstrap_pod.sh` reinstalls apt bits, re-inits the Postgres cluster on the
overlay, **`pg_restore`s `/workspace/backup/cdi.dump`**, runs `alembic upgrade
head`, rebuilds the venv if missing; then the gateway and web app start. Model
weights are already in `/workspace/hf-cache`; scans are already in
`/workspace/minio-data`.

### 10.3 DB snapshots
`snapshot.sh` (`pg_dump -Fc` → `/workspace/backup/cdi.dump`, keeps last 10) is on
`cron` every 15 min; run it manually before a planned pod stop.

### 10.4 Logs & health
```
/workspace/logs/mlserve.log     model load, every /vlm/generate
/workspace/logs/webapp.log      pipeline events (classified, ocr_done, extracted,
                                terminology_bound, projected, vlm_json_partial, job_doc_failed)
/workspace/logs/bootstrap.log   infra bring-up
curl -s localhost:8888/healthz  {db,s3,mlserve}
curl -s localhost:8077/healthz  {model, loaded, device}
psql postgresql://cdi:cdi@127.0.0.1:5432/cdi -f scripts/verify_s3.sql
```
Common issues: SSH sessions drop >~10 s (use `setsid nohup … </dev/null &`);
RunPod nginx returns HTTP 200 with a fake 502 page for unbound ports (health
checks must grep a content marker); `psql`/`pg_restore` need the URL with
`+psycopg` stripped.

---

## 11. Testing

`pytest` (`tests/`): ~32 unit + 2 integration.
- Unit (no infra): page render/deskew/thumbnail, mime sniffing, `extract_json`,
  classification schema + prompt, `StubMLClient`, OCR reading-order, terminology
  helpers (`freq_per_day`, `to_ucum`), `repair_payload` + `validate_schema` for all
  extraction schemas, **S6 rules** (value ranges, °F normalization, medication
  dose/frequency/ceiling, future/implausible dates, evidence-present) and the
  `_calibrate` gate function.
- Integration (`-m integration`, needs Postgres+MinIO, stub VLM): full
  `ingest → ocr → classify` producing `doc_classification` + `ocr_block` rows with
  bboxes and `pipeline_run` stages `ok`.
- `scripts/pipeline_smoke.py` drives S1–S3 (or S1–S9 via the web app) over the
  generated sample docs and prints a summary; `scripts/make_sample_docs.py`
  generates rotated/noisy synthetic prescriptions / lab reports / vitals sheets.

---

## 12. Security & compliance posture (current)

| Area | Now | Gap |
|---|---|---|
| Data residency | all inference + PHI on the pod; no external API calls in the hot path | — |
| Legacy DB | strictly read-only; never touched | — |
| Web UI auth | **none** | add auth before any real data |
| Transport | RunPod proxy provides TLS to the browser; internal is plaintext localhost | mTLS for a multi-node deployment |
| At rest | MinIO + Postgres on the pod; DB dump on `/workspace` | encryption at rest, KMS |
| Audit | `audit_log` on every create/update/read | ship to a WORM store |
| De-identification | none (demo data) | Presidio + clinical NER before any training corpus |
| ABDM | schema for `abdm_care_context/consent/transfer`; **no gateway, no Fidelius** | build the DMZ edge (§14) |
| DPDP Act 2023 | not addressed | consent, purpose limitation, retention, data-principal rights |
| Secret hygiene | note: `RUNPOD_API_KEY` is readable in `/proc/1/environ` on this pod. *(E1-S5)* DB/Redis/MinIO credentials come from a 0700/0600 secrets store (`CDI_SECRETS_DIR`), not `.env`; no password on any command line (pgpass, Redis `requirepass` file); repo secret scan in CI | rotate the RunPod key (owner); don't share pods; the persistent store copy on `/workspace` (MooseFS, 0777) → KMS on the production host (E1-S10) |

---

## 13. What is built vs. not — production gap analysis

| Capability | State | Notes |
|---|---|---|
| S1 ingest + normalize | ✅ built, tested | dedupe, deskew/denoise, page store |
| S2 classify (heuristic first, VLM fallback) | ✅ built, 9/9 on samples | scored keyword heuristic; Qwen2.5-VL-7B only for ambiguous / handwritten pages (§5 S2) *(reconciled E1-S8)* |
| S3 OCR (printed + handwriting) | ✅ built | RapidOCR + VLM; printed strong, handwriting untested at scale |
| S4 extract (schema-locked) | ⚠️ built, **quality-limited** | 7B mis-slots JSON often → repair marks `_partial`; **the S6 gate now holds all `_partial` facts for review** rather than trusting them. Medication dose parsing and vitals extraction are still the weak spots — fixed properly by the fine-tuned DSLM |
| S5 terminology | ⚠️ **seed map only** | ~120 concepts; no Snowstorm, no SapBERT/FAISS, no ICD, no ConceptMap `$translate` |
| **S6 clinical validation + gate** | ✅ **built** | deterministic rules (ranges, units, dose ceilings, dates, evidence, unmapped) + calibration placeholder + routing; `fact_conflict` populated (duplicate + same-day contradiction); 3-valued `evidence_state` recorded |
| S7 longitudinal reconciliation | ⚠️ **foundation added, engine not built** | canonical `cn_*` tables + Customer-360 read-model now exist (§4.5, caveat applies); temporal merge, `supersedes` chains beyond corrections, and a derived summary *graph* are still not built on top of it |
| **S8 human review console** | ✅ **built, since hardened** | `/review`: image + bbox + OCR + fact + code + confidence + findings → accept/correct/reject; reviewer-signed provenance; live re-projection. **Added (§4.5, caveat applies):** dedicated `/reviewer` workbasket→worklist assignment queue, Customer-360 view, `/admin` inspector, canonical-layer promotion. Not yet: auth, SLA-timer/queue-age alerting, throughput tooling |
| S9 FHIR projection (gated) | ✅ built | asserts only governed facts; `ready_to_share` vs `draft`; 0 structural errors on last clean run |
| FHIR/IG validation | ⚠️ **structural lint only** | no HAPI validator, no `nrces.fhir.r4.ndhm` package check, no terminology `$validate-code` |
| Fine-tuned DSLM (`cdi-dslm`, Llama/Qwen-7B + LoRA) | ❌ not built | **the biggest gap** — Qwen2.5-VL currently does extraction; no QLoRA training, no eval harness, no structured decoding (XGrammar) |
| Identity / MPI | ⚠️ minimal | form-driven `get_or_create_patient`; no blocking/scoring, no ABHA verification |
| ABDM HIP/HRP gateway + consent + Fidelius | ❌ not built | schema only |
| Legacy write-back / FHIR façade | ❌ not built | |
| Ingestion at scale | ⚠️ | web app is a single-thread `ThreadPool`; Celery chain exists but unused here; no queue backpressure, no retries tuning |
| Observability | ⚠️ | structlog to files; no Prometheus/Grafana/OTel |
| HA / DR | ❌ | single pod; DB dump is the only backup |
| AuthN/AuthZ, rate limiting, DPDP workflow | ❌ | |

**Honest summary:** a functional, on-prem, open-source **prototype** that takes real
scanned documents to ABDM-shaped FHIR — not a production system.

---

## 14. Roadmap to production (ordered)

The governance chain the reviewer asked for is now in place —
`scan → OCR → extraction → schema validation → clinical validation → confidence
calibration → human review when required → FHIR` — but several links are
placeholder-grade:

| # | Work | Status |
|---|---|---|
| 1 | **Harden + accelerate S4.** (a) **Swap the gateway to a `vllm` `AsyncLLMEngine`** — paged KV cache + continuous batching; deprecate `hf`. (b) **Remove the Phase-2 serialisation lock** in `webapp/jobs.py` so concurrent extracts batch on vLLM. (c) **Grammar-locked decoding** (vLLM `guided_json` / XGrammar) — JSON valid by construction, deletes the repair + retry path; re-tighten the v3 schemas so the grammar constrains. (d) **Fine-tune `cdi-dslm`** (Qwen2.5-VL-7B or a 2–3 B distil + QLoRA, AWQ/INT4) on synthetic + de-identified gold; eval harness (field F1, numeric exactness, hallucination, FHIR validity). Expected: 3-doc job ~90 s → ~30–40 s; 5-doc ~20–35 s. **Floor stays ~8–15 s/doc** for a 7B — sub-5 s needs the small DSLM (d). Grammar fixes JSON validity, **not** clinical fidelity. | **(a)(b)(c) built + tested, measured no gain, reverted to `hf` — see §3.4/§7.4. (d) not started — see `CDI-Adapter-Finetuning-Plan.md`** |
| 2 | **Real terminology service** — Snowstorm-lite (SNOMED CT India) + LOINC/ICD in Postgres; SapBERT/BioLORD + FAISS candidate gen + rule reranker; `$validate-code` / `$translate`; local-code minting. Replaces `seed.py`. | next |
| 3 | **Fit the S6 calibrator** — replace `_calibrate` with isotonic regression per `doc_type × fact_type` on clinician-adjudicated data; derive the gate thresholds empirically per class. | after data |
| 4 | **S7 longitudinal reconciliation** — temporal merge across encounters, `supersedes` chains, medication continuity, derived patient-summary view. | — |
| 5 | **FHIR/IG validation in-loop** — HAPI validator + `nrces.fhir.r4.ndhm` package + terminology `$validate-code`; bundle fails on `error`, quarantines on IG `warning`. | — |
| 6 | **Review console hardening** — auth, reviewer assignment + SLA queue, keyboard-driven throughput, correction diffs as training data, dual-review for high-risk facts. | **assignment queue built** (`/reviewer` workbasket→worklist, §4.5/§7.1, caveat applies) — auth, SLA-timer alerting, keyboard throughput and dual-review still open |
| 7 | **ABDM edge** — DMZ service: HFR/HPR registration, care-context linking, consent-artifact intake, Fidelius (ECDH) encryption, HIU push + status callback; keys never persisted. | — |
| 8 | **Scale** — durable queue (Temporal / tuned Celery) for web-app uploads too, separate ingest/OCR/VLM worker pools, priority + dead-letter queues, idempotency keys, autoscaling. Replaces the in-process `ThreadPool` (robustness / back-pressure — not a single-job latency win). | — |
| 9 | **Ops & governance** — Prometheus/Grafana/Loki + OTel; drift monitors (confidence dist, human-override rate, unmapped-code rate); model registry + canary/rollback; Postgres primary+standby; MinIO 3-node; KMS; web-app AuthN/AuthZ; DPDP data-principal workflow; WORM audit. | — |
| 10 | **Shadow-mode pilot** on real historical documents with clinician adjudication before anything is trusted or shared. | — |

### The gate, as implemented

```
model output ──▶ schema validation ──▶ repair ──▶ (still invalid?) mark _partial
                                                       │
clinical_fact + terminology binding + provenance ──────┤
                                                       ▼
                                          S6 deterministic rules
                                    (range · unit · dose · date · evidence ·
                                     terminology · duplicate · contradiction)
                                                       │
                                            confidence calibration
                                                       │
                            ┌──────────────────────────┴───────────────────────────┐
                            ▼                                                       ▼
          conf ≥ 0.985 & no findings                         blocker | _partial | conf < 0.85 |
          conf ≥ 0.95 & no blockers (+ audit sample)         med-* finding | same-day contradiction
                            │                                                       │
                            ▼                                                       ▼
                     review_state =                                         review_state =
                     auto_accepted                                          in_review  (+ review_task)
                            │                                                       │
                            │                                          S8 console: accept / correct / reject
                            │                                                       │
                            └──────────────┬────────────────────────────────────────┘
                                           ▼
                       S9 FHIR projection asserts ONLY governed facts
                       bundle.status = ready_to_share  (all governed, lint-clean)
                                       | draft         (held facts | structural error)
```

---

## 15. Mandates accepted into the architecture (E1-S8, 2026-09-21)

Four requirements that the brief and the planning backlogs carried, but this document did not, are
now part of the architecture. Each has an ADR with its constraints; none of them relaxes the §0
governing principle.

| # | Mandate | ADR | Key constraint |
|---|---|---|---|
| 15.1 | 30 concurrent patients × 5–10 documents; SLA 5 min from last upload to governed JSON/FHIR **or an explicit hold with an ETA** | [ADR-0002](adr/0002-load-and-sla-requirement.md) | the SLA is met by holds, never by auto-accepting |
| 15.2 | Bounded agentic control plane under a persisted deterministic workflow / state machine | [ADR-0003](adr/0003-agentic-control-plane.md) | agents can never set `auto_accepted`, `clinician_confirmed`, `billing_ready`, `ready_to_share`; S6 stays the only machine authority |
| 15.3 | GPU-class and cost policy: Blackwell 32 GB baseline, ₹75,000/month (Voice + CDI), two-GPU topology, India-resident production host | [ADR-0004](adr/0004-cost-and-gpu-policy.md) | cheapest configuration named only after real quotes and only among those passing the production gate |
| 15.4 | Counter status board + billing handoff contract (`billing_ready` impossible while identity, billing-critical facts or FHIR validation are unresolved) | [ADR-0005](adr/0005-counter-billing-contract.md) | unresolved facts are serialised separately, never labelled confirmed |

**Authoritative document state machine (from ADR-0003):**
`UPLOADED → DURABLY_STORED → TRIAGED → {CPU_PATH | GPU_ESCALATED} → CANDIDATES → EVIDENCE_BOUND →
VALIDATED → {GOVERNED | REVIEW_REQUIRED} → FHIR_VALIDATED → BILLING_READY`.

Not accepted here (still ⛔ HOLD, need an explicit owner decision): WhatsApp intake and HIS
integration (epic E15), review-console draft persistence + optimistic concurrency (E7-S10),
annotate-from-scratch mode (E7-S9), and the v7 vital-sign profiles / ImmunizationRecord half of
E6-S8.

## 16. Scheduled reconciliation pass (E1-S8)

The next time a pod or GPU host is live, run and record:

1. `pg_dump --schema-only` on that host and diff it against `docs/schema/cdi_schema.sql`.
2. The endpoint list from the running web app (`/openapi.json`) against §7.1 + §4.6.
3. The deferred integration tests: `CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000 pytest -m integration`.
4. Update §4.5 / §4.6 and this section with the result and the date.

---

## Appendix A — Repo layout

```
clinical-emr-adapter/
├── docs/
│   ├── DESIGN.md            north-star design + 10-country research
│   ├── ARCHITECTURE.md      this document (as-built)
│   └── adr/0001-adapter-not-replacement.md
├── db/
│   ├── schema.sql  views.sql  apply.sh
│   └── alembic/…/0001_initial_schema.py
├── schemas/                 classification.v1, common.defs, {prescription,lab_report,vitals,
│                            opd_note,discharge_summary,radiology}.v3
├── src/cdi_adapter/
│   ├── config.py db.py storage.py repo.py logging.py
│   ├── ingest/   pages.py service.py watcher.py
│   ├── classify/ prompt.py service.py
│   ├── ocr/      rapid.py vlm_ocr.py service.py
│   ├── extract/  prompt.py service.py
│   ├── terminology/ seed.py service.py
│   ├── validate/ rules.py service.py            (S6 clinical validation + gate)
│   ├── fhir/     resources.py service.py
│   ├── ml/       client.py            (HttpMLClient, StubMLClient, repair_payload, registry)
│   ├── mlserve/  app.py backends.py __main__.py   (model gateway)
│   ├── webapp/   app.py jobs.py page.py review.py review_page.py __main__.py
│   ├── worker.py  api.py
├── infra/
│   ├── runpod/   start_all.sh  bootstrap_pod.sh  start_mlserve.sh  snapshot.sh  README.md
│   └── compose/  docker-compose.yml Dockerfile      (not used: pod and fallback host are native, E1-S3)
├── scripts/      make_sample_docs.py  pipeline_smoke.py  ingest_batch.py  verify_s3.sql
└── tests/        test_pages / test_ingest_unit / test_ml_and_classify_unit /
                  test_ocr_order_unit / test_ingest_integration / test_pipeline_integration
```

## Appendix B — Model & dependency stack (as installed on the pod)

| Component | Version / build |
|---|---|
| GPU | NVIDIA RTX PRO 4500 Blackwell, 32 GB |
| CUDA / driver | 13.0 / 580 |
| Python | 3.12 |
| torch / torchvision | 2.8.0+cu128 / 0.23.0+cu128 (inherited via `venv --system-site-packages`) |
| transformers | 5.16.1 (current `hf` serving backend — one forward pass at a time) |
| vLLM | *not installed* — planned serving backend (§7.4), needs a Blackwell sm_120 / torch 2.8 build |
| VLM | `Qwen/Qwen2.5-VL-7B-Instruct` (Apache-2.0), bf16, ~15 GB VRAM, sdpa attention |
| OCR | `rapidocr-onnxruntime` + `onnxruntime` (CPU) |
| API / server | FastAPI + uvicorn |
| DB / store / broker | PostgreSQL 16, MinIO (RELEASE.2025-09-07), Redis 7 |
| schema validation | `jsonschema` + `referencing` registry |
| DB access | SQLAlchemy 2 (Core `text()`), psycopg 3 |

## Appendix C — Live endpoints (current pod `g1a7lswmb5t79o`)

```
UI            https://g1a7lswmb5t79o-8888.proxy.runpod.net/
review        https://g1a7lswmb5t79o-8888.proxy.runpod.net/review
health        …/healthz
submit        POST …/api/jobs              (multipart: patient_ref? (existing-patient lookup), abha?, files[])
poll          GET  …/api/jobs/{id}         (per-doc facts / accepted / in_review)
bundles       GET  …/api/jobs/{id}/fhir    (re-projected live; ready_to_share vs draft)
download      GET  …/api/jobs/{id}/fhir/download
review queue  GET  …/api/review/tasks
fact detail   GET  …/api/review/facts/{fact_id}
decision      POST …/api/facts/{fact_id}/review   {action, corrections?, reviewer?}
```

## Appendix D — Verified S6/S8 run (2026-09-08)

3 documents, patient "Anjali Das". The 7B extraction returned `_partial` for all
three → **the gate held every fact**: `auto_accepted = 0`, `in_review = 15`
(prescription 9, lab 5, vitals 1), 3 `review_task`s, all bundles `status = draft`,
`asserted_facts = 0`. Accepting one lab fact via
`POST /api/facts/{id}/review {action:"accept","reviewer":"dr.sen"}` →
`GET /api/jobs/{id}/fhir` re-projected with `lab_report … asserted=1 held=4`.
`fact_detail` returned the page-image URL, the `bbox_union`
`[426,1041,2488,1107]`, the OCR blocks (`"HbAlc" 0.937`, `"4.0-5.6" 0.995`) and
the rule findings — everything the review UI overlays on the scan.
