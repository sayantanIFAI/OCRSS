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
- **Update note 2 (2026-09-29) — recognition architecture frozen, NOT built.** A
  design review of the handwritten branch (TrOCR vs Qwen2.5-VL, Bengali + English
  prescriptions, a closed ~100-doctor polyclinic) converged on a target
  recognition architecture. It is recorded in **§15** and is **design only**:
  nothing in §15 exists in code yet, and every section from §0 to §14 still describes
  the as-built system. The main decisions were:
  (1) RapidOCR stays on printed regions; handwritten *line crops* go to **TrOCR/HTR and
  Qwen2.5-VL independently**, never one prompted with the other's answer;
  (2) an **immutable 3-table evidence schema** (`ocr_observation` →
  `interpretation_candidate` → `verified_fact`) enforced at the DB level;
  (3) **recognition is never merged with normalization** — no OCR model emits a
  LOINC/SNOMED code;
  (4) a **strict evidence hierarchy** with a cheapest-first resolution cascade
  replaces "signals vote";
  (5) **per-field calibrated confidence** and precision-at-coverage replace a
  single "99 % OCR accuracy" target;
  (6) personalization comes from a **DoctorNode data object** (vocabulary priors,
  exemplar memory, novelty detection). Per-doctor LoRA is the *last* step, not
  the first.
  Build order is HW-Phase A → E (§15.14). **Phase A is the near-term target. Phases
  B–E are sequenced but not yet scoped.** FHIR/ABDM corrections from the
  `emr.docx` review are in §6.1, and the legacy read-only/FHIR-façade boundary is in
  §6.2. Open discrepancies are listed in §15.15. This revision also **merges a
  parallel draft** (`ARCHITECTURE-emr-OCR-Target-Updated.md`) into one superset.
  The merge brought in the recognition contract, decision-trace rules, the
  locum-doctor case, pgvector exemplars, the release-claim rule and the façade
  boundary.
- **Companion docs:** [`DESIGN.md`](DESIGN.md) is the north-star design + 10-country
  research; this document is **what is actually built and running** (plus §15, the
  frozen-but-unbuilt recognition target). Backlog:
  `CDI-Adapter-Production-Backlog-Enhanced.xlsx` (20 epics / 150 stories, plus a
  *Coverage Matrix* sheet); §15.14 maps each design element to its story.
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
SLA-timer alerting on the queue are still open. **Also not built:** the §15 target
recognition architecture. Today handwriting is still read by Qwen2.5-VL alone, per
*page*, with no line crops, no second engine and no immutable raw-evidence layer.

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
                         │  ThreadPool(1) ── runs the pipeline ──────┼──┐
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
| S3 | OCR / layout | `ocr/service.py`, `ocr/rapid.py`, `ocr/vlm_ocr.py` | **runs before classify.** printed → RapidOCR (boxes+conf+reading order); a page the classifier later calls handwritten gets a second VLM-transcription pass that replaces the blocks. `ocr_document(force_engine="rapidocr"|"vlm")`. *Target (§15, not built): per-region printed/handwritten routing, TrOCR + Qwen on line crops, append-only `ocr_observation`.* | `ocr_block` |
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
| Human review | `review_task` | queue — not yet populated |
| ABDM | `abdm_care_context`, `abdm_consent`, `abdm_transfer` | linking / consent / encrypted push — schema only |
| Audit | `audit_log` | every create/update/read with actor + detail |
| Read model (views) | `v_problem_list`, `v_medication_list`, `v_allergy_list`, `v_results_grid`, `v_encounter_timeline` | flattened lists clinicians read |
| S7 continuity (E5-S1) | `medication_supersession` + `v_medication_history` (0014), `problem_continuity` + `v_problem_history` (0023) | across encounters: the latest order and the latest mention of each problem are what the two lists show; every earlier row is kept, linked and audited |
| S7 summary (E5-S2) | `patient_summary` + `v_patient_summary` (0026) | one row per patient - the current problem list, active medications and allergies, built from the read-model views above and refreshed for that one patient **in the same transaction** as the governed fact that changed it, so it is current inside the job's window (§8.1); a trigger marks `stale_at` so a write path that forgets is visible |

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
- **Known limitations against the §15 target** (as-built behaviour, recorded so
  nobody mistakes it for the design):
  1. **Raw evidence is deleted, not superseded.** The handwritten second pass calls
     `delete_ocr_blocks` and re-inserts, so the RapidOCR reading of that page is
     gone. §15.4 requires append-only observations per engine run.
  2. **Handwriting has no usable pixel evidence.** `vlm_ocr.run_vlm_transcription`
     returns a page-level bbox at a fixed conf of 0.55, so the grounding verifier
     (E4-S4) has nothing narrower than the whole page to re-crop. It needs line-
     or field-level crops (§15.2, E2-S13) first.
  3. **"Handwritten" is a page flag, not a region label.** A printed letterhead
     with a handwritten Rx body is treated as all-VLM. That wastes the cheap
     RapidOCR read of the header, which is also the deterministic doctor-ID signal
     (§15.11).

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

### 6.1 Known conformance gaps from the ABDM v7 review (`emr.docx`, not yet fixed)

An external correctness review of the FHIR/ABDM model (recorded in `emr.docx`)
checked the design against **ABDM FHIR IG v7.0.0** (HL7 FHIR R4.0.1). The review
found these gaps in what is built:

| # | Finding | As built | Target | Story |
|---|---|---|---|---|
| 1 | Current IG is **v7.0.0** | `CDI_IG_PACKAGE = nrces.fhir.r4.ndhm#6.5.0` (§9) | pin v7.0.0 through the E6-S5 staging lane | E6-S9 |
| 2 | v7 replaces generic `ObservationVitalSigns` with dedicated profiles (`ObservationBP`, `…HeartRate`, `…BodyTemp`, `…OxygenSat`, `…RespRate`, `…BMI`, `…BodyHeight`, `…BodyWeight`, `…HeadCircum`) | generic vital-signs profile | profile by LOINC | E6-S8 |
| 3 | 7 clinical artifacts **+ `InvoiceRecord`**; `ImmunizationRecord` exists | 6 artifact types, no Immunization, no Invoice | full artifact map | E6-S8 |
| 4 | Composition sections must reference their entries | PrescriptionRecord references only Medications; Condition/Observation are carried in the bundle but not referenced by any section | every clinical resource referenced from a section | E6-S7 |
| 5 | **Investigation advice → `ServiceRequest`** (section *Investigation Advice*) | no `ServiceRequest`; `fact_type` has no investigation-order kind, so a prescribed "CBC, KFT, S.Creat" has nowhere correct to go | new `investigation_order` fact → `ServiceRequest`, coded from the lab-order ontology (§15.7) | E6-S10 |
| 6 | **Chief complaint ≠ Condition** — "fever for 3 days" is a presenting complaint, "viral fever" is a diagnosis | depends on per-doc handler | complaints never emitted as a confirmed `Condition` | E6-S10 |
| 7 | `Composition.type` codes are profile-/version-specific (binding: FHIRDocumentTypeCodes preferred, SNOMED where possible) | hard-coded SNOMED codes in `ARTIFACT` (§5 S9) | read from the pinned IG package; golden tests per version | E6-S10 |
| 8 | Patient identifiers are **typed**; ABHA ≠ hospital MRN | ABHA + `urn:cdi:legacy-mrn` coexist (§6) | explicit `identifier.type` coding for each | E6-S3 / E6-S10 |
| 9 | Bundle must carry `Practitioner` (HPR) **and `PractitionerRole`** as author | `Practitioner` emitted, not linked to a real doctor | practitioner linked at ingestion (§15.11) | E6-S11 |
| 10 | v7 introduces **INPS** (Indian Patient Summary, 28 profiles, aligned to ISO 27269 IPS) | no patient-summary projection | project the S7 derived summary as INPS | E5-S5 |

The recommended layering is the one this adapter already follows: *internal
clinical model → ABDM FHIR mapping layer → artifact → document Bundle*. ABDM does
not require the internal DB to look like FHIR.

### 6.2 Legacy integration boundary — FHIR façade, no write-back by default (E6-S12)

The adapter principle (§2) stays as it is: **the legacy HMS database is read-only by
default**, and the adapter owns the canonical structured record.

- **How downstream systems read data:** through a stable FHIR/read API façade over
  *governed* canonical data. They never read CDI internal tables.
- **Write-back to the legacy HMS** is not implied by FHIR projection. It would be a
  separately approved, explicitly enabled, idempotent adapter, and before production
  use it must define source-of-truth, conflict handling, field-level provenance,
  rollback and audit.
- **Guard:** a configuration boundary stops write-back from being switched on by
  accident.

This closes the §13 row "Legacy write-back / FHIR façade ❌", which had no owning
story before.

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

### 7.5 Separate CPU and GPU worker pools (backlog E9-S2)

§7.3 measures extraction as **86 % of wall time and the only GPU-bound stage**. One
worker pool doing both halves therefore has to be sized for the GPU, so buying
Phase-1 capacity would mean buying GPU capacity with it. The two halves are split
across queues, and so across pools, each started and sized on its own
(`cdi_adapter.worker`):

| Queue | Work | Pool | Size |
|---|---|---|---|
| `cdi` | folder-watch CPU stages: `ingest_file`, `classify_document`, `ocr_document`, `bind_document` | CPU (`role=cpu`) | `CDI_WORKERS` × `CDI_WORKER_CPU_CONCURRENCY` (0 = `job_max_workers`) |
| `cdi-web` | web-app jobs: **Phase 1** of every document (§7.2) | CPU (`role=cpu`) | as above — the same workers consume both |
| `cdi-gpu` | `extract_document`, and the web job's **Phase 2** | GPU (`role=gpu`) | `CDI_GPU_WORKERS` × `CDI_WORKER_GPU_CONCURRENCY` (0 = `extract_slots()`, E2-S2) |

- **The folder-watch chain is unchanged** apart from routing: `cdi.extract_document`
  goes to `cdi-gpu`, every other stage stays on `cdi` under its old name, and the
  chain itself is the same five tasks.
- **A web job crosses the boundary once.** The CPU-pool runner finishes Phase 1,
  saves it, **gives its lease up and puts the job back on the queue addressed to
  `cdi-gpu`** (`jobstore.dispatch_extract`); the GPU-pool runner claims it and
  resumes at extraction. Nothing is ingested or OCR'd twice: `jobs.phase1_done`
  says that work is already in the database, so no staged upload is fetched for it.
  The handoff claim does not spend one of `job_max_attempts` — it is the same run
  carrying on, not a retry — and a handoff message that is lost leaves an ordinary
  orphan whose Phase 1 is saved, which the next sweep (E10-S9) sends on to `cdi-gpu`.
- **A runner's role follows the queues its worker consumes** (`worker.role_for_queues`),
  so it can never contradict the work it actually takes. A worker consuming
  everything is `role=all` — both phases in one process, nothing handed over, which
  is what `make worker`, the compose stack and every in-process runner (dev, tests,
  `CDI_JOB_EXECUTION=inprocess`) do. `CDI_WORKER_ROLE` overrides the derivation; it
  belongs on one worker's command line, never in a shared `.env`.
- **What this buys:** `CDI_WORKERS` / `CDI_WORKER_CPU_CONCURRENCY` can be raised for
  Phase-1 throughput with the GPU pool, and so GPU spend, untouched — the lever
  E9-S4/S5 autoscale and E13 cost work both need. The medication agent (E12-S4)
  still runs from the CPU pool: it is advisory and outside the job's critical path,
  so it holds no extraction slot.

### 7.6 Broker durability: Redis or RabbitMQ (backlog E9-S8) — **decision open**

§13 / §14 #8 chose **Celery with a Redis broker** and named Celery/RabbitMQ as the
hardened option, which leaves Redis's behaviour under a real crash an open question
rather than a settled one. This section records how that question is answered and
where the answer will be written; it is **not yet answered** — the measurement needs
the pod (see below).

**What makes this a configuration question, not a rewrite.** The broker is one
setting, `CDI_BROKER_URL` (empty = `CDI_REDIS_URL`, which is what every deployment
runs today). `cdi_adapter.worker.broker_conf()` is the only thing that differs
between the two:

| | Redis | RabbitMQ (`amqp://…`) |
|---|---|---|
| Priority lanes (E9-S3) | emulated: one list per step (`cdi-web`, `cdi-web:1`, …), so `priority_steps`/`sep`/`queue_order_strategy` must match on every sender and worker | real priority queues: `x-max-priority` declared on the queue (`task_queue_max_priority`) |
| Publishing | fire-and-forget | `confirm_publish`: a publish returns once the broker has the message |
| Queues, routing, lanes, task code | identical | identical |
| Result backend | Redis | Redis (an AMQP result backend is deprecated in Celery) |

So a swap is `CDI_BROKER_URL=amqp://…` plus starting RabbitMQ — the queues
(`cdi`, `cdi-web`, `cdi-gpu`), the routing, the lanes and every task stay as they are.

**How it is measured.** `scripts/broker_crash_drill.py`, under the E9-S6 load shape
(30 patients × 5–10 documents): it records the broker's own persistence settings (for
Redis the live `CONFIG GET`), uploads the burst, **SIGKILLs the broker at randomised
points strictly inside the burst** (seeded, so a run that found a loss can be replayed)
and restarts it, optionally kills a job worker too, waits for every acknowledged job
to end, then counts from PostgreSQL (`cdi_adapter.durability`):

- **lost** — an acknowledged document that never reached a terminal state. A document
  that was *redelivered and ran again* is not lost; that is the queue working, and it
  is reported separately as `resumed`.
- **duplicated** — a document that reached the results twice (a second `extraction`
  row or a second open review item). This is the figure that matters *after* E9-S3,
  whose idempotency keys already stop a retried upload becoming a second job — and the
  drill proves that separately (`jobs_sharing_a_key`), so a duplicate here is the
  broker's, not the client's.
- **given_up** — a job dead-lettered after `CDI_JOB_MAX_ATTEMPTS`: it ended, but
  without a result, so it is never counted as a success.

**Tolerance, agreed in E9-S8:** **0 lost, 0 duplicated, 0 given up**, over at least
three kills across repeated runs. A Redis run that misses it is the trigger to set
`CDI_BROKER_URL` to RabbitMQ and run the same drill again.

**Status.** The swap mechanism, the drill and the measurement are built and
unit/integration-tested (`tests/test_broker_unit.py`,
`tests/test_durability_integration.py`). The drill itself needs a live broker, the app
tier and the E9-S6 load, so **the numbers and therefore the go/no-go are pending the
pod run**; they belong in this section, with the measured loss/duplication figures,
and nothing here should be read as a decision until they are. **Kafka is out of scope
by design decision** (disproportionate to tens-to-low-hundreds of documents per batch
window and a one-engineer operating constraint), not by oversight.

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

---

## 9. Configuration (`cdi_adapter.config.Settings`, env prefix `CDI_`)

| Key | Default (pod) | Meaning |
|---|---|---|
| `CDI_DATABASE_URL` | `postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi` | adapter DB |
| `CDI_REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker |
| `CDI_S3_ENDPOINT_URL` / `_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` | MinIO on `127.0.0.1:9000`, `cdi-documents` | object store |
| `CDI_INBOX_DIR` / `_PROCESSED_DIR` / `_FAILED_DIR` | `/workspace/data/*` | folder-watch |
| `CDI_PAGE_DPI` | 200 | render resolution |
| `CDI_WEBAPP_PORT` | **8888** | the RunPod-proxied port |
| `CDI_MLSERVE_URL` / `_PORT` | `http://127.0.0.1:8077` / 8077 | model gateway |
| `CDI_MLSERVE_BACKEND` | `hf` | `hf` \| `stub` (`vllm` planned — §7.4) |
| `CDI_VLM_MODEL_ID` / `_FALLBACK_MODEL_ID` | `Qwen/Qwen2.5-VL-7B-Instruct` / `…-3B-…` | VLM + OOM fallback |
| `CDI_VLM_MAX_PIXELS_OCR` | 2 000 000 | processor cap (VRAM bound) |
| `CDI_FAST_CLASSIFY` | `true` | S2 scored heuristic first; VLM only on ambiguous/handwritten |
| `CDI_EXTRACT_RETRIES` | `1` | S4 VLM re-tries on schema failure (schemas relaxed → rare) |
| `CDI_JOB_MAX_WORKERS` | 6 | Phase-1 documents in flight concurrently |
| `CDI_BROKER_URL` | *(empty)* | Celery broker; empty = `CDI_REDIS_URL`, `amqp://…` = RabbitMQ (§7.6) |
| `CDI_OCR_ENGINE` / `CDI_HANDWRITTEN_USES_VLM` | `rapidocr` / `true` | S3 routing |
| `CDI_IG_PACKAGE` | `ndhm.in#6.5.0` | Profile/IG package |
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
| Secret hygiene | note: `RUNPOD_API_KEY` is readable in `/proc/1/environ` on this pod | rotate; don't share pods |

---

## 13. What is built vs. not — production gap analysis

| Capability | State | Notes |
|---|---|---|
| S1 ingest + normalize | ✅ built, tested | dedupe, deskew/denoise, page store |
| S2 classify (real VLM) | ✅ built, 9/9 on samples | Qwen2.5-VL-7B |
| S3 OCR (printed + handwriting) | ✅ built | RapidOCR + VLM; printed strong, handwriting untested at scale |
| S4 extract (schema-locked) | ⚠️ built, **quality-limited** | 7B mis-slots JSON often → repair marks `_partial`; **the S6 gate now holds all `_partial` facts for review** rather than trusting them. Medication dose parsing and vitals extraction are still the weak spots — fixed properly by the fine-tuned DSLM |
| S5 terminology | ⚠️ **seed map only** | ~120 concepts; no Snowstorm, no SapBERT/FAISS, no ICD, no ConceptMap `$translate` |
| **S6 clinical validation + gate** | ✅ **built** | deterministic rules (ranges, units, dose ceilings, dates, evidence, unmapped) + calibration placeholder + routing; `fact_conflict` populated (duplicate + same-day contradiction); 3-valued `evidence_state` recorded |
| S7 longitudinal reconciliation | ⚠️ **foundation added, engine not built** | canonical `cn_*` tables + Customer-360 read-model now exist (§4.5, caveat applies); temporal merge, `supersedes` chains beyond corrections, and a derived summary *graph* are still not built on top of it |
| **S8 human review console** | ✅ **built, since hardened** | `/review`: image + bbox + OCR + fact + code + confidence + findings → accept/correct/reject; reviewer-signed provenance; live re-projection. **Added (§4.5, caveat applies):** dedicated `/reviewer` workbasket→worklist assignment queue, Customer-360 view, `/admin` inspector, canonical-layer promotion. Not yet: auth, SLA-timer/queue-age alerting, throughput tooling |
| S9 FHIR projection (gated) | ✅ built | asserts only governed facts; `ready_to_share` vs `draft`; 0 structural errors on last clean run |
| FHIR/IG validation | ⚠️ **structural lint only** | no HAPI validator, no `ndhm.in` package check, no terminology `$validate-code` |
| Fine-tuned DSLM (`cdi-dslm`, Llama/Qwen-7B + LoRA) | ❌ not built | **the biggest gap** — Qwen2.5-VL currently does extraction; no QLoRA training, no eval harness, no structured decoding (XGrammar) |
| Identity / MPI | ⚠️ minimal | form-driven `get_or_create_patient`; no blocking/scoring, no ABHA verification |
| ABDM HIP/HRP gateway + consent + Fidelius | ❌ not built | schema only |
| Legacy write-back / FHIR façade | ❌ not built | read-only façade by default, write-back opt-in only — §6.2, E6-S12 |
| Ingestion at scale | ⚠️ | web app is a single-thread `ThreadPool`; Celery chain exists but unused here; no queue backpressure, no retries tuning |
| Observability | ⚠️ | structlog to files; no Prometheus/Grafana/OTel |
| HA / DR | ❌ | single pod; DB dump is the only backup |
| AuthN/AuthZ, rate limiting, DPDP workflow | ❌ | |
| Handwriting: second engine (TrOCR/HTR) + disagreement engine | ❌ designed (§15.3) | gated on the E2-S10 Bengali benchmark; its dataset does not exist yet |
| Line/region detection (printed / handwritten / mixed / uncertain) | ❌ designed (§15.2) | today the "handwritten" flag is per page (§5 S3 limitations) |
| Immutable evidence schema (`ocr_observation` / `interpretation_candidate` / `verified_fact`) | ❌ designed (§15.4) | today `ocr_block` is deleted and re-inserted on the VLM pass |
| Evidence hierarchy + resolution cascade | ❌ designed (§15.6) | — |
| Lab-order ontology, drug master, alias engine | ❌ designed (§15.7) | seed map only (S5) |
| Per-field calibrated confidence, precision-at-coverage | ❌ designed (§15.10) | `_calibrate` placeholder (S6) |
| Practitioner link at ingestion / DoctorNode | ❌ designed (§15.11–§15.12) | FHIR `Practitioner` is not linked to a real doctor |

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
| 11 | **HW-Phase A — core immutable recognition pipeline** (§15): immutable evidence schema, lab + drug ontologies, alias cascade, line detection, TrOCR + Qwen disagreement under the evidence-hierarchy precedence rule, pixel grounding on line crops, per-field calibrated review. | **near-term target** — E4-S7, E4-S8, E3-S6/S7/S8, E2-S13, E2-S10→S11, E4-S4, E4-S2/S3 |
| 12 | **HW-Phases B–E** — numeric recognizer + field grammars + negative constraints (B); practitioner-linked DoctorNode, vocabulary priors, bidirectional exemplar memory, novelty detection (C); adjudication learning loops, confusion/digit/layout/sequence profiles (D); per-doctor adapters only if a held-out benchmark proves them (E). | **sequenced, not scoped** — epic E15 |

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

## 15. Target recognition architecture — handwriting, evidence hierarchy, DoctorNode

> **Status: DESIGNED and FROZEN (2026-09-29), NOT BUILT.** This section came out of
> a multi-round design review: a proposed TrOCR + Qwen ensemble, a
> production-pipeline critique, and a closed-polyclinic personalization design. It
> is the *target*, not the as-built system, so nothing here may be cited as
> existing behaviour. HW-Phase A is committed as the near-term target. HW-Phases
> B–E are sequenced but not yet scoped (§15.14).

### 15.1 Why the handwritten branch changes

- **Qwen2.5-VL reads messy handwriting well because it reasons about context, and
  that same reasoning is the risk.** Faced with ambiguous strokes, it can produce a
  fluent, plausible, *wrong* drug name (e.g. resolving to "Montek LC" when the pixels
  don't support it). A confident wrong answer and a confident right answer look the
  same to a threshold gate, so confidence alone cannot catch this.
- **Two independent engines disagreeing is a much stronger ambiguity signal** than
  either engine's own confidence.
- **Qwen's general OCR ability says nothing about Bengali doctors' handwriting.**
  Only a West Bengal benchmark can answer that (E2-S10), and it gates the whole
  ensemble.
- **The product objective is selective, not universal.** The target is not "99 %
  handwriting OCR". It is **precision on auto-accepted critical fields ≥ 99 % at a
  measured coverage**, with everything else going to review, and the job is to raise
  coverage without letting that precision fall (§15.10).
- **The polyclinic is a closed world** (~100 known doctors, a finite local
  vocabulary). The problem therefore changes from "read arbitrary handwriting" to
  *"identify which known clinical concept this known doctor intended, from pixel
  evidence, and abstain when ambiguous"*. That is a much easier ML problem (§15.12).

**Governing recognition contract** (the §15 counterpart of the header's governing
principle):

> *Pixels are evidence. OCR/HTR observes. Terminology interprets. Priors rank.
> Grounding can veto. Calibration decides auto-accept eligibility. Humans resolve
> residual uncertainty.*

### 15.2 Frozen pipeline

```
                            PRESCRIPTION (any channel)
                                     │
                        ┌────────────▼────────────┐
                        │ ORIGINAL IMMUTABLE IMAGE │  (sha256; never altered — S1 already)
                        └────────────┬────────────┘
                                     │
                  [1] IMAGE QUALITY GATE  blur · glare · clipping · resolution · orientation
                                     │        └─ fail → specific rescan request (E2-S12)
                  [2] PREPROCESS  perspective · deskew · crop · CLAHE · light denoise
                                     │        (derived render; original kept separately)
                  [3] DOCUMENT CLASSIFICATION  heuristic → VLM fallback, schema-locked (as built)
                                     │
                  [4] PRACTITIONER LINK  Encounter.practitioner → reg. no. → header/template
                                     │        → UNKNOWN  (never inferred from handwriting)  §15.11
                  [5] REGION + LINE DETECTION  printed | handwritten | mixed | uncertain  (E2-S13)
                                     │
             ┌───────────────────────┴────────────────────────┐
         PRINTED                                        HANDWRITTEN line crops
         RapidOCR (CPU, as built)                ┌──────────────┴──────────────┐
             │                                TrOCR / HTR                 Qwen2.5-VL(-AWQ)
             │                                (literal)                   (contextual)
             │                                   └── run INDEPENDENTLY — no shared candidate ──┘
             └───────────────────────┬────────────────────────┘
                                     ▼
                  [6] RAW EVIDENCE  → ocr_observation  (append-only, DB-immutable)   §15.4
                                     │
                         ┌───────────┴───────────┐
                     free text               numeric field → domain recognizer + grammar (B)
                         └───────────┬───────────┘
                                     ▼
                  [7] CANDIDATE GENERATION  (resolution cascade, cheapest first)     §15.6
                      global vocabulary (ALWAYS active) · doctor vocabulary (PRIOR only)
                      · exemplar memory (VISUAL evidence) → top-K → reranker
                                     │     → interpretation_candidate (never edits raw_text)
                  [8] NEGATIVE CONSTRAINTS  eliminate impossible candidates; never invent (B)
                  [9] PIXEL GROUNDING  enlarged crop from ORIGINAL image; independent check
                 [10] DISAGREEMENT ENGINE  TrOCR↔Qwen · OCR↔terminology · OCR↔grounding · fields
                 [11] CALIBRATED PER-FIELD CONFIDENCE  policy by field × evidence state
                                     │
                        ┌────────────┴────────────┐
                   AUTO-ACCEPT                HUMAN REVIEW  (existing S8 / /reviewer; no parallel path)
                        └────────────┬────────────┘
                                     ▼
                 [12] verified_fact + full provenance  →  FHIR / ABDM / EMR  (S9, as built)
                                     │
                    exemplars · training data · doctor stats   (HW-Phase D loops)
```

Stages 1–3 and 12 → S9 exist, or are already backlogged, in the as-built pipeline.
The new work is stages 4–11.

### 15.3 Independent inference and the disagreement engine (E2-S10 → E2-S11)

- **Independence is a hard requirement.** Qwen must never be prompted with TrOCR's
  output ("OCR thinks this is *Telma 40*, check it"). That is anchoring: the model
  agrees with the supplied candidate even when the pixels are ambiguous. It is the
  same bias E7-S9 measures in human reviewers, one layer earlier. A *separate*
  adjudication pass that sees both candidates plus the crop is allowed, but only as
  cascade level L7 (§15.6), after independent reads.
- **Four disagreement sources** are compared, not two: (1) TrOCR vs Qwen on the same
  crop, (2) selected OCR candidate vs terminology/formulary retrieval, (3) selected
  candidate vs pixel grounding, (4) cross-field relations (e.g. a strength the
  matched drug is not made in). Any disagreement beyond tolerance **forces review
  regardless of VLM confidence**. When all four sources agree, the fact follows the
  existing E4 gate unchanged.
- **It hooks into the existing gate** (S6 → E4-S4 grounding → S8). It is one more
  input to that gate, never a parallel review path.
- **Scope:** handwritten regions only. The RapidOCR printed path and its cost are
  unaffected.
- **Gate:** built only if E2-S10 shows a measurable gain in **medicine-name
  exact-match** accuracy over Qwen alone. CER/WER is reported separately, because a
  name can be close in characters and still clinically wrong. Results are
  stratified by Bengali / English / mixed script.
- **Worked cases (test fixtures):**
  - TrOCR "Telma 4O", Qwen "Telma 40". Lexical, embedding and reranker all favour
    Telma 40 (0.995), and grounding supports both "Telma" and "40". → accept
    *Telma 40*.
  - TrOCR "Telma 40", Qwen "Telmikind 40". Both are valid products and grounding
    cannot separate the names. → **REVIEW**. Never pick Telma because it is
    prescribed more often.

### 15.4 Immutable evidence schema (E4-S7)

Three tables. The immutability is enforced **by the database** (trigger or
permissions), not by coding convention, following the E4-S5 pattern:

```
ocr_observation                       -- RAW. append-only. never UPDATEd, never DELETEd.
  id, document_id, page_id, region_id
  bbox int[], polygon, crop_hash       -- crop taken from the ORIGINAL image
  region_kind  printed|handwritten|mixed|uncertain
  field_domain text|strength|dose|frequency|duration|lab_value|age|date|null
  engine       rapidocr|trocr|htr|qwen2.5-vl|digit|...
  engine_version, prompt_hash
  raw_text                             -- IMMUTABLE
  raw_confidence, token_confidences jsonb
  run_id → pipeline_run, superseded_by (nullable; supersession is a pointer, not a delete)

interpretation_candidate              -- SCORED + SOURCED. many per observation.
  id, observation_id[] → ocr_observation
  concept_id, normalized_text, code_system, code        -- normalization lives ONLY here
  source   literal|verified_alias|generated_alias|doctor_alias|confusion_map|
           doctor_exemplar|global_exemplar|embedding|reranker|qwen_adjudication
  resolved_by_level  L1..L8 (§15.6)
  score, evidence jsonb                 -- the per-signal evidence matrix
  eliminated_by (negative constraint id, nullable)

verified_fact                         -- FINAL. what S6/S9 consume.
  id, observation_ids[], winning_candidate_ids[], concept_id, governed_value
  verification_method  auto_accept|clinician_confirmed|corrected|rejected
  confidence (calibrated, per field), policy_id, reviewer_id, model_stack, verified_at
```

**Traceability chain** (tested end to end): `verified_fact → winning candidate(s) →
raw observation(s) → pixels (crop_hash on the original image) + model versions`. A
**reviewer correction never edits an observation.** It adds a candidate
(`source = reviewer`) and a `verified_fact` that points to it, so the literal
reading and the human decision both survive.

What this changes relative to what is built:

- The S3 handwritten pass **stops deleting** RapidOCR blocks. Each engine run
  appends its own observations.
- Re-extraction may purge *machine-derived* candidates. It may **never** purge
  `verified_fact` rows or reviewer decisions.
- `clinical_fact` / `fact_provenance` (§4.2–§4.3) stay as the projection
  contract. `fact_provenance.ocr_block_ids` maps to observation ids through a
  compatibility view, so the S8 console keeps working during migration.
- The handoff's "do not touch `rapid.py`" still holds for the **engine**. Only
  where its output is **persisted** changes (`ocr_block` → `ocr_observation`). That
  is a deliberate, recorded exception (§15.15).

### 15.5 Recognition ≠ normalization

The recognition layer outputs **transcription only**, e.g. `{"raw":"Sr Cr",
"trocr":"Sr Cr","qwen":"Sr. Cr"}`. It never outputs `SERUM_CREATININE` / LOINC
`2160-0`. That is reasoning, and it happens only in `interpretation_candidate`. This
is an **acceptance criterion** in E2-S11 and E4-S7, and is owned end-to-end by
**E4-S9**, not a guideline:
- recognizer-output schemas contain no code fields;
- API contracts carry *observed raw text* and *normalized concept/code* as separate
  objects;
- terminology and confusion maps cannot change the observation;
- the **review UI shows both** — observed "Sr Cr" beside normalized "Creatinine
  [Mass/volume] in Serum or Plasma, LOINC 2160-0" — each with its provenance.

### 15.6 Evidence hierarchy and resolution cascade (E4-S8)

Signals are **not** equal voters. Without a precedence rule the system becomes
"nine signals vote and nobody can say why a fact was accepted", which contradicts
the provenance principle.

**Precedence (highest authority first):**

| Tier | Evidence | May | May never |
|---|---|---|---|
| 0 | **Pixels** (grounding on the original-image crop) | veto any candidate | be overridden by any lower tier |
| 1 | **Independent recognizer readings** (RapidOCR / TrOCR / Qwen / numeric recognizer) and **visual exemplar similarity** | propose, support, contradict | be merged with normalization (§15.5) |
| 2 | **Lexical mapping** (verified alias > observed doctor alias > generated alias candidate; terminology validity) | map a reading to a concept | turn a literal reading into another string |
| 3 | **Priors** (doctor vocabulary frequency, co-order / sequence, specialty, layout zone) | re-rank candidates already supported by tiers 0–2 | pick between two valid, pixel-indistinguishable candidates, or override pixels ("usually orders HbA1c" never beats pixels showing HBeAg) |
| — | **Negative constraints** (specimen marker, glyph extent, strength↔drug, grammar) | eliminate candidates | add or invent text |

**Cascade (each level runs only if the previous one is ambiguous; the resolving
level is stored as `resolved_by_level`):**

```
L1 exact verified alias ─▶ L2 normalized alias ─▶ L3 fuzzy lexical ─▶ L4 doctor exemplar retrieval
─▶ L5 global exemplar retrieval ─▶ L6 embedding retrieval + reranker ─▶ L7 Qwen contextual
adjudication (sees both candidates + crop, AFTER independent reads) ─▶ L8 human
```

Most easy cases (e.g. OCR "S.Creat" is a verified alias → `SERUM_CREATININE`) stop
at L1 at zero GPU cost.

- **Short-circuiting needs validation.** A cheap level may end the cascade only
  where its result has been *validated for that field and cohort* (e.g. L1 exact
  alias for lab-order names on printed text). Otherwise escalation continues.
- **Majority vote is never enough to auto-accept.** An explicit contradiction from
  pixel grounding or from an independent HTR reading forces review, however many
  priors support the candidate.
- **The machine-readable decision trace** lists each evidence source, its tier, its
  score and reason, and the escalation cause. It is persisted with the fact and
  logged as an agent step (E12-S5). Every accepted fact carries its **evidence matrix**
(one row per signal: TrOCR, Qwen, exemplars, alias, doctor vocabulary, co-order,
grounding, negative constraints). An auditor can then see *why* it was accepted.
When the matrix has any real conflict (e.g. TrOCR "S.Creat", Qwen "S.Ca", ambiguous
exemplars, several alias hits), the fact goes to **review even when history favours
one reading**.

### 15.7 Knowledge layer: lab-order ontology, drug master, alias classes (E3-S6/S7/S8)

- **Global lab / investigation-order ontology (E3-S7).** Closed-world,
  concept-centred. Each concept has: canonical name, LOINC mapping(s),
  individual-vs-panel type, specimen, method constraints, panel components (*likely*
  vs *optional*; composition varies by lab), aliases, Bengali aliases, OCR
  confusions (as candidates), and co-order relations.
  - **Size it from data, don't assume it.** The useful claim is "the top ~N
    canonical orders cover ~95 % of *this polyclinic's* historical order volume",
    with N measured from historical prescriptions. It is not "100 tests = 95 % of
    all tests". At ~100 concepts × 10–30 aliases the lexicon is only a few thousand
    strings, and a given doctor's vocabulary is smaller still.
  - **Panels stay panels.** "KFT" → `raw_text "KFT"`, normalized order = KFT/RFT
    panel. Never expand it into Urea/Creatinine/Na/K…, because labs define panels
    differently. Panel composition is stored as lab-specific metadata. **The
    constituent results come from the diagnostic lab report**, never from
    inference.
  - **Specimen is not assumed.** "Creat" → candidates {serum, blood, urine
    creatinine}. Context ("KFT", an "S." marker) resolves it, or it goes to review.
    LOINC keeps these as separate concepts on purpose.
- **Indian drug master (E3-S6, already backlogged).** brand, generic, normalized
  generic, strength + unit, dosage form, manufacturer, composition, aliases, Bengali
  aliases, common abbreviations, OCR variants, active flag. SNOMED/LOINC/ICD do not
  cover Indian brands, so this is required, not optional.
- **Three alias classes with different evidence weights (E3-S8):**
  - **A. Verified standard** — e.g. S. Creatinine, Sr. Creat, SCr. Clinician
    sign-off required.
  - **B. Generated candidates** — from abbreviation rules (Serum→S/S./Sr/Sr.,
    Creatinine→Creat/Cr). These feed *candidate generation only* and are never
    auto-promoted to A, because unrestricted combinatorial generation creates
    collisions ("CR").
  - **C. Observed doctor aliases** — learned from reviewed prescriptions in HW-Phase
    D. Much stronger evidence than B.
  - Any alias that maps to more than one concept is flagged as a collision.

### 15.8 Numeric fields, grammars, negative constraints (HW-Phase B, E15-S1..S3)

- **Numeric errors are the dangerous ones:** 20↔40, 5↔50, 0.5↔5, 1-0-1↔1-0-0.
  Numeric recognition is split by domain because each domain has its own valid
  grammar: strength `<number><unit>`; dose `d-d-d` (incl. ½); duration `x<n>d | x<n>
  days | x<n>wk | x<n>/52`; frequency from a closed set (OD, BD, TDS, QID, HS, SOS,
  PRN); lab values; age; dates.
- **The grammars reuse E2-S3's dormant grammar-locked decoding** (XGrammar /
  `guided_json`), extended from document schemas to field grammars. They are also
  applied as a validator on TrOCR output. That makes this cheaper than it looks: the
  infrastructure is already built.
- Example: glyph probabilities for the third digit are 1 = 0.73, 7 = 0.21; the grammar
  is `d-d-d`; the doctor's historical glyph "1" matches at 0.96 and "7" at 0.54. →
  `1-0-1`. That is stronger than asking a 7B VLM "what dose was written?".
- **Negative evidence eliminates candidates** that cannot be right. An "S." specimen
  marker contradicts urine creatinine. A three-glyph extent contradicts "HbA1c". A
  strength the product is not made in contradicts that product. It never adds text.

### 15.9 Pixel grounding (E4-S4, sharpened)

The selected candidate's bbox is re-cropped **from the original image** (not the
preprocessed render), enlarged with margin, and independently checked: *"can this
exact value be supported by these pixels?"* Grounding runs per field (name and
strength separately: "Telma" yes, "40" yes). On handwriting it only means something
once E2-S13 supplies line/field-level crops (§5 S3 limitation 2).

### 15.10 Per-field calibrated confidence, policies and the headline metric (E4-S2/S3, E2-S7)

- **Confidence is per field** (medicine, strength, dose, frequency, duration, lab
  test, diagnosis), never one document-level score.
- **Thresholds are keyed by field × evidence state.** Illustrative evidence states:
  engines agree + grounding pass; known doctor + strong exemplar; known doctor, no
  exemplar; unknown doctor; engine disagreement (= always review). Stricter for
  higher clinical consequence, so strength and dose are the strictest. **Every value
  comes from held-out adjudicated data.** No universal 0.99 constant. Novelty
  (§15.12) always lowers eligibility.
- **Headline metric = precision on auto-accepted fields, reported with coverage.**
  Illustrative: 50,000 medication fields → 42,000 auto-accepted (84 % coverage) →
  41,650 correct → 99.17 % precision. The engineering objective is to keep accepted
  precision ≥ 99 % while coverage rises. The KPI is owned by **E4-S10**, and the CI
  regression gate is E2-S7.
  - **Reported separately:** auto-accept precision; auto-accept coverage;
    human-review rate; medicine-name exact match; strength / dose / frequency /
    duration exact match; lab-order exact match; CER/WER for literal OCR.
  - **Sliced by:** field; printed vs handwritten; Bengali / English / mixed; known
    vs unknown doctor; known vs novel concept; **image-quality bucket**.
  - **Release-claim rule:** a release may state "≥ 99 %" only for the *exact*
    held-out field/cohort metric that actually meets it. A clinical owner signs off
    the definitions of the critical-field metrics.
- **Error class → defence**:

| Error source | Defence |
|---|---|
| Bad photograph | quality gate (E2-S12) |
| Wrong doctor | Encounter link + registration no. + template (E6-S11, E15-S4) |
| General handwriting error | TrOCR / HTR |
| Contextual ambiguity | Qwen, *independent* |
| Doctor-specific handwriting | exemplar memory (E15-S7) |
| Repeated doctor OCR errors | confusion **candidate** map (E15-S10) |
| Digits | numeric recognizer + grammar (E15-S1/S2) |
| Known abbreviations | deterministic lexicon (E3-S8) |
| New test / drug | global vocabulary always active; novelty detector (E15-S8) |
| Over-personalization | novelty detector; priors tier 3 only (E4-S8) |
| Wrong normalization | terminology constraints (E3) |
| Plausible hallucination | pixel grounding (E4-S4) |
| Model disagreement | disagreement gate (E2-S11) |
| Residual uncertainty | human review (S8) |

### 15.11 Practitioner link at ingestion (E6-S11) and deterministic doctor ID (E15-S4)

Doctor identity is captured **at ingestion, for every source channel**, as data with
a method and a confidence:

| Channel | Primary signal | Fallback | Notes |
|---|---|---|---|
| **Fresh intake** (appointment / counter) | `Encounter.practitioner` from the booking — deterministic | — | no OCR of the doctor name at all |
| **WhatsApp** submission | the sender number identifies the *patient/attendant*, **not the doctor** | printed registration no. → printed name → letterhead/template fingerprint | never use the sender as the doctor |
| **Batch-digitized historical paper** | printed registration no. (very high) → printed name (high) → known template (high) | chamber (medium), signature (low–medium), handwriting style (low, fallback only, never sole basis) | no Encounter exists |

Stored on the document: `practitioner_id` (nullable), `practitioner_link_method ∈
{encounter, registration_no, printed_name, template, manual, unknown}`,
`link_confidence`. **`unknown` is a valid, normal state.** An unknown or uncertain
doctor runs the global system only. Doctor A's priors must never be applied to
Doctor B's prescription. The same link feeds the FHIR `Practitioner` (HPR) +
`PractitionerRole` + `Composition.author` (§6.1 #9).

- **Locum / covering doctor.** A covering doctor may write on the regular doctor's
  letterhead, or under the regular doctor's booking. If the Encounter, the printed
  header and the handwriting signals **conflict**, the document goes to the global
  path and to review. It never takes a guessed DoctorNode. Both the locum case and
  the wrong-header case are required test fixtures.
- **Practitioner routing is independent of patient MPI.** It must work before and
  after patient identity is resolved (§5 Identity, E6-S6).
- **It needs a practitioner master** that carries registration numbers. That master
  is an external data dependency.

### 15.12 DoctorNode: a data object, not a model (HW-Phase C, E15-S5..S8)

```
DoctorNode #017
├── practitioner_id, specialty, identity features (reg. no., templates)
├── vocabularies   tests · drugs · diagnoses · instructions · dosage patterns   (PRIOR only)
├── aliases        observed doctor aliases (class C)
├── exemplar memory  verified crops: words · tests · medicines · numerics  (VISUAL evidence)
├── glyph / digit profile                                               (Phase D)
├── OCR confusion map   → candidates only, never text.replace()          (Phase D)
├── layout profile · co-order graph · sequence (n-gram/Markov) model      (Phase D)
├── confidence calibration
└── optional handwriting adapter (TrOCR/HTR LoRA)                        (Phase E, only if proven)
```

- **100 DoctorNodes are cheap. 100 models are not.** There is one global
  recognition stack, and each node supplies conditional knowledge. The adapter is
  level 4 of personalization (global model → profile → exemplar memory → adapter),
  not level 2.
- **The global vocabulary stays active**, whatever the doctor's history. Formally
  P(concept | pixels, doctor) ∝ P(pixels | concept, doctor-handwriting) ×
  P(concept | doctor). The first term decides. The second is only a prior.
- **Exemplar retrieval runs in two directions:** doctor-specific (this doctor's
  verified crops) *and* global-concept (every doctor's verified crops of each
  concept). Doctor #17's first-ever "Anti-CCP" can then be matched through Doctors
  #32/#44/#81's examples.
  - **Storage:** embeddings go in **PostgreSQL + pgvector**; no second vector store.
  - **What an exemplar is:** each one points to a `verified_fact` and its immutable
    crop hash, and only clinician-verified crops qualify.
  - **What retrieval may do:** it returns top-k similarities *as evidence*. It never
    writes the clinical value.
  - **When it switches on:** benchmark doctor-only, global-only and combined
    retrieval. The feature stays disabled unless the combined path raises coverage
    at the same target precision.
  - **Why it comes before LoRA:** it helps from the first verified corrections and
    needs no retraining.
- **Novelty detector:** if the best doctor-exemplar similarity falls below a
  threshold (e.g. CBC 0.51, HbA1c 0.48 …), the item is **NOVEL**. Confidence is
  lowered and the search widens to global vocabulary and exemplars. The system must
  never pick the nearest historical item by default.
  - The threshold is calibrated on a held-out *"first-ever-for-this-doctor"*
    drugs/tests slice.
  - Novel status is visible in the decision trace.
  - Personalization must **fail open** to the global vocabulary, never **fail
    closed** to the doctor's history.
  - Doctor priors (E15-S6) cannot be switched on until the novelty detector
    (E15-S8) is live.
- **Cold start** (doctor #101): day 1 uses the global stack. At ~20 reviewed
  prescriptions a vocabulary starts to form, at ~50 aliases/confusions, at ~100
  exemplar retrieval becomes useful. Adapters are *evaluated* after N validated
  samples, never trained automatically at a count.

### 15.13 Learning loops and adapters (HW-Phases D–E, E15-S9..S14)

- **Every adjudicated crop feeds three loops:**
  1. global (better global HTR, which helps everyone);
  2. doctor (that doctor's DoctorNode);
  3. concept (the concept exemplar bank).

  Each crop is stored with doctor_id, raw OCR, canonical concept, confidence,
  decision, timestamp and model version. That one record serves as training data,
  exemplar, eval candidate, alias evidence and confusion evidence. This extends
  E4-S1.
- **Adapter benchmark matrix (E15-S13)**, on held-out per-doctor data:
  - A. global TrOCR
  - B. A + doctor exemplars
  - C. doctor-adapted TrOCR
  - D. global Qwen
  - E. per-doctor Qwen LoRA
  - F. TrOCR doctor adapter + global Qwen + exemplars

  The working hypothesis is that **F, or even B**, beats E on accuracy per unit of
  complexity. Style mostly affects the *recognition* problem, so if any adapter wins
  it is more likely to belong on the HTR than on Qwen. The benchmark decides.
- **An adapter is deployed per doctor only if** it improves held-out accuracy for
  that doctor **without lowering auto-accept precision**. It is then registered
  under E10-S5 with rollback.
  - New and low-volume doctors stay on the global path.
  - Adapters are MB-scale. Maintaining 100 full 7B models is never acceptable.
  - Per-doctor Qwen LoRA (arm E) is tested **only if** HTR adaptation (C / F) leaves
    a measured gap.
- **Late priors** (layout, co-order, sequence, specialty) go through ablation one at
  a time. Any prior with no measurable held-out gain is disabled. A prior may raise
  or lower a score, or force review. It can never create a concept.

### 15.14 Build order → backlog mapping (one engineer)

| HW-Phase | Contents | Stories | State |
|---|---|---|---|
| **A — core immutable pipeline** | immutable 3-table schema; recognition/normalization boundary; evidence hierarchy + cascade; global lab-order ontology; drug master; alias engine (exact→normalized→fuzzy); line/region detection; Bengali benchmark → TrOCR + Qwen disagreement; pixel grounding; per-field calibrated review; selective-prediction KPI; practitioner link at ingestion; AWQ base-VLM validation | **E4-S7, E4-S9, E4-S8, E3-S7, E3-S6, E3-S8, E2-S13, E2-S10 → E2-S11, E4-S4, E4-S2/S3, E4-S10, E6-S11, E2-S14** (+ E2-S12 quality gate, E2-S7 CI gate) | **near-term target, scoped** |
| B — safety | domain numeric recognizer; field grammars; negative constraints | E15-S1, E15-S2, E15-S3 | sequenced, **not scoped** (precision/coverage dashboard was pulled forward into A as E4-S10, because Phase A's exit and the E2-S10 go/no-go can't be measured without it) |
| C — polyclinic advantage | deterministic doctor ID for unlinked documents + template fingerprints; DoctorNode; vocabulary priors; bidirectional exemplar memory; novelty detection | E15-S4 … E15-S8 | sequenced, **not scoped** |
| D — learning | adjudication → three loops; confusion candidate maps; digit/glyph profiles; layout / co-order / sequence priors | E15-S9 … E15-S12 | sequenced, **not scoped** |
| E — adapters, only if proven | benchmark matrix A–F; gated per-doctor adapter deployment | E15-S13, E15-S14 | sequenced, **not scoped** |

**Non-blocking technology-radar arm:** E2-S15 (Could; depends only on E2-S4 + E2-S10) reuses the E2-S10 benchmark harness to compare any credible commercially usable/open-weight challenger OCR/VLM/HTR model available at execution time. It does **not** delay E2-S10/E2-S11, and it cannot change the production hot path without a separate architecture decision plus E10-S5 model-registry/canary governance.

### 15.15 Discrepancies and open decisions (not silently resolved)

1. **GPU / checkpoint size.** The pilot pod runs Qwen2.5-VL-7B in **bf16 (~15–16 GB)
   on a 32 GB RTX PRO 4500** (§1, Appendix B), and this works. `emr.docx` states the
   project "already chose the AWQ build (~6.92 GB)". That holds for the *production /
   shared-GPU target* (E13-S2, E13-S6) and for keeping TrOCR resident alongside Qwen.
   It does not hold for the pilot pod. E2-S14 validates AWQ as non-inferior before any
   switch. Do not size hardware from the 16.6 GB published-checkpoint figure.
2. **Classification order.** The E2-S10/S11 handoff (written from `DESIGN.md`) calls
   classification "VLM-based, upstream of OCR". As built (§3.2, §5 S2) it is a
   **scored heuristic that runs after RapidOCR**, with the VLM only as fallback.
   The pod and this document win. The handoff is stale on this point.
3. **"Do not touch `rapid.py`"** (handoff) vs the §15.4 schema: the engine is left
   alone, only its persistence target moves. Recorded here as a deliberate exception.
4. **E2-S10 dataset does not exist.** 500–1,000 de-identified West Bengal
   prescription lines, sourcing owner TBD. **Do not synthesize.** De-identify via
   E11-S4 first. This blocks the whole handwritten-ensemble decision.
5. **Off-the-shelf TrOCR checkpoints are English** (trained on IAM). E2-S10 must name
   the exact HTR checkpoint used for Bengali lines. If no Bengali-capable one exists,
   report TrOCR as an English-only arm rather than claim a Bengali result.
6. **IG version.** The code pins `nrces.fhir.r4.ndhm#6.5.0`, while the current IG is
   v7.0.0 (§6.1 #1, E6-S9).
7. **Parallel draft reconciled.** A second draft of this design
   (`ARCHITECTURE-emr-OCR-Target-Updated.md` + `…-OCR-Updated.xlsx`, same day) used
   **different story IDs**. For example, its E2-S13 is the numeric recognizer (here
   E15-S1), its E6-S9 is practitioner routing (here E6-S11), its E4-S10 is the
   recognition/normalization boundary (here E4-S9), and its E6-S10 is the FHIR façade
   (here E6-S12). **This document and `D:\CDI-Adapter-Production-Backlog-Enhanced.xlsx` are the
   superset of record** (a `…-Perfected.xlsx` copy in Downloads is identical apart from
   pass-6 fixes). The parallel files should not be used for IDs.
8. **Resolved as an optional, non-blocking story (E2-S15):** the wider challenger-model
   bake-off is now explicitly backlogged under the same E2-S10 West Bengal handwriting
   harness. It benchmarks whatever credible commercially usable/open-weight challenger
   models exist at execution time against the same held-out crops, accuracy metrics,
   latency/VRAM constraints and licence checks. It does **not** gate E2-S10/E2-S11 and
   cannot alter the production architecture without a separate design decision plus
   E10-S5 model-registry/canary governance.

---

## 16. Operational integration features (E16–E20) — DESIGNED, NOT BUILT

> Added 2026-09-29 (backlog pass 7). **Repo of record is now
> `manishtech0607-cmyk/CDI-Adapter`** (main). It is an architecture-driven rebuild and
> does **not** contain the vLLM/XGrammar backend or the Customer-360 view that exist in
> `sayantanIFAI/emr@fdd4aeb`. Its Repo Baseline sheet lists what each file proves.

| Epic | Design rules (the acceptance criteria enforce them) | Builds on (in the repo today) |
|---|---|---|
| **E16 File listener** (most important) | One connector interface selected by config: local / network folder, **OneDrive** (Microsoft Graph delta + webhook, certificate auth, least-privilege scope), **SharePoint** (Sites.Selected). Persisted lifecycle: inbox → processing (lease) → **completed** \| **error** \| quarantine. A file moves to completed only after its pipeline run is durably recorded. Reliability features: upload-complete detection, dedupe, idempotency key per file version, backpressure, graceful drain. An **error-recovery agent** classifies each failure, retries **at most 3 times** (DB-enforced) with a remedy per class, then dead-letters to a human task. The agent can re-queue or quarantine only. It can never govern facts. | `ingest/watcher.py` (local only; moves to *processed* before the pipeline runs), `worker.py` (3 Celery retries per stage) |
| **E17 Persistence + FHIR agent** | Everything extracted goes into normalized 3NF tables (prescription header, medication order, lab/investigation order, diagnosis, complaint, vitals…), each linked to its evidence. The write is one idempotent transaction per document. A transactional **outbox** feeds a **FHIR-builder agent** that reads **governed rows only**, runs the deterministic projector, validates, and stores the bundle as a **blob** (MinIO), with an index row holding key, hash, version and validation status. | `clinical_fact`, `medication_detail`, `cn_*`; `fhir/service.py`, `fhir/canonical.py` (synchronous today) |
| **E18 Doctor master + matching** | `practitioner_master`, starting from a clearly marked sample: name variants, designation, specialty, registration no. Matching order: registration number (exact) → normalized, transliteration-aware name scoring, returning top-k candidates with a calibrated confidence. Below threshold there is no auto-link. The UI shows the **extracted name next to the DB-matched name** with the score and the match reasons. | `mpi/service.py` fuzzy matching is patients-only |
| **E19 Medicine context plausibility** | Maps drugs to therapeutic class and indication. Drug–context plausibility is **tier-3 evidence plus a negative constraint** (§15.6): it can re-rank or eliminate look-alike candidates, e.g. no chest-pain drug for a spine prescription. It **never overrides clear pixels**, and a legitimate co-morbidity drug is **flagged for review, never removed**. It stays enabled only if auto-accept precision is equal or better. | `validate/rules.py` dose ceilings only |
| **E20 Screen dispatch agent** | A versioned field-mapping registry per target screen. An agent sends **governed data only**, API/FHIR first, with UI automation as an opt-in fallback that reads back each field. Idempotent dispatch key, acknowledgement, nightly reconciliation and dead-letter. Disabled per target until approved, under the §6.2 write-back boundary (E6-S12). | none |

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
│   └── compose/  docker-compose.yml Dockerfile      (not used on the no-Docker pod)
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
| *Target, not installed (§15)* | TrOCR / HTR line recognizer (the Bengali-capable checkpoint is chosen by E2-S10); `Qwen2.5-VL-7B-Instruct-AWQ` candidate (E2-S14); vision-embedding index for exemplar memory (HW-Phase C) |
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


## Epic 1 readiness audit (2026-10-03)

The current release assessment is [epic-1-readiness.md](epic-1-readiness.md). The non-RunPod
Compose path now includes webapp, queue worker, stub model gateway, Postgres, MinIO and Redis,
with migrations/bucket initialization gating startup (see [compose.md](compose.md)). Native
RunPod lifecycle scripts remain separate. Bootstrap requires a private `SECRETS_SRC`; a
world-readable MooseFS directory is rejected. Configured stores are authoritative and must
contain all four application credentials. Restore errors stop bootstrap.

Golden full runs process exactly the three PDFs in §8.2 order. The general stub still emits
21 facts while §8.2 records 15 from the real model: this difference is an unresolved acceptance
requirement, not permission to claim E1-S6 complete. Nightly infrastructure/backend errors write
an alert as well as exiting nonzero; successful scheduling/delivery needs deployment evidence.
Load reports count only nonempty document-bundle responses as ready and fail on functional errors.


## Epic 2 readiness review — 2026-10-03

This supplement records code hardening after the historical deployment observations above.
The supplied stories are E2-S1, E2-S2, E2-S3, E2-S6 and E2-S8. Their acceptance status and
external release requirements are recorded in [epic-2-readiness.md](epic-2-readiness.md).
The earlier 72–74 s vLLM result remains historical evidence; this review establishes no new GPU
speedup, no 32 GB capacity measurement and no clinical evaluation of a fine-tuned checkpoint.

- The vLLM engine initializes on its owned event loop, releases failed initialization state,
  cancels generation after `CDI_VLLM_REQUEST_TIMEOUT_S` (default 240 seconds), and shuts down
  with the gateway. Processor loading uses the same model revision as the registered model.
  Initialization/model download time is separate from the generation deadline.
- HF Phase-2 concurrency remains one even with constructor overrides. vLLM concurrency is
  bounded by `CDI_VLLM_MAX_NUM_SEQS`. Batch cardinality failures mark every affected document
  as failed, rather than leaving some documents running indefinitely.
- The existing strict v3 schemas drive XGrammar token masks. Unknown schema features fail
  closed; 0–1 confidence rules cannot rewrite literal data. Constrained responses receive
  `usage.grammar_locked` metadata (a per-item list for batch responses). The HTTP client uses
  that metadata to bypass repair/retry; incomplete or schema-invalid responses fail. HF and
  stub response metadata retain their existing shape and repair behavior.
- `start_mlserve.sh` verifies the requested backend against health output. Changing
  `CDI_MLSERVE_BACKEND` requires restarting the existing gateway process; a stale healthy HF
  process cannot be reported as a successful vLLM switch.
- Quantization refuses overwriting nonempty output or overlapping source/corpus paths.
  Benchmark comparison verifies matching input and execution conditions, rejects invalid raw
  JSON, and limits reported KV slot estimates by the engine configuration. Clinical accuracy,
  measured safe capacity, model registration and workbook changes remain separate release gates.

## Epic 4 validation supplement — 2026-10-03

See [the Epic 4 assessment](epic-4-readiness.md) for the supplied seven stories and release gaps.

Migration 0024 extends the no-evidence invariant to provenance removal/reassignment/truncation,
validates observation/candidate trace references, and constrains concordance records to the
same handwritten crop with an unordered pair/version uniqueness rule. Migration 0025 adds
immutable `calibration_adjudication` snapshots, written with S8 and preserved before legacy
adjudicated projections are purged. Scheduled export includes that retained history.

Calibration now fits correct PAV plateaus, groups the evaluation split by document within each
field, recognizes dose-unit corrections, and retains full score precision. Newly fitted models
use method `isotonic_pav_v2`; old models must be refitted. Derived thresholds need a successful
held-out Wilson-bound verdict. A configured gate holds uncovered or incomplete classes instead
of falling back to global constants. Numeric threshold storage is double precision.

The cascade escalates cross-tier conflicts and every explicit tier 0/1 contradiction. Attached
engines do not constitute measured agreement. The shipped `assumed` policy basis cannot accept
facts with a real backend. The synthetic stub uses an explicit test-fixture basis, preserving
its existing golden regression; that is not measured clinical validation.

The original-image crop requirement remains unmet: ingest stores normalized page images and
S3 hashes crops from those images. Extracted-text-only evidence and aggregate verified-fact
confidence also leave the full per-field raw-pixel contract incomplete. Independent recognizer
integration and clinical tolerance/NFR evidence remain outstanding.
