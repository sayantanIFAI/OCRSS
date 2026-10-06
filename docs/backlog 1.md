# BACKLOG — CDI Adapter: FEATURE STORIES (Production-Grade, Single Engineering Resource)

> **Source of truth:** `CDI-Adapter-Production-Backlog-Enhanced.xlsx` (91 stories), restated as
> **Epic → Phase → Block → Task**, then extended with the blocks marked ➕ (production review
> 2026-09-20, plus the target-architecture additions of the same day). One Excel story = one block.
> **Companion file:** `backlog infra 1.md` (infrastructure, non-functional, deployable production grading). Nothing is duplicated between the two; the split rule is in §0.6.
>
> **Scope guard:** `docs/ARCHITECTURE.md` bounds what may be built. Blocks it does not back are
> marked **⛔ HOLD** and are not built without approval. E1-S8 (infra file) reconciles the document.
>
> **Baseline:** 2026-09-19, code at commit `fdd4aeb`. **No application code has been written.**
> **Revised 2026-09-20 (rev 2):** split into two files; decisions applied — production GPU is the
> **Blackwell 32 GB**; monthly envelope **₹75,000**; a small reviewer pool is **not** a blocker;
> phase conflicts resolved by resequencing (§3.1); new blocks from the target architecture diagram.

---

## B. Build tracking (added 2026-09-21, EMR_PROJECT1 build)

**Status values** (one per block, the `**Status:**` line under each block header):
`Pending` · `In Progress` · `Completed` (implemented **and** tested **and** verified) · `Blocked` (reason stated).
The ✅/🔄/⬜ marks inside block headers are the *source plan's* marks and are kept for traceability;
the `**Status:**` line is authoritative for this build.

**Verification of the four source files (2026-09-21)** — findings that change the plan:
1. Both backlogs assume the as-built code at commit `fdd4aeb`; that code was not provided and the
   project folder was empty. → **Phase 0 (Epic E0) added** below: rebuild the as-built baseline from
   `docs/ARCHITECTURE.md`, block by block, before Phase 1. (Decision: project owner, 2026-09-21.)
2. Blocks marked ✅/🔄 (E2-S1/S2/S3, E7-S8, E6-S1, E7-S2) cite "confirmed in code" that is not present
   → reset to `Pending`.
3. Done-marked blocks depend on not-done ones (E2-S1 ✅ → E1-S3 ⬜; E7-S8 ✅ → E7-S2 🔄).
4. Residual forward dependencies despite §3.1's "0 remaining": E3-S5 and E12-S2 (Phase 1) need the
   E2-S4 gold set (Phase 2); E9-S1 depends on E1-S7; E14-S1 depends on E7-S6 (both lifted from HOLD by E1-S8).
5. Stale HOLD marks: the shared ARCHITECTURE.md **does** contain §4.5 and `/reviewer/c360` (§7.1), so
   the "no §4.5" notes on E5-S4 / E7-S8 are out of date. Re-scored by E1-S8.
6. ARCHITECTURE.md internal inconsistencies (resolved by using the more specific section; logged):
   §3.1 `ThreadPool(1)` vs §7.2 6 workers; §13 "S2 classify (real VLM)" vs §5 heuristic-first;
   §9 / App. B "vLLM planned / not installed" vs §3.4 built + reverted; §4.1 `review_task`
   "not yet populated" vs §5 S6 writes it; `patient_registry` (migration 0003) absent from §4.1.
7. E3-S1 needs licensed SNOMED CT India / LOINC / ICD-10 releases (owner must supply).

**E1-S8 mandate decisions** (owner delegated the choice, 2026-09-21): accept into the architecture
the 30 × 10 load + 5-min SLA, the agentic control plane (bounded by §0.5: agents never set governed
states), the cost/GPU policy (Blackwell 32 GB, ₹75k/month), and the counter/billing contract.
Recorded as ADRs when E1-S8 executes.

**Environment:** Windows 11, Python 3.12 venv (matches the pod's 3.12). **No Docker** (owner
decision 2026-09-21, per ARCHITECTURE §1/§3.3 "runs on a single RunPod pod, no Docker"): Postgres 16,
MinIO and (later) Redis run as **native processes**, as on the pod. For local integration tests,
native Windows binaries sit in `.tools/` (git-ignored, dev/test only); production runs them natively
on the pod via `infra/runpod/start_all.sh` (E0-S16). PostgreSQL 16.14 runs locally via
`scripts/dev/pg_local.ps1 start`. Windows Smart App Control is on (enforce) and blocks unsigned
executables without reputation — it is never changed or bypassed; CLI tools run as `python -m <tool>`. The Compose files remain only as the backlog's own
alt-deploy items (E0-S16 as-built, E1-S3); nothing in the build depends on Docker.

**Combined build order:** Phase 0 (E0, this file) → then both files' phases as in §3, with
E1-S8 first in Phase 1.

**Phase 0 status (2026-09-21):**

| Block | Status | Open item |
|---|---|---|
| E0-S1 skeleton / settings / logging | Completed | — |
| E0-S2 schema + migrations + DAL | Completed | — |
| E0-S3 storage + S1 ingest | Blocked | 4 tests need real MinIO |
| E0-S4 ML client + gateway | Completed | real Qwen run listed under E0-S16 |
| E0-S5 S3 OCR | Blocked | 2 tests need real MinIO |
| E0-S6 S2 classify | Blocked | 2 tests need real MinIO |
| E0-S7 registry + MPI | Completed | — |
| E0-S8 S4 extract | Blocked | 1 test needs real MinIO |
| E0-S9 S5 terminology | Completed | — |
| E0-S10 S6 gate | Completed | — |
| E0-S11 S9 FHIR | Blocked | 1 test needs real MinIO |
| E0-S12 S8 review | Completed | — |
| E0-S13 web app + jobs | Blocked | 1 end-to-end job test + page/scan endpoints need real MinIO |
| E0-S14 canonical + reviewer + admin | Completed | — |
| E0-S15 Celery + watcher + API | Blocked | real Redis round trip (+ MinIO) |
| E0-S16 ops + compose + samples | Blocked | the pod run itself |

All Blocked items share one cause — this Windows machine cannot run MinIO or Redis (no official
binaries; Smart App Control blocks local builds) and has no GPU. They unblock together on the
RunPod pod: `bash infra/runpod/start_all.sh`, then
`CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000 .venv/bin/python -m pytest -m integration`.

---

## 0. How to use these files

### 0.1 Hierarchy

| Level | Meaning | Source in the Excel |
|---|---|---|
| **Epic** | E1 … E15 | `Epic ID` / `Epic` |
| **Phase** | 1 … 9 (the single-engineer sequence) | `Phase` column + *Phase Plan* sheet |
| **Block** | one story, built and tested on its own | `Story ID` / `Title` |
| **Task** | checklist items taken from the story's *Description* and *Acceptance Criteria* | `Description`, `Acceptance Criteria` |

### 0.2 Status legend

| Mark | Meaning |
|---|---|
| ✅ | Done (per the Excel, and the code was confirmed to exist in the repo) |
| 🔄 | In Progress (per the Excel) |
| ⬜ | Backlog |
| ⛔ | **HOLD**: in the Excel but **not in `docs/ARCHITECTURE.md`**; needs your approval before it's built |
| 🔒 | Blocked on an external dependency / sign-off (the Excel's column T) |
| ➕ | **Added beyond the Excel** (2026-09-20 production review, §1.2) — not traceable to an Excel story |

**ARCHITECTURE.md check** (on every block):
**✔** named there · **◐** extends something named there (the specific item isn't) · **✖** not there.

### 0.3 Block workflow (applies to every block)

1. Build **only** that block. Combine or skip blocks only when a dependency forces it, and write
   down the reason.
2. Don't add behaviour that goes beyond the block's acceptance criteria and `docs/ARCHITECTURE.md`.
3. Write or extend tests: unit tests always; integration tests when Postgres, MinIO, Redis or the
   model gateway are involved.
4. Run the block's tests plus the full `pytest` suite. Run `ruff` on the changed files.
5. Meet the block's Definition of Done (§0.4).
6. Update this file: tick the tasks, set the status, and add a dated line to the §9 change log with
   the test result.

### 0.4 Definition of Done templates (as written in the Excel)

| Code | Definition of Done |
|---|---|
| **DoD-CODE** | Merged via PR with passing CI (unit+integration); tests added/updated; ARCHITECTURE.md updated; demoed against the golden-path suite (E1-S6). |
| **DoD-DEPLOY** | Deployed to the pod/cluster; verified via /healthz + the relevant metric (E10-S1/S2); runbook entry added. |
| **DoD-SEC** | Reviewed against a documented negative/misuse test case; no plaintext secret in git history; sign-off recorded. |
| **DoD-EVAL** | Eval harness (E2-S4) run against the gold set; thresholds met and logged; result registered before any production traffic (E10-S5). |
| **DoD-DATA** | Data quality checked (no PHI leakage, class balance reviewed); stored with provenance of how it was produced. |
| **DoD-LEGAL** | Reviewed against the source legal/regulatory requirement; sign-off recorded; linked from ARCHITECTURE.md. |

Some blocks have their own DoD, written out in full in the block.

### 0.5 Non-negotiable rules (Excel *README* + *Prod Blueprint*)

- **No-guess rule.** AI proposes. Evidence proves. Deterministic rules govern. Humans adjudicate
  uncertainty. FHIR represents only the governed result. Model output without enough
  provenance/evidence is held or omitted, and **never promoted because of SLA pressure**.
- **Core production contract.** A fact is governed only after evidence binding + deterministic
  validation. Uncertain facts go to human review. FHIR comes only from governed identity/facts
  and must pass the configured validator before `ready_to_share`.
- **Identity.** Registration/master-patient identity is the source of truth; document demographics
  are evidence only. An identity mismatch halts the affected bundle and routes it to review.
- **Agents.** A persisted deterministic workflow/state machine is the authority. Agents may choose
  tools, re-check evidence, query terminology, recommend corrections or request escalation. They
  **cannot** set `auto_accepted`, `clinician_confirmed`, `billing_ready` or `ready_to_share`.
- **Authoritative state machine.**
  `UPLOADED → DURABLY_STORED → TRIAGED → {CPU_PATH | GPU_ESCALATED} → CANDIDATES → EVIDENCE_BOUND →
  VALIDATED → {GOVERNED | REVIEW_REQUIRED} → FHIR_VALIDATED → BILLING_READY`.
  State changes are service methods / DB transactions with allow-listed previous states and audit records.
- **Human-in-the-loop reality.** Reviewer capacity is sized separately from GPU capacity. **Decision
  2026-09-20: a small reviewer pool is acceptable and is not a go-live blocker.** Facts nobody has
  reviewed stay held, the counter sees them as held with an ETA, and nothing is ever auto-accepted to
  save reviewer time.
- **Production proof.** Planning estimates don't prove production readiness. E14-S5 is the final gate.
- **Cost proof.** Provider rates are inputs. The cheapest configuration can only be named after real
  quotes are entered, and only among configurations that pass the production gate.
- **Two files, one plan.** `backlog 1.md` holds **feature** stories (what the product does). `backlog infra 1.md`
  holds **infrastructure, non-functional and production-readiness** stories (how it runs, scales, stays
  up, is secured, is deployed and is cut over). Both use the same phases, so one engineer works them
  as a single plan; each file also shows the combined calendar.
- **Single-resource scope.** One engineering owner. That doesn't remove clinical, legal, ABDM or
  counter-operations dependencies; they're listed per block (🔒).

### 0.6 Split rule (feature vs infrastructure)

| File | Holds | Test |
|---|---|---|
| `backlog 1.md` | **Feature** stories | Changes what the product does or produces for a user: intake, extraction quality and routing, terminology, governance/calibration, review, FHIR/ABDM output, billing handoff, reconciliation, ABDM exchange, the agentic layer |
| `backlog infra 1.md` | **Infrastructure / non-functional / production grading** | Changes how it runs: performance and serving, scale and queues, availability and DR, security and compliance, observability, CI/CD and deployment, hosting, cost, load/soak/cutover |

Serving-speed work (E2-S1/S2/S3/S8, quantization E2-S6) sits in the infra file because it changes
speed and cost, not what the product produces. E7-S1 (review-console AuthN) and E6-S5 (IG upgrade
runbook) sit there for the same reason. Block IDs are unchanged, so cross-references still resolve.

---

## 1. Summary

| Metric | This file |
|---|---|
| Epics / blocks | 9 / **49** |
| Done / In Progress / Backlog | 1 / 2 / 46 |
| Total effort | **156.5 person-days · 235 points** |
| Must-only effort | **21 blocks · 56.5 person-days** |
| Go-live blockers remaining (Must, not Done) | **21** |
| Blocks on ⛔ HOLD (not in ARCHITECTURE.md) | **5** (11 lifted by E1-S8, 2026-09-21) |
| Calendar envelope, this file alone @0.7 focus | **336 days** |
| **Combined plan (both files, one engineer)** | **103 blocks · 296 person-days · 468 points · 616 calendar days · 53 blockers** |

**Decisions applied in this revision**

- **GPU:** production baseline is the **Blackwell 32 GB** running the current Qwen2.5-VL-7B (bf16). No T4, no quantization needed to start; quantization (E2-S6) and a cheaper GPU class (E13-S2) become optional later savings.
- **Budget:** **₹75,000/month** for Voice + CDI combined (envelope and sensitivity in `backlog infra 1.md` §2.2).
- **Reviewers:** a small reviewer pool is acceptable. The Sizing sheet's reviewer gate is reported, not blocking; unreviewed facts stay held with an ETA.
- **Resequenced** (§3.1): E11-S1, E11-S4 → Phase 1; E10-S7 → Phase 6; E6-S8 → Phase 3; E4-S6 → Phase 9; E13-S5 → Phase 7. No dependency now points at a later phase.
- **Open decisions (🔒):** which store is the system of record for governed facts (E15-S2); WhatsApp Business approval (E15-S1); India-region provider + residency sign-off (E1-S10); Voice-project sign-off on the two-GPU topology (E13-S6).

### 1.1 Inputs the feature blocks must turn from ESTIMATE into MEASURED

| Input | Value | Type | Measured by |
|---|---|---|---|
| GPU escalation fraction (share of documents that fail gate 1 and reach the GPU) | 0.3 | ESTIMATE — **unmeasured**; today every document reaches the GPU | E2-S9 |
| Handwritten / image-only fraction | 0.15 | ESTIMATE | E2-S9 / E14-S5 |
| Facts per document · in-review target · facts/reviewer/min | 8 · 0.1 · 4 | ESTIMATE / TARGET | E14-S1, E4-S2/S3, E7-S3 |
| Drug/dose fields always held for HITL | policy flag | DECISION | E7-S5 |

Reviewer supply is **reported, not gating** (see decisions). The Sizing sheet still computes it, so the
counter can see how many facts will be held and roughly for how long.

### 1.2 Blocks added beyond the Excel that belong to this file

| Block | Phase | Why it isn't already covered |
|---|---|---|
| **E2-S10** Gold-set replenishment ➕ | 9 | E4-S1 exports corrections for the **calibrator**; E2-S4 is a **static** gold set. Nothing turns production failures into new regression fixtures, so edge-case layouts can regress silently (Risk R4). |
| **E7-S10** Review draft + optimistic concurrency ➕ | 3 | E7 covers auth, assignment and keyboard throughput, not a network drop mid-correction — and nothing version-checks a review write, so a stale replay can lost-update a governed clinical value. |
| **E15-S1** WhatsApp intake ➕ | 4 | The target diagram shows the front desk uploading by WhatsApp; the stack has only the web form and folder-watch. |
| **E15-S2** Clinic/HIS integration ➕ | 8 | The diagram shows the HIS API as read source and write-back target; §13 lists write-back as not built and no block owned it. |

**Reframed rather than adopted as proposed:** *offline-first review UI* → **E7-S10 raised to Must and widened** to write safety. The data-loss half is real, but optimistic local state without server-side version checks would silently lose updates on governed facts under E7-S2 claiming and E7-S5 dual review.

---

## 3. Build order

| Phase | Name | Epics | Blocks | Person-days | Calendar days (@0.7 focus) |
|---|---|---|---|---|---|
| 1 | Foundation, CI, Patient Identity, FHIR & Core No-Guess Safety | E3, E4, E6, E12 | 16 | 44 | 91 |
| 2 | Extraction Acceleration + CPU-First Selective-GPU Routing | E2 | 4 | 19 | 42 |
| 3 | Human-in-the-Loop / Counter Billing Contract | E6, E7, E12 | 11 | 31.5 | 63 |
| 4 | Scale, Queueing, Deadline Scheduling, Autoscaling & Production Hosting | E15 | 1 | 4 | 14 |
| 5 | Cost Optimization / Two-GPU Topology | — | 0 | 0 | 0 |
| 6 | Security, AuthN/AuthZ & DPDP Compliance | — | 0 | 0 | 0 |
| 7 | Observability, HA/DR, SLOs & Agent Trace | E12 | 2 | 5 | 14 |
| 8 | ABDM Gateway, Clinic Integration & Longitudinal Reconciliation | E5, E8, E15 | 10 | 40 | 84 |
| 9 | Calibration, Shadow Pilot, 30×10 Soak & Cutover | E2, E4 | 5 | 13 | 28 |
| | **Total** | | **49** | **156.5** | **336** |

> Calendar days use the Excel's own formula: `ROUNDUP((person-days ÷ 0.7) ÷ 5) × 7`, applied per phase to *this file's* blocks. Combined across both files: **616 days**.

**Block sequence inside each phase** (respects dependencies; ✅ done, 🔄 in progress, ➕ added, ⛔ HOLD):

- **P1:** E3-S1 → E3-S2 → E3-S3 → E4-S5 → E4-S4 → E6-S1🔄 → E3-S4 → E3-S5 → E6-S2 → E6-S3 → E6-S4 → E6-S6 → E6-S7 → E12-S1 → E12-S2 → E12-S3
- **P2:** E2-S4 → E2-S7 → E2-S5 → E2-S9
- **P3:** E7-S2🔄 → E7-S8✅ → E7-S3 → E7-S10➕⛔ → E7-S5 → E7-S6 → E7-S7 → E6-S8⛔ → E7-S9⛔ → E12-S4 → E7-S4
- **P4:** E15-S1➕⛔
- **P7:** E12-S5 → E12-S6
- **P8:** E5-S1 → E5-S2 → E5-S3 → E5-S4 → E8-S1 → E8-S2 → E8-S3 → E8-S4 → E8-S5 → E15-S2➕⛔
- **P9:** E2-S10➕ → E4-S6 → E4-S1 → E4-S2 → E4-S3

### 3.1 Dependency conflicts — resolved by resequencing

| Block | Was | Now | Why |
|---|---|---|---|
| E11-S1 web-app AuthN | Phase 6 | Phase 1 | E7-S1 (review AuthN) and E7-S10 depend on it; nothing real ships without auth |
| E11-S4 de-identification | Phase 6 | Phase 1 | E2-S4 gold set (Phase 2) cannot use real documents without it |
| E10-S7 WORM audit export | Phase 7 | Phase 6 | E11-S7 (Phase 6) needs it for the shred-vs-WORM test |
| E4-S6 S6 rule packs | Phase 1 | Phase 9 | its acceptance needs real failure modes from E14-S1 shadow adjudication |
| E6-S8 v7 profiles + Invoice/Immunization | Phase 1 | Phase 3 | the InvoiceRecord half depends on E7-S7 billing handoff (Phase 3) |
| E13-S5 cost dashboard | Phase 5 | Phase 7 | needs the E10-S1 metrics (Phase 7); a conflict the original plan never flagged |

After these moves the script check finds **0 remaining** cases of a block depending on a later phase.
New blocks added in this revision depend only on earlier or same-phase blocks; inside a phase the sequence above is dependency-ordered.

---

# EPIC E0 — As-Built Baseline Rebuild ➕ (Phase 0)

> Added 2026-09-21 (finding B.1). Rebuilds exactly what `docs/ARCHITECTURE.md` + `docs/TECH_STACK.md`
> describe as built — nothing more. Where a later backlog block owns a piece of the as-built system,
> E0 leaves it to that block (vLLM backend → E2-S1/S2/S3; HAPI deployment → E6-S1; Customer 360 →
> E7-S8), so no work is duplicated. DoD for every E0 block: unit tests pass, `ruff` clean, integration
> tests pass where Postgres/MinIO/Redis are involved, behaviour traced to the ARCHITECTURE section cited.

### Phase 0

#### Block E0-S1 — Project skeleton, typed settings, logging, CPU thread caps ➕
**Status:** Completed (2026-09-21)
`Arch ✔ App. A, §9, §7.4 #4 · TECH_STACK §1, §2 (pydantic-settings), §12, §14`
- [x] `pyproject.toml`: setuptools `src/` layout, `requires-python >=3.11`, core deps, extras `dev` / `ocr` / `ml`; `[tool.ruff]` line length 100, target py311; `[tool.pytest.ini_options]` with the `integration` marker
- [x] `config.py`: one `Settings` (env prefix `CDI_`, `.env` file) with every key in ARCHITECTURE §9; code default backend `stub`
- [x] `logging.py`: structlog configuration + `get_logger`
- [x] `_cpu.py`: reads the cgroup CPU quota (v2 `cpu.max`, v1 `cfs_quota/period`), falls back to `os.cpu_count()`; caps `OMP`/`OPENBLAS`/`MKL`/`RAYON`/ORT threads to `(budget-1) ÷ job_max_workers` (min 1) without overriding values already set
- [x] `.env.example`, `.gitignore`, `README.md`
- **Tests:** settings defaults + env override; cgroup parsing (v2, v1, unlimited, fallback); cap arithmetic; logger emits structured events
- **Evidence:** `tests/test_foundation_unit.py` 19/19 pass; full suite 19/19; `ruff check` + `ruff format --check` clean; `mypy src` clean; scripted check: 24/24 ARCHITECTURE §9 keys present in `Settings`. No integration test applies (no Postgres/MinIO/Redis/gateway involved). Config keys owned by later blocks (gate thresholds, OCR min-conf, validator jar, vLLM) are added by those blocks.

#### Block E0-S2 — Database schema, read-model views, Alembic migrations 0001–0004, DAL ➕
**Status:** Completed (2026-09-21) — was Blocked earlier the same day (no runnable native PostgreSQL); unblocked by running PostgreSQL 16.14 natively (see Evidence).
`Arch ✔ §4.1–§4.5, §5 (patient registry) · TECH_STACK §3`
- [x] `db/schema.sql` (20 per-document tables, §4.1–§4.4) + `db/views.sql` (5 read-model views)
- [x] Alembic `0001_initial_schema`, `0002_patient_mpi` (`patient_mpi_seq`, `mpi_id`), `0003_patient_registry` (4 dummy patients), `0004_canonical_emr` (`cn_*`, `ops_*`/`fin_*`, `ai_*`, `rv_*`)
- [x] `db.py` (engine/session), `repo.py` raw-SQL DAL (SQLAlchemy Core `text()`) — shared helpers (audit, pipeline runs, document status); each stage adds its own queries
- **Tests:** unit (DDL parses, migration chain order); integration: `alembic upgrade head` on Postgres 16, every table/view present, downgrade/upgrade round trip
- **Evidence:** `tests/test_schema_unit.py` 12/12 pass — schema tables == the 20 named in ARCHITECTURE §4.1 (parsed from the doc), 5 views == §4.1, `clinical_fact` columns + CHECK enums == §4.2, provenance/FHIR columns == §4.3/§4.4, linear chain 0001→0004, 0004 carries every §4.5 name incl. `cn_lab_result.test_code`, downgrades drop everything upgrades create. `tests/test_schema_integration.py` **7/7 pass on native PostgreSQL 16.14** (server version, all 36 tables + 5 views after `upgrade head`, registry seed + composite key, CHECK constraints reject bad states, views read current facts only, DAL audit/run/status round trip, downgrade base → upgrade head). `python -m alembic upgrade head` on the real `cdi` DB → `0004 (head)`, 37 tables incl. `alembic_version`, 5 views, 4 registry rows. Full suite 38/38; ruff + format + mypy clean.
- **Environment note:** PostgreSQL runs as a native process (`scripts/dev/pg_local.ps1`, dev/test only). 16.15's `initdb.exe` was blocked by Windows Smart App Control (no reputation yet); 16.14 from the same official EDB source has reputation and runs. Smart App Control was **not** changed or bypassed. For the same reason pip's `alembic.exe` launcher is blocked, so tools run as `python -m alembic`, `python -m uvicorn`, etc.
- **Note:** §4.5 gives no column lists; the `cn_*`/`ops_*`/`fin_*`/`ai_*`/`rv_*` columns are the minimum the named workflow needs and are flagged for re-derivation by E1-S8.

#### Block E0-S3 — Object storage + S1 ingest & normalize ➕
**Status:** Completed (2026-09-25). Checked on the Colab L4 fallback host (Ubuntu 24.04) at the owner's direction, against a **real MinIO** built from the pinned official source (`MINIO_TAG`) by `bootstrap_pod.sh`, and PostgreSQL 16.15. With `CDI_TEST_REQUIRE_INFRA=1` (missing infrastructure fails; it does not skip): `tests/test_ingest_integration.py` **4/4 passed** (rows + pages + objects, dedupe, the 4-thread race, the corrupt-PDF error path); the E10-S9 ingest re-render test passed; `tests/test_pages.py` 33/33 on Linux. Live system: 50 documents in bucket `cdi-documents`, every original, page and thumbnail present at the §4.1 key layout (`documents/<sha[:2]>/<sha>/original.<ext>`, `pages/NNNN.png`, `pages/NNNN.thumb.png`), 0 missing, SHA-256 unique. (Was Blocked 2026-09-21: MinIO publishes no binaries, and Smart App Control blocks a local build on the Windows dev box.)
`Arch ✔ §5 S1, §4.1 · TECH_STACK §4, §6`
- [x] `storage.py` (boto3 → MinIO, path-style, s3v4; key layout `documents/<sha[:2]>/<sha>/original.<ext>`, `pages/NNNN.png`, `pages/NNNN.thumb.png`)
- [x] `ingest/pages.py`: PyMuPDF render at `CDI_PAGE_DPI`, Pillow images, multi-frame TIFF; deskew (0.15° < |skew| ≤ 15°) + denoise + CLAHE; thumbnails; mime sniffing; thread caps before OpenCV loads + `cv2.setNumThreads`
- [x] `ingest/service.py` `ingest_bytes`: SHA-256 dedupe (race-safe `ON CONFLICT DO NOTHING` → reuse), store original, `source_document`, `document_page`, `pipeline_run`, audit, failure path
- **Tests:** render/deskew/thumbnail/mime unit tests; integration ingest + dedupe
- **Evidence:** `tests/test_pages.py` 31/31 pass — mime sniffing (5 magic types, extension fallback, content-over-extension, reject unknown), PDF 3 pages → 3 PNGs at 1653×2339 (A4 @200 dpi), single image, 3-frame TIFF, corrupt PDF raises; deskew detects ±3/±5/−8/12° within 0.3° and leaves residual < 0.3°; 0°, 0.1° and 20° are not rotated (the §5 window); thumbnail 192×256; key layout + bad-SHA rejection; client endpoint/path-style/s3v4; exact S3 request shapes via botocore `Stubber`. `tests/test_ingest_integration.py` (4 tests: rows+pages+objects, dedupe, 4-thread race, corrupt-PDF error path) **skipped: no real MinIO**. Full suite 69 passed / 4 skipped; ruff + format + mypy clean (mypy target → 3.12, matching the pod).

#### Block E0-S4 — ML client, JSON schemas, model gateway (stub + hf backends) ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §3.4 · TECH_STACK §8, §9` (vLLM backend left to E2-S1)
- [x] `schemas/`: `classification.v1`, `common.defs`, `prescription/lab_report/vitals/opd_note/discharge_summary/radiology.v3` (relaxed per §5 S4: evidence optional, bare-number/string quantity, `patient` not root-required, free-text `patient.sex`, list items allow extra keys, radiology lists accept bare strings)
- [x] `ml/client.py`: `HttpMLClient`, `StubMLClient`, `extract_json`, `repair_payload`, `referencing.Registry`, `validate_schema`, `vlm_json` with one retry + `_partial` fallback; `bundled_schema` (refs inlined) is sent to the gateway
- [x] `mlserve/app.py` + `backends.py`: `POST /vlm/generate`, `GET /healthz`; `stub` (reads only `<document>…</document>`) and `hf` (lazy load, bf16/sdpa/`max_pixels`, 7B→3B OOM fallback, one forward pass at a time)
- **Tests:** `extract_json`, `repair_payload` + `validate_schema` for every schema, stub client, gateway API via TestClient
- **Evidence:** `tests/test_ml_unit.py` 77/77 — all 8 schemas are valid Draft 2020-12 with matching `$id`; minimal payload valid / missing-confidence + stray-key invalid for each of 7 schemas; §5 S4 relaxations; `extract_json` (fences, prose, nested, first object, 4 no-object cases); repair (rehome `medicines`→`medications`, `name`→`drug_name`, wrap single object, string item → object, confidence normalisation, **never invents a missing required list**); `vlm_json` valid / retry-then-valid / `_partial` instead of raising / best-effort kept / `CDI_EXTRACT_RETRIES` honoured; stub classify + extraction valid for all 6 extraction schemas; HTTP client ↔ gateway contract; gateway 200 + 4×422 + `/healthz`; hf lazy-load-once, 7B→3B on OOM, non-OOM error raised. **Native process check:** `python -m cdi_adapter.mlserve` on :8077 → `/healthz` ok, `HttpMLClient.vlm_json` over HTTP → valid, non-partial. Full suite 146 passed / 4 skipped (MinIO, E0-S3); ruff + format + mypy clean.
- **Pod follow-up:** a real Qwen2.5-VL `hf` inference needs the GPU pod → task added to E0-S16.

#### Block E0-S5 — S3 OCR (RapidOCR + VLM transcription) ➕
**Status:** Completed (2026-09-25). Checked on the Colab L4 fallback host at the owner's direction, against the **real MinIO** (pinned source build) and PostgreSQL 16.15, with `CDI_TEST_REQUIRE_INFRA=1`: `tests/test_ocr_integration.py` **2/2 passed** (ordered blocks with bboxes, idempotent re-run; the VLM pass supersedes the RapidOCR blocks) and `tests/test_repo_integration.py` 5/5; `tests/test_ocr_unit.py` 15/15 on Linux with the real RapidOCR. Live system: 50 documents OCR'd by RapidOCR, 457 blocks, lowest confidence 0.83 (floor 0.30), 0 boxes outside their page, 54/54 OCR runs ok. **Real GPU pass** on a new synthetic page: RapidOCR read all 3 lines (conf 0.97-1.00, boxes at the drawn positions), then a VLM transcription by Qwen2.5-VL-3B through the gateway (16.9 s) returned the same 3 lines exactly and replaced the RapidOCR blocks (engine `vlm`, page bbox, conf 0.55), document status staying `ocr_done`. (Was Blocked 2026-09-21: no real MinIO on the Windows dev box.)
`Arch ✔ §5 S3 · TECH_STACK §7`
- [x] `ocr/rapid.py` (min conf 0.30 via new `CDI_OCR_MIN_CONF`, quad→bbox, polygon), `order_reading`, `ocr/vlm_ocr.py` (page-level bbox, conf 0.55), `ocr/service.py` `ocr_document(force_engine=…)`, idempotent re-run (delete then insert); status → `ocr_done` only from `received`/`pages_rendered` (a post-classify VLM pass must not move the document backwards — §5 S3 vs S2 ordering)
- **Tests:** reading-order unit tests; integration OCR on a generated page
- **Evidence:** `tests/test_ocr_unit.py` 15/15 — quad→bbox, reading order (same-band tolerance, left→right), confidence filter at exactly 0.30, 7 engine-choice cases + rejection, **real RapidOCR 1.4.4 / ONNX Runtime 1.30 reads "Tab Metformin 500 mg" / "HbA1c 7.8 %" from a generated page with bbox at the drawn position and conf ≥ 0.8**, VLM transcription page-bbox/0.55, stub returns nothing for pixels. `tests/test_repo_integration.py` 5/5 on **real PostgreSQL 16.14** — race-safe SHA insert, pages round trip, OCR block bulk insert + ordered read + GIN full-text + delete, 4-int bbox CHECK, latest classification. `tests/test_ocr_integration.py` 2 tests **skipped: no real MinIO**. Suite 166 passed / 6 skipped; ruff + format + mypy clean.

#### Block E0-S6 — S2 classify (scored heuristic + VLM fallback) ➕
**Status:** Blocked (2026-09-21) — code complete; unit-verified; heuristic path verified end to end on real PostgreSQL. The 2 remaining integration tests (full ingest → OCR → classify chain; VLM fallback, which needs the page-1 image) need **real MinIO** (same reason as E0-S3). Unblocks on the pod via E0-S16.
`Arch ✔ §5 S2`
- [x] `_heuristic_classify` (score ≥ 5, lead ≥ 3, ≥ 180 chars, ≥ 75 % alnum; 8 types, 38 weighted word-boundary patterns, weights 3/2/1), VLM fallback with `classification.v1` (11 doc types; `_partial`/unknown → `other`, confidence ≤ 0.5), `doc_classification` row, status `classified`, `_ocr_hint` only when no blocks exist
- **Tests:** the "Cardiac Cath Laboratory" prescription case; ambiguous → `None`; integration ingest → OCR → classify
- **Evidence:** `tests/test_classify_unit.py` 13/13 — 4 clean page types classified; Cardiac Cath Laboratory prescription scores 9 (§5 S2 "9+") with lab_report ≤ 1; short, garbled (< 75 % alnum), weak and genuinely tied (12 vs 12) pages → `None`; word boundaries; dose-strength signal ignores `mg/dL`; prompt lists the 11 types. `tests/test_classify_integration.py`: heuristic path on **real PostgreSQL** → prescription/heuristic/0.95, **no VLM call**, row + status + run ok; 2 tests **skipped: no real MinIO**. The first integration run caught a real gap (the prescription scored 8, not 9+); fixed by adding the dose-strength corroborating signal. Suite 179 passed / 8 skipped; ruff + format + mypy clean.

#### Block E0-S7 — Identity: patient registry + MPI ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §5 Patient registry, Identity/MPI`
- [x] `mpi/registry.py` lookup (CareFlow id / ABHA / mobile digits; ≥ 10 digits so a fragment never matches), `ensure_identity` (idempotent, identity_confidence 1.0), `upsert_registry` (backs `POST /api/registry`); `mpi/service.py` `parse_name/sex/age`, `resolve_identity` (ABHA → fuzzy ≥ 0.90 ± 1 yr → mint `CFP-<YYYY>-<seq>`), aliases, `merge_identity_evidence` (mode, longest on ties; confidence capped 0.99; **never overwrites a registry-backed identity**, backlog §0.5), `names_match` / `dob_match`; shared `dates.parse_date` (partial dates with precision, day-first)
- **Tests:** parser/matcher unit tests; integration resolve/mint
- **Evidence:** `tests/test_identity_unit.py` + `tests/test_identity_integration.py` 63/63 on **real PostgreSQL 16.14** — 9 date formats + 5 rejections; titles/relations/annotations stripped from names; sex; age → birth year incl. months; candidate from payload (malformed ABHA dropped); names_match / dob_match; registry lookup by CareFlow id (case-insensitive), ABHA and mobile in 6 spellings, 5 misses; ensure_identity idempotent; resolve mint → ABHA → fuzzy (OCR slip + 1 yr) → a 3-year gap never merges; minted ids sequential; merge from 3 documents (confidence 0.89) and full agreement capped at 0.99 (§5 example); registry identity not overwritten; registry upsert + partial update + required fields. The tests found 3 real bugs (annotation in names, month-age arithmetic, partial upsert vs NOT NULL), all fixed in code. No object store involved. Suite 242 passed / 8 skipped (MinIO); ruff + format + mypy clean.

#### Block E0-S8 — S4 extract ➕
**Status:** Blocked (2026-09-21) — code complete; all S4 database work (`write_extraction`) verified on real PostgreSQL with a real stub-model payload; the one remaining test, the full `extract_document` chain, needs the page-1 image from **real MinIO** (same reason as E0-S3). Unblocks on the pod via E0-S16.
`Arch ✔ §5 S4`
- [x] prompts + per-doc-type token budgets (2600/2400/2000/…), guidance, `[bN]` block map; tolerant readers `_qty`/`_coded_text`; per-doc-type handlers (8 doc types → 6 schemas); medication detail parsing (`terminology/parse.py`: `freq_per_day`, `duration_days`, `to_ucum`, PRN); provenance + `bbox_union` + literal-substring evidence fallback (never fuzzy); fused confidence `0.5·(extract + mean OCR)`; purge-on-re-extract; encounter (IMP/AMB, partial dates); identity via MPI; mismatch guard (`IdentityMismatch`, facts purged); unknown type → `normalized`
- **Decision logged:** blood pressure is stored as two `vital_sign` facts (systolic / diastolic), matching the two LOINC codes §8.2 binds it to (8480-6 / 8462-4).
- **Tests:** reader/handler/medication-parse unit tests; integration extract with the stub
- **Evidence:** `tests/test_extract_unit.py` 51/51 — `_qty` 9 shapes, `_coded_text` 7, frequency table incl. **§5 examples verbatim** (`1-0-1`→2, `BD`→2, `TWICE IN A YEAR`→0.006, `x 15 days` is a duration), medication parsing (**`METPURE XL 50 MG` → `Metpure XL`/50/mg**, pen IU over fill volume + SC route, schedule separated from name), vitals BP split / dedupe / numeric-only, lab ranges + flags + text values, handler outputs for all 8 doc types, prompt, mismatch rule (6 cases). `tests/test_extract_integration.py` 7/7 on **real PostgreSQL** — facts + med detail + provenance (every fact bound to its OCR block, bbox, text) + fused confidence 0.965 + encounter AMB 2026-08-12/day + minted identity; re-extraction does not double; given patient + alias; mismatch → no facts; unsupported fact → no evidence, conf 0.495; `_partial` recorded; unknown type skipped. 1 test **skipped: no real MinIO**. My own pre-test check caught 3 medication-parsing bugs, all fixed. Suite 300 passed / 9 skipped; ruff + format + mypy clean.

#### Block E0-S9 — S5 terminology (seed map) ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §5 S5`
- [x] `terminology/seed.py` (129 concepts: 37 conditions with ICD-10, 12 symptoms, 7 procedures, 35 labs + 14 vitals LOINC, 20 drugs incl. Indian brand aliases, 6 allergens; `ROUTE_SCT`), `FREQ_PER_DAY` / `UCUM_ALIASES` / `to_ucum` / `freq_per_day` in `terminology/parse.py` (built with E0-S8, which needed them first), `bind_text`/`bind_fact` (exact → alias → token (meds) → fuzzy 0.86), `bind_document` (status `normalized`, run + audit)
- **Tests:** exact/alias/fuzzy binding, medication token match, UCUM + frequency parsing
- **Evidence:** `tests/test_terminology_unit.py` 28/28 — seed sizes match §5 S5; code formats; **all 14 §8.2 worked-example bindings verbatim** (T2DM 44054006, HTN 59621000, Metformin 372567009, Amlodipine 386864001, Atorvastatin 373444002, BP 8480-6/8462-4, weight 29463-7, HbA1c 4548-4, FBS 1558-6, creatinine 2160-0, cholesterol 2093-3, LDL 2089-1, penicillin 373270004); normalisation incl. the Appendix D OCR reading "HbAlc"; 11 binding cases across exact/alias/fuzzy/token (brand Ecosprin → aspirin, Metpure → metoprolol); 6 unknown terms never force-coded (incl. a near-miss disease). `tests/test_terminology_integration.py` 2/2 on **real PostgreSQL** — S4 → S5 on the worked-example prescription: every code matches §8.2, meds get `rxlike_code`, 0 unmapped, status `normalized`; unknown terms stay `local_only` with text kept, conf 0.4. Suite 330 passed / 9 skipped (MinIO); ruff + format + mypy clean.
- **Caveat (recorded in `seed.py`):** codes beyond the §8.2 ones are curated from general knowledge, not yet checked against a licensed SNOMED CT India / LOINC / ICD-10 release. E3-S1 loads the licensed releases and demotes this map to the offline fallback (E3-S5).

#### Block E0-S10 — S6 validation rules + governance gate ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §5 S6, §14 "The gate"`
- [x] `validate/rules.py` (6 rules: value range per LOINC with °F→°C + unit mismatch, unit present, medication dose/frequency/ceiling with `DOSE_MAX_MG_DAY`, dates, evidence, terminology), `_calibrate`, gate routing with thresholds as settings (`CDI_GATE_AUTO_ACCEPT_CONF` 0.985, `CDI_GATE_AUDIT_CONF` 0.95, `CDI_GATE_REVIEW_FLOOR` 0.85, `CDI_GATE_PARTIAL_PENALTY` 0.30, `CDI_AUDIT_SAMPLE_RATE` 0.05), cross-document duplicate/contradiction → `fact_conflict`, one `review_task` per document (+ low-priority audit task); deterministic audit sampling; idempotent re-run; **human decisions are never re-gated**
- **Tests:** every rule, °F→°C, `_calibrate`, gate table rows, integration validate
- **Evidence:** `tests/test_validate_rules_unit.py` 49/49 — 10 range cases, °F→°C (with and without unit), unit-present, 13 medication cases (ceiling at 1.5×, g→mg, PRN needs no schedule, unbound drug has no ceiling, Prolia 0.006/day plausible), 4 date cases, evidence, terminology, 6 calibration rows, **all 9 gate-table rows of §14**, audit sampling deterministic ≈ 5 %. `tests/test_validate_integration.py` 6/6 on **real PostgreSQL**, full S4 → S5 → S6: clean extraction → 8/8 auto-accepted, status `validated`; **`_partial` extraction → 0 auto-accepted, 8 in review, 1 task (the Appendix D behaviour)**; dose ceiling / unmapped / implausible BP held with task kind `dose_check`; missing evidence → blocker; cross-document same-day contradiction holds both facts, same value → duplicate info; a rejected fact stays rejected across re-runs. Suite 386 passed / 9 skipped (MinIO); ruff + format + mypy clean.
- **Decision logged:** cross-document contradiction pulls the *other* fact back to `in_review` only if it is still in a machine state; a clinician-confirmed fact is not overridden (the new fact is held instead).

#### Block E0-S11 — S9 FHIR projection (gated) + Python ABDM conformance check ➕
**Status:** Blocked (2026-09-21) — code complete; the gated projection is verified end to end on real PostgreSQL (S4 → S5 → S6 → S9); the one remaining test, inlining the scan bytes into `DocumentReference`, reads the original from **real MinIO** (same reason as E0-S3). Unblocks on the pod via E0-S16.
`Arch ✔ §5 S9, §6 · TECH_STACK §10` (HAPI deployment left to E6-S1)
- [x] `fhir/resources.py` builders (Patient with CareFlow/ABHA/legacy-MRN identifiers, Organization, Practitioner, Encounter, Condition + ICD-10, exam/lab/vital Observations, MedicationRequest with timing/route/duration, Procedure, AllergyIntolerance, DiagnosticReport, DocumentReference, Provenance, Composition with the §5 S9 sections, Bundle) + `ARTIFACT` map, `fhir/service.py` `project_document` / `project_patient` + `_lint` (stable UUIDv5 FHIR ids; persist replaces the document's previous projection; `bundle_hash`), `fhir/validate_abdm.py` (HAPI auto-switch when `java` + `CDI_FHIR_VALIDATOR_JAR` exist, else Python conformance checks)
- **Decisions logged:** (1) §5 S9 says inline scans ≤ 12 MB, §7.2 says > 700 KB are URL-referenced → one setting `CDI_FHIR_INLINE_MAX_BYTES` (default 700 000); (2) an uncoded Condition is a lint **warning** (`code.text` alone is valid FHIR), so a clinician-accepted local term does not block sharing forever; (3) advice/referral facts go into the "Advisory notes" section narrative (§5 S9 maps no resource for them).
- **Tests:** per-resource builders, `_lint` (dangling ref, uncoded Condition), governed-only projection, `ready_to_share` vs `draft`
- **Evidence:** `tests/test_fhir_unit.py` 26/26 — artifact map (10 doc types incl. default), Patient identifiers, Condition SNOMED + ICD-10, lab Observation quantity/range/interpretation, vital-signs profile, MedicationRequest timing (2/day, weekly, 0.006/day) + SC route + supply, allergy/procedure/exam, bundle shape + lint clean, lint catches 4 defect kinds, Python conformance clean + 6 negative cases. `tests/test_fhir_integration.py` 6/6 on **real PostgreSQL** — fully governed prescription → `PrescriptionRecord` `ready_to_share` (3 MedicationRequest, 2 Condition, 3 Observation, actors, DocumentReference, Provenance) passing the conformance check, persisted with a 64-char hash; **a held medication is reported, never asserted** (`draft`, Composition `preliminary`); accepting it → `ready_to_share`; re-projection keeps ids and one bundle row; non-persisting projection writes nothing; lab report → `DiagnosticReportRecord` with a DiagnosticReport grouping 3 results. 1 test **skipped: no real MinIO**. Suite 419 passed / 10 skipped; ruff + format + mypy clean.

#### Block E0-S12 — S8 review console (`/review`) ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §5 S8, §7.1`
- [x] `webapp/review.py` (`/api/review/tasks`, `/api/review/facts/{id}`, `POST /api/facts/{id}/review`, `apply_fact_correction` with a field whitelist and a `supersedes` chain; reviewer-signed provenance; audit; task closure; 409 on a second decision), `review_page.py` (image + bbox overlay + OCR span + fact + code + confidence + findings + accept/correct/reject; vanilla JS, no CDN), `theme.py` (CareFlow white + blue, shared header), `jsonutil.plain`
- **Tests:** accept / correct / reject state + provenance + task closure
- **Evidence:** `tests/test_review_integration.py` 10/10 on **real PostgreSQL** via FastAPI TestClient — task list shows held facts (`dose_check`); fact detail has provenance, bbox, OCR blocks, page-image URL, `med-dose-ceiling` finding, numbers as JSON numbers; accept → `clinician_confirmed` + reviewer provenance `{"reviewer": "human"}` + audit, second decision → 409; correct → new current fact superseding the old one (evidence + signature copied), reject → `entered-in-error` and the task closes; **live re-projection then asserts only the corrected medication and is `ready_to_share`**; 4 bad requests → 422 and nothing changes; unknown fact → 404; page renders with no external scripts. The test run found a real API defect (Postgres `numeric` serialised as JSON *strings*), fixed with `jsonutil.plain` (+ unit test). Suite 430 passed / 10 skipped; ruff + format + mypy clean (E501 exempted only for HTML/CSS/JS template modules).

#### Block E0-S13 — Web app, job model, upload page, registry endpoints, `/healthz` ➕
**Status:** Completed (2026-09-25). Checked on the Colab L4 fallback host at the owner's direction, against the **real MinIO**, PostgreSQL 16.15, Redis and the GPU gateway, with `CDI_TEST_REQUIRE_INFRA=1`. `tests/test_webapp_integration.py` **12/12 passed**, including the end-to-end job, now also reading the page image and the original scan back from MinIO, with an intake account refused the original (403). The job, auth and app-tier unit tests passed 82/82 on Linux. **Found and fixed:** the `/api/documents/{id}/original` endpoint ticked below did not exist in the code (lost in the Phase 0 rebuild). It is now added (clinical roles only, like the page images; the FHIR DocumentReference keeps the `s3://` location). **Live run through the real site** (the E10-S9 balancer, both instances, the queue workers, Qwen2.5-VL-3B on the GPU) with a throwaway admin account, deactivated afterwards: `/healthz` ok; `/login` and `/` served; registry lookup found Arjun Mehta; upload 202; the job reached `review` in 30 s with every step done (ingest, OCR, classify = prescription, extract, terminology, validate); the page image (PNG) and the original (byte-identical PDF) served from MinIO; FHIR download (1 artifact, `ready_to_share`). Observed, not a web-app fault: the 3B model's extraction failed the prescription schema (no `medications`), so it was stored `_partial` with **0 facts released** (the no-guess rule held). That is extraction quality for the real 7B + grammar-locked decoding (E2-S1/E2-S3), as the E10-S5 drill also found. (Was Blocked 2026-09-21: no real MinIO on the Windows dev box.)
`Arch ✔ §7, §7.1, §7.2, §8.1`
- [x] `webapp/app.py` (all §7.1 endpoints + `/api/documents/{id}/original` for the FHIR DocumentReference URL; 202 job create, ≤ 10 files, registry `patient_ref` → `ensure_identity` + mismatch guard; a mismatch never surfaces identity), `jobs.py` (Phase 1 parallel, Phase 2 serial, `state=review`/`mismatch`, first document fixes the patient, `merge_identity_evidence`, one bad document never sinks the job; pluggable `Stages`), `/facts`, `/generate` (keep/drop/corrections via the S8 decision logic, then live re-projection), `page.py` (header "Intelligent OCR driven Agentic EMR generation", dropzone, optional ABHA, registry lookup, stage grid, patient banner, mismatch card, "sent to the reviewer", FHIR download, "Complete patient details"), `theme.py`, registry lookup/persist, FHIR download, `/healthz` {db, s3, mlserve}, `python -m cdi_adapter.webapp`
- **Tests:** API tests with the stub; integration end-to-end job
- **Evidence:** `tests/test_jobs_unit.py` 6/6 (Phase 2 never runs two documents at once; handwritten → second VLM-OCR pass; a failing document is isolated; mismatch stops the job and skips the rest; unknown type skipped; JSON shape). `tests/test_webapp_integration.py` 11/11 on **real PostgreSQL** (upload page without name/sex fields and no CDN; `/healthz` per-dependency, 503 when storage is down **in < 10 s**; registry lookup ×2 + miss; registry persist → found by mobile, unknown id 404, bad mobile 422; document evidence + bundle + patient FHIR + 404s; upload limits 0/11 → 422; unknown `patient_ref` → 404; unknown job → 404); 1 end-to-end job test **skipped: no real MinIO**. **Native run:** `python -m cdi_adapter.webapp` → `/` 200, `/review` 200, registry lookup returns Ramesh Kumar + ABHA from the `cdi` DB, `/healthz` 503 (no MinIO). Suite 447 passed / 11 skipped; ruff + format + mypy clean.
- **Defect found and fixed:** with object storage down, `/healthz` hung for minutes (boto3 default 60 s connect timeout × retries); the store now uses bounded timeouts and a dedicated 2 s probe client.
- **Test-infrastructure note:** on this Windows box `DROP DATABASE` takes ~1 min (each Postgres backend is a new, security-scanned process); the test fixture now resets the `cdi_test` schema per session instead of dropping the database. Full suite 70 s.

#### Block E0-S14 — Canonical EMR promotion, `/reviewer` workbasket/worklist, `/admin` ➕
**Status:** Completed (2026-09-21)
`Arch ✔ §4.5, §7.1` (Customer 360 left to E7-S8; SLA/visibility left to E7-S2)
- [x] `canon/repo.py` (rv_item per validated document, idempotent; elements resolve to the **current** fact through the `supersedes` chain; actions), `canon/promote.py` (`claim`, `reject_item`, `promote_review_item`: owner-only, **refused while any fact is held**, governed facts → `cn_condition`/`cn_medication`/`cn_lab_result`(`test_code`)/`cn_vital_sign`/`cn_allergy`, `ai_event → ai_suggestion → ai_clinical_verification` chain), `fhir/canonical.py` `build_bundle` (validated with `validate_abdm`, stored in `cn_fhir_bundle`), `webapp/reviewer.py` + `reviewer_page.py`, `webapp/admin.py` + `admin_page.py` (allow-listed tables, patient graph); the job model now puts each validated document into the workbasket
- **Tests:** claim → approve → promote → bundle validates; `cn_lab_result.test_code` regression
- **Evidence:** `tests/test_reviewer_integration.py` 7/7 (count corrected 2026-09-21; earlier recorded as 12) on **real PostgreSQL** via the real app — workbasket shows held count; claim → worklist; second claim 409; another reviewer's approve 403; **approve with a held fact 409**; correcting the held fact updates the element to the new current version; approve → promoted {2 conditions, 2 meds, 2 vitals}, PrescriptionRecord `ready_to_share`, conformance valid, actions create/claim/approve/promote, 6 ai_suggestions with decisions 5 accepted + 1 corrected, bundle endpoint; re-approve 409; **lab item → `cn_lab_result.test_code` read back by the admin patient graph (§4.5 bug-fix regression)**; reject item promotes nothing; unknown item 404; admin inspector refuses `clinical_fact`, `patient_registry` and an injection-style name; pages render without CDN. `tests/test_jobs_unit.py` +1 (validated documents are enqueued, skipped ones are not). Suite 455 passed / 11 skipped, **3 consecutive green runs**; ruff + format + mypy clean.
- **Flaky test found and fixed (test-side):** two tests assumed exactly one review task per document, but §14 audit sampling adds a low-priority `audit_sample` task for ~5 % of random fact ids; the tests now select the held-fact task.

#### Block E0-S15 — Folder-watch path: Celery worker, watcher, ingest API ➕
**Status:** Blocked (2026-09-21) — code complete and unit-verified; the real broker round trip needs a **Redis 7** server (Redis publishes no Windows build; same no-substitutes rule as MinIO), and the real stages need MinIO. Unblocks on the pod via E0-S16.
`Arch ✔ §3.1 (optional path) · TECH_STACK §5`
- [x] `worker.py` (Celery on Redis, queue `cdi`, `acks_late`, tasks `cdi.ingest_file` / `classify_document` / `ocr_document` / `extract_document` / `bind_document` **+ `validate_document`** with `max_retries=3` and doubling backoff; `submit_file`, `submit_document`), `ingest/watcher.py` + `cdi-ingest-watch` console script (size-stable wait, atomic move inbox → processed, rejects and broker outages → failed, never overwrites, processes files present at start-up), `api.py` `POST /ingest` (202; 422 empty; 415 unsupported)
- **Decision logged:** §5 names five Celery tasks; `validate_document` was added so folder-watch facts pass the same S6 gate as web uploads (without it they would stay held forever).
- **Tests:** task chain with eager Celery; watcher moves files to processed/failed
- **Evidence:** `tests/test_folder_watch_unit.py` 15/15 — 6 task names, queue, `acks_late`, Redis broker, retry caps; `submit_file` runs ingest → classify → ocr → extract → terminology → validate in order (Celery eager mode); `submit_document` skips ingest; retry *policy* (backoff 10/20/40 s, cap 3), success never retries, a vanished file is not retried; watcher on the real filesystem (move + queue, reject → failed, broker down → failed and never lost, no overwrite on name collision, waits for a still-growing file) and **a real watchdog `Observer` picking up a dropped file**; API 422/415. Suite 474 passed / 11 skipped; ruff + format + mypy clean.
- **Security defect found and fixed:** `ingest_bytes` trusted the client-declared MIME type, so an executable declared as `application/pdf` or `application/octet-stream` skipped sniffing and was stored. The stored type now comes from the bytes only; unsupported content is refused before storage or the database is touched (3 regression tests). This also closes the same hole in the web app's `POST /api/jobs`.

#### Block E0-S16 — Ops scripts, compose alt-deploy, Makefile, sample-doc tooling ➕
**Status:** Blocked (2026-09-21) — every artifact is written and checked locally; the pod tasks below (run `start_all.sh`, build + start MinIO, run the deferred MinIO/Redis/GPU tests) need the **RunPod GPU pod**, which is not available to this build. Unblocks when the owner provides pod access (or runs the listed commands and returns the output).
`Arch ✔ §3.3, §10, App. A · TECH_STACK §13, §14`
- [x] `infra/runpod/*.sh` (`bootstrap_pod.sh`: apt, PGDATA on the overlay, role/db, `pg_restore`, venv `--system-site-packages`, `alembic upgrade head`, Redis AOF, MinIO **built from source** at a pinned tag with a SHA-256-checked Go toolchain, bucket, 15-min cron; `start_all.sh` with the §10.4 content-marker guard and LIVE URL; `start_mlserve.sh`; `snapshot.sh` verified-before-promote, keep 10; `README.md`), `.env.runpod`, `infra/compose/` (Dockerfile + compose as described — no webapp/mlserve service; that is E1-S3; MinIO images on **quay.io**, tags verified to exist), `Makefile`, `scripts/make_sample_docs.py` (the §8.2 set, rotated + noisy), `pipeline_smoke.py`, `ingest_batch.py`, `verify_s3.sql`
- [ ] ➕ (2026-09-21) `bootstrap_pod.sh` builds MinIO from official source at a pinned release tag (no binaries are published any more) and starts it natively on :9000/:9001
- [ ] ➕ (2026-09-21) On the pod, run every MinIO-dependent integration test deferred by earlier E0 blocks (`CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000`) and lift those blocks from Blocked
- [ ] ➕ (2026-09-21) On the pod, start `mlserve` with `CDI_MLSERVE_BACKEND=hf` and run one real Qwen2.5-VL-7B `/vlm/generate` (lazy load, schema-valid JSON back)
- [ ] ➕ (2026-09-21) On the pod, run a Celery worker on native Redis and push one file through `cdi-ingest-watch` → all six tasks → `validated` (E0-S15's real broker round trip)
- **Tests:** `bash -n` on scripts; compose config validates; sample-doc generator unit test
- **Evidence (local):** `tests/test_ops_unit.py` 15/15 — `bash -n` on all 4 pod scripts (Git Bash), strict mode, no Docker on the pod; bootstrap matches the §3.3 persistence model (overlay PGDATA, `pg_restore`, Redis AOF, 15-min cron, MinIO from source with checksum, inherited torch); `start_all.sh` content-marker guard; snapshot keep-10 + verify; compose has exactly the 8 TECH_STACK §13 services, pinned images, health checks, localhost-only ports; Dockerfile base; Makefile targets; every `.env.runpod` key is a real setting; 3 scripts' `--help`; **the rotated, speckled sample scans go deskew → real RapidOCR → heuristic classifier → correct type for all three**. `tests/test_ops_integration.py` 1/1 — `verify_s3.sql` runs with `ON_ERROR_STOP` on real PostgreSQL. Suite 491 passed / 11 skipped; ruff + format (incl. `scripts/`) + mypy clean.
- **Findings:** (1) the MinIO image tags I first wrote from memory did **not** exist on Docker Hub (the repository returns 404); MinIO's images live on quay.io, where both pinned tags were confirmed. (2) The noisy-scan test showed RapidOCR running headings together ("VITALSIGNSCHART") and reading "SpO2" as "Sp02"; the S2 anchors now tolerate both (the score / lead / legibility gates are unchanged; regression tests added).

---

# EPIC E2 — S4 Extraction Quality & Serving Acceleration

### Phase 2

#### Block E2-S4 — Build the gold evaluation set ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 5 d · 8 pts · Deps: E11-S4 (resolved by resequencing, §3.1) · Arch ✔ §14 #1d · DoD-DATA`
≥ 300 documents across all 6 doc_types (prescription/lab/vitals/opd/discharge/radiology), field-level ground truth, de-identified.
- [ ] Collect ≥ 300 documents, ≥ 40 per doc_type
- [ ] A hand-verified structured-field answer key stored alongside each source scan
- [ ] No real-patient PHI without E11-S4 de-identification applied first
- [ ] Stratify the set so handwritten (≥ 20%) and poor-scan (≥ 10%) documents are included, closing the ARCHITECTURE §13 note "handwriting untested at scale"
- **Tests:** manifest check (counts per doc_type); PHI-leak check; class balance reviewed
- 🔒 Clinical annotator / de-identification approval

#### Block E2-S7 — Automated eval harness as a CI gate ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E2-S4 · Arch ✔ §13, §14 #1d · DoD-CODE`
- [ ] Compute field-F1, numeric exactness, hallucination rate and FHIR validity per doc_type against E2-S4
- [ ] Thresholds documented in the harness config
- [ ] Runs in CI on any prompt/model/schema change; a regression below threshold fails CI
- [ ] Dashboard of metric history over time
- **Tests:** a seeded regression makes CI fail

#### Block E2-S5 — Fine-tune cdi-dslm (QLoRA) ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 8 d · 13 pts · Deps: E2-S4 · Arch ✔ §13, §14 #1d · DoD-EVAL`
Fine-tune Qwen2.5-VL-7B or a 2–3B distilled base on the E2-S4 gold set.
- [ ] QLoRA fine-tune on the E2-S4 training split
- [ ] Beats the untuned Qwen2.5-VL-7B baseline on field-F1, numeric exactness and hallucination rate (E2-S7) on the held-out split
- **Tests:** E2-S7 harness, baseline vs fine-tuned
- 🔒 GPU capacity; gold data (E2-S4)

#### Block E2-S9 — CPU-first / native-text routing with selective GPU escalation ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 4 d · 8 pts · Deps: E2-S4, E4-S5 · Arch ◐ §7.4 ("not calling the VLM for documents a layout model + deterministic parser can handle"); native-text routing isn't named · DoD: eval harness records route, latency, accuracy and provenance; thresholds in config; production defaults not enabled until the gold-set gate passes`
- [ ] Extract native PDF text first; use deterministic/template parsers and CPU OCR where sufficient
- [ ] Escalate to VLM/DSLM only for image-only scans, handwriting, unsupported layouts, low OCR confidence, extraction disagreement, or failed deterministic checks
- [ ] Persist a `route_reason` for every document
- [ ] Measure the GPU-escalation fraction by doc_type; write it into *Sizing & Capacity*
- [ ] Non-inferiority gate vs the all-GPU reference (field exactness, numeric exactness, hallucination rate, held-fact rate); a failing class is forced back to GPU
- **Tests:** gold-set run with the routed vs all-GPU comparison
- 🔒 Clinical gold-set adjudication for the non-inferiority gate

### Phase 9

#### Block E2-S10 — Production-failure sampling → gold-set replenishment ➕ ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E2-S4, E2-S7, E11-S4, E4-S1 · Arch ◐ §14 #6 ("correction diffs as training data" — names training, not eval) · DoD-DATA`
Closes the loop from production failures back into the regression suite. Distinct from E4-S1,
which feeds the **calibrator**; this feeds the **eval set**.
- [ ] Configurable sampling of held / corrected / rejected / low-confidence facts from production
- [ ] Sampled documents pass E11-S4 de-identification before leaving the production boundary
- [ ] A clinician verifies each candidate label before it enters the gold set — a reviewer correction is a candidate, **not** ground truth (E7-S5 exists precisely because single-reviewer decisions aren't trusted)
- [ ] Gold set versioned with a **frozen core split** (so thresholds stay comparable across releases) and a **growth split** (new edge cases), reported separately by E2-S7
- [ ] Layout / doc_type / handwriting coverage tracked so replenishment doesn't skew the balance (Risk R4)
- **Tests:** a replenishment cycle on fixtures; a PR that regresses only the frozen core still fails CI; the de-identification gate provably blocks un-sanitized samples
- 🔒 Clinician verification capacity

---

# EPIC E3 — Real Terminology Service

### Phase 1

#### Block E3-S1 — Snowstorm-lite (SNOMED CT India) + LOINC/ICD-10 in Postgres ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: — · Arch ✔ §5 S5, §13, §14 #2 · DoD-CODE`
- [ ] Load SNOMED CT India edition + LOINC + ICD-10 into Postgres (replacing the ~120-concept `seed.py` map as the primary source)
- [ ] Lookup by code returns the canonical display for ≥ 95% of `seed.py`'s own concept list
- **Tests:** the ≥ 95% regression check against `seed.py`

#### Block E3-S2 — SapBERT/BioLORD embedding candidate generation (FAISS) ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E3-S1 · Arch ✔ §14 #2 · DoD-EVAL`
- [ ] Embed `local_text`; FAISS top-k candidates per target system
- [ ] On held-out clinician-phrased strings not in the seed map, the correct SNOMED/LOINC concept is in the top 5 ≥ 85% of the time
- **Tests:** top-5 recall evaluation

#### Block E3-S3 — Rule-based reranker + local-code minting fallback ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E3-S2 · Arch ✔ §5 S5, §14 #2 · DoD-CODE`
- [ ] Combine embedding similarity with the existing exact/alias/fuzzy matchers
- [ ] Mint a local code for novel concepts; every fact ends in `code_status ∈ {bound, local_only}`, never silently unmapped
- [ ] Track the local-code mint rate as a metric (feeds E10-S4)
- **Tests:** unit tests for reranking; an unmapped term → `local_only` + a minted code

#### Block E3-S4 — FHIR `$validate-code` / `$translate` operations ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E3-S1, E6-S1 · Arch ✔ §6, §13, §14 #2/#5 · DoD-CODE`
- [ ] Every coded resource (Condition/Observation/MedicationRequest/etc.) passes `$validate-code` before bundle assembly
- [ ] An invalid code blocks `ready_to_share`
- **Tests:** an invalid-code fixture → bundle stays `draft`

#### Block E3-S5 — Migrate `bind_fact`/`bind_document` to the new service (`seed.py` as offline fallback) ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E3-S3, E3-S4 · Arch ✔ §5 S5 · DoD-CODE`
- [ ] Cut `terminology/service.py` over to E3-S1–S3
- [ ] With the service stopped, documents still process (facts land `local_only` via `seed.py`)
- [ ] With the service up, the bound-code rate on E2-S4 beats the seed-map baseline
- **Tests:** outage test (service down) + bound-rate comparison

---

# EPIC E4 — S6 Calibration & No-Guess Governance

### Phase 1

#### Block E4-S5 — Hard DB constraint: no evidence ⇒ can't be auto_accepted ⬜
**Status:** Pending
`Must · BLOCKER · 1 d · 2 pts · Deps: — · Arch ◐ §5 S6 check_evidence (DB-level enforcement isn't named) · DoD-CODE`
- [ ] A CHECK constraint or trigger (new Alembic migration) makes `check_evidence` impossible to bypass
- [ ] Setting `review_state='auto_accepted'` on a `clinical_fact` with zero `fact_provenance` rows fails at the DB level
- **Tests:** a direct-SQL negative test that bypasses the application layer

#### Block E4-S4 — Grounding/faithfulness verifier before auto-accept eligibility
**Status:** Pending
`Must · BLOCKER · 4 d · 8 pts · Deps: — · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 rule 4 (E1-S8 re-score; was ✖ (cites the user mandate "no guess answer will be tolerated")) · DoD-EVAL · built together with E12-S3`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] For every fact proposed for auto-accept, re-crop the `fact_provenance` evidence bbox and check the value is visually present
- [ ] A crop that doesn't support the value forces `in_review` regardless of confidence
- [ ] The crop check runs on **CPU by default** (RapidOCR on the cropped bbox); a GPU call is allowed only for pages already escalated, so the verifier adds no GPU-seconds per fact
- **Tests:** ≥ 95% catch rate on deliberately injected mis-slotted values

### Phase 9

#### Block E4-S6 — Extend S6 rule packs for edge cases ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E14-S1 shadow data (resolved: block moved to Phase 9) · Arch ◐ §5 S6 rule table · DoD-CODE`
- [ ] ≥ 5 new deterministic doc-type-specific rules (e.g. discharge summary: admission < discharge; radiology laterality sanity)
- [ ] Each traceable to a real observed failure mode from E14 shadow-mode adjudication
- **Tests:** a unit test per rule

#### Block E4-S1 — Pipeline for clinician-adjudicated training data ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E14-S1 · Arch ✔ §4.2, §4.3, §14 #3/#6 · DoD-DATA`
- [ ] Scheduled job exports every {proposed value, corrected value, doc_type, fact_type, confidence} triple from `clinical_fact` + `fact_provenance`
- [ ] Writes into a versioned training table
- **Tests:** a corrected fact appears exactly once in the export; PHI check
- 🔒 Real clinician-adjudicated corrections (needs shadow-mode data)

#### Block E4-S2 — Fit isotonic-regression calibrator per doc_type × fact_type ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E4-S1 · Arch ✔ §5 S6, §14 #3 · DoD-EVAL`
- [ ] Replace the placeholder `_calibrate` (fixed penalty) with a fitted isotonic model
- [ ] Reliability diagrams per doc_type × fact_type within an agreed tolerance band
- **Tests:** calibration evaluation on the held-out adjudicated set
- 🔒 Real clinician-adjudicated corrections

#### Block E4-S3 — Derive gate thresholds empirically per class ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E4-S2 · Arch ✔ §5 S6, §14 #3 · DoD-EVAL`
- [ ] Replace the global 0.985 / 0.95 / 0.85 constants with per-class thresholds checked in as config
- [ ] Document the target false-auto-accept rate as a hard NFR (e.g. ≤ 0.1%)
- [ ] The measured false-auto-accept rate on a held-out adjudicated sample meets the NFR
- **Tests:** threshold config loads; NFR measurement report
- 🔒 Clinical safety owner approves the false-auto-accept NFR

---

# EPIC E5 — S7 Longitudinal Reconciliation

### Phase 8

#### Block E5-S1 — Temporal supersedes chain across encounters ⬜
**Status:** Pending
`Could · OPTIMIZE · 3 d · 5 pts · Deps: — · Arch ✔ §4.1 fact_conflict, §13, §14 #4 · DoD-CODE`
- [ ] Cross-encounter medication continuity and problem-list merge rules (the reconciliation engine; the `cn_*`/`rv_*` foundation already exists)
- [ ] A medication reordered at a later encounter supersedes the earlier order in the active-medication view, keeping the historical row
- **Tests:** a two-encounter reorder fixture

#### Block E5-S2 — Derived patient-summary materialized view ⬜
**Status:** Pending
`Could · OPTIMIZE · 3 d · 5 pts · Deps: E5-S1 · Arch ✔ §4.1 read-model views, §14 #4 · DoD-CODE`
- [ ] Refresh problem list / active meds / allergies (building on `v_problem_list`, `v_medication_list`, `v_allergy_list`) on every governed fact change
- [ ] Updates within the same job's live-reprojection window (§8.1); no manual refresh
- **Tests:** a governed fact change → the view reflects it within the job

#### Block E5-S3 — Conflict-resolution UI for multi-fact merges ⬜
**Status:** Pending
`Could · OPTIMIZE · 3 d · 5 pts · Deps: E5-S1 · Arch ◐ §5 S8 (a merge UI isn't named) · DoD-CODE`
- [ ] Reviewer sees both conflicting facts side by side with their evidence
- [ ] Choices: keep-A / keep-B / merge → a single auditable decision
- **Tests:** each choice writes one audit record

#### Block E5-S4 — Feed the ops_\*/fin_\* canonical tables and cn_clinical_note 🔒
**Status:** Pending
`Should · HARDEN · 5 d · 3 pts · Deps: E7-S2, E9 · Arch ◐ §4.5 + §4.6 (ops_patient_visit, fin_billing_item, cn_clinical_note exist in the live schema); the feeding rules are not specified (E1-S8 re-score; was ✖ (cites §4.5 + emr.docx; the repo's ARCHITECTURE.md has no §4.5)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ◐. External 🔒 dependencies, if any, are unchanged.
- [ ] Wire the promotion flow (workbasket → worklist → promote) to write `ops_*`/`fin_*` rows for ops/finance-relevant facts (e.g. E7-S7 billing-critical facts)
- [ ] Document `cn_clinical_note`'s unfed state and the cross-project voice dependency in ARCHITECTURE.md
- **Out of scope (per Excel):** `cn_clinical_note` voice-pipeline integration
- **Tests:** a promoted item with ops/fin facts writes the matching rows
- 🔒 Decision on whether/how the voice-AI project feeds `cn_clinical_note`

---

# EPIC E6 — FHIR/ABDM IG Validation & Correctness

### Phase 1

#### Block E6-S1 — HL7 validator + nrces.fhir.r4.ndhm IG package sidecar 🔄
**Status:** Blocked (2026-09-22). Built and verified here with the real validator (JRE + `validator_cli.jar` 6.10.4 + the pinned IG, all offline). What remains is the pod run of `bootstrap_pod.sh` step 5b (install + seed + digest proof on the pod), which needs the **RunPod pod**. Built as a dependency of E6-S5 (owner: "based on the architecture"). **Finding:** `nrces.fhir.r4.ndhm` is not a FHIR registry id; the NRCeS ABDM IG is **`ndhm.in`** (6.5.0, canonical `https://nrces.in/ndhm/fhir/r4`). The old `_run_hapi` also dropped the version. Both fixed. Runbook: `docs/ig-upgrade.md`.
`Must · BLOCKER · 2 d · 3 pts · Deps: — · Arch ✔ §6, §13, §14 #5 · DoD-DEPLOY`
The code already auto-switches to HAPI when `java` + `validator_cli.jar` are present (confirmed in `fhir/validate_abdm.py`). Remaining work is **deployment**, not new integration.
- [ ] Deploy a JRE + `validator_cli.jar` + the pinned `nrces.fhir.r4.ndhm` IG package on the pod/cluster. **Built, not run on the pod:**
  - `bootstrap_pod.sh` step 5b refuses a `CDI_IG_PACKAGE` that differs from the pin, fetches and sha256-checks the jar, then runs `scripts/fhir_ig.py seed`.
  - The seed installs the IG tarball (sha256-pinned) and exactly the 10 packages in `infra/fhir/packages.lock` from the registry, then proves every digest.
  - The JRE is `openjdk-21-jre-headless=21.0.12+8-1~24.04` in the E1-S4 apt pins. New settings: `CDI_FHIR_JAVA`, `CDI_FHIR_PACKAGE_HOME`.
- [x] `java -jar validator_cli.jar <bundle> -ig nrces.fhir.r4.ndhm` returns a real OperationOutcome for a test bundle. The pinned `ndhm.in#6.5.0` was verified here: the official IG example gives 0 errors. `hapi_command` runs offline (`-tx n/a -no-http-access` plus a closed-port proxy, §12) and batches files, because JVM start-up dominates.
- [x] Verify the auto-switch from the Python fallback to HAPI. `validate()` gives `engine: hapi` and 21 IG errors on the WellnessRecord golden. Without Java it gives `engine: python` and `valid: true` on the same bundle: the fallback is blind to the IG, which is E6-S2's job to close.
- **Tests:** a known-bad bundle → errors reported by HAPI. The real validator reports **120 errors across the 5 E6-S4 goldens**, so our S9 output is not yet ABDM-conformant (see E6-S4). `tests/test_fhir_ig_unit.py`.

#### Block E6-S2 — Wire S9 to the real validator (replace `_lint`) ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E6-S1 · Arch ✔ §6, §14 #5 · DoD-CODE`
- [ ] Bundle status gates on zero IG errors, not only `_lint`
- [ ] A real IG-profile violation (e.g. a missing must-support element) forces `draft` even when `_lint` passes
- [ ] `ready_to_share` only when IG-clean **and** fully governed
- **Tests:** an IG-violation fixture → `draft`; a clean governed fixture → `ready_to_share`

#### Block E6-S3 — ABHA identifier correctness test suite ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E6-S1 · Arch ✔ §6 · DoD-CODE`
- [ ] Every Patient carries a valid ABHA-format identifier (system `https://healthid.ndhm.gov.in`), checked against ABDM sandbox fixtures
- [ ] Negative tests: a missing/malformed ABHA fails validation in CI
- [ ] Legacy MRN (`urn:cdi:legacy-mrn`) always present as a separate identifier, never substituting for ABHA
- **Tests:** positive + negative identifier fixtures

#### Block E6-S4 — Golden-bundle regression tests per artifact type ⬜
**Status:** Blocked (2026-09-22). The suite is built and runs on the real validator. The goldens do **not pass the ABDM IG yet**: 120 errors, recorded in `tests/golden/fhir/validation-baseline.json`. The fixes belong to other blocks:
- `Patient.identifier.type`: E6-S6 🔒
- Composition section slices and small gaps: E6-S7 / E6-S2
- HFR/HPR ids: E8-S1 🔒

The CI run also needs a git host (E1-S1). Built as a dependency of E6-S5.
`Must · BLOCKER · 2 d · 3 pts · Deps: E6-S1, E1-S1 · Arch ◐ §5 S9 ARTIFACT map · DoD-CODE`
- [x] 5 golden bundles: PrescriptionRecord, DiagnosticReportRecord, OPConsultRecord, DischargeSummaryRecord, WellnessRecord.
  - `tests/golden/fhir/*.json`, built by `scripts/fhir_golden.py` from fixed synthetic inputs through the real S9 assembly.
  - `fhir.service.build_document_bundle` is split into DB reads plus a pure `assemble_document_bundle`, with no behaviour change.
- [ ] Each passes the real IG validator in CI. **Not yet: 120 known IG errors.**
  - `fhir_golden.py --validate` is a ratchet: it fails on any error beyond the baseline and reports fixed ones.
  - Verified here offline from the lock-seeded cache: "no new errors", exit 0.
  - The CI step is in `.github/workflows/ci.yml` (integration job); it runs once a git host exists (E1-S1).
- [x] Any `fhir/resources.py` change that breaks one fails the build. `fhir_golden.py --check` runs in the unit suite (`test_goldens_are_committed_and_match_the_builders`); a monkeypatched builder change makes all 5 differ.
- **Tests:** the golden-bundle suite. `tests/test_fhir_ig_unit.py`, plus `CDI_TEST_FHIR_VALIDATOR=1` for the real-validator test.

#### Block E6-S6 — Patient identity & demographics source of truth before FHIR ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E6-S1 · Arch ◐ §5 patient registry + mismatch guard; §6 Patient · DoD: identity matching rules, normalization tests, conflict fixtures and FHIR Patient regression tests pass in CI; all reviewer overrides audited`
- [ ] Bind a `patient_id` for each upload job before clinical extraction
- [ ] Exact/normalized matches proceed; configurable mismatches create `identity_mismatch` with evidence → human review
- [ ] Negative test: document text can't overwrite registered identity without an audited reviewer action
- [ ] The FHIR Patient references the governed identity record only
- **Tests:** normalization, conflict and override-audit tests
- 🔒 Registration/master-patient source + operations owner

#### Block E6-S7 — Flesh out Composition section maps against the live v7 profile ⬜
**Status:** Pending
`Must · BLOCKER · 4 d · 3 pts · Deps: E6-S4 · Arch ◐ §6 Composition/Bundle rules (the specific defect comes from emr.docx) · DoD-CODE`
- [ ] Audit section maps for all 6 artifact types (PrescriptionRecord emits only Medications today)
- [ ] Every projecting fact_type (condition, symptom, finding, lab_result, vital_sign, procedure, allergy) has a Composition section referencing it, for every artifact that can contain it
- **Tests:** E6-S4 golden bundles assert zero unreferenced clinical resources

### Phase 3

#### Block E6-S8 — Adopt ABDM v7 vital-sign profiles + add missing artifact types ⛔
**Status:** Pending
`Should · HARDEN · 6 d · 5 pts · Deps: E6-S1, E7-S7 (resolved by resequencing, §3.1) · Arch ✖ (from an emr.docx review; ARCHITECTURE.md names neither v7 vital profiles nor Immunization/Invoice records) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** stays ⛔ HOLD (Arch ✖) — InvoiceRecord half is now backed by ADR-0005, but the ABDM v7 vital-sign profiles and ImmunizationRecord half are not in ARCHITECTURE.md (§15 'Not accepted here'). Needs an explicit owner decision.
- [ ] `vital_sign` facts project to the dedicated v7 profile by LOINC (ObservationHeartRate, BP, BodyTemp, OxygenSat, RespRate, BMI, BodyHeight, BodyWeight, HeadCircum) instead of the generic ObservationVitalSigns
- [ ] Add an ImmunizationRecord artifact type + mapping
- [ ] Add an InvoiceRecord artifact type + mapping, fed by the E7-S7 billing handoff
- **Tests:** one golden bundle per new profile/artifact validates

---

# EPIC E7 — Human-in-the-Loop / Counter-Side Review Hardening

### Phase 3

#### Block E7-S2 — Reviewer assignment + SLA queue 🔄 🔒
**Status:** Pending — reset from 🔄: code at `fdd4aeb` not provided
`Must · BLOCKER · 3 d · 5 pts · Deps: E7-S1 · Arch ✔ §13, §14 #6 · DoD-CODE`
- [x] Workbasket → worklist claim/assign (`/api/reviewer/workbasket`, `/worklist`, `/items/{id}/claim`), confirmed in code
- [ ] An unclaimed `review_task` older than (SLA minutes − buffer) raises an alert
- [ ] A reviewer sees only tasks assigned to them or claimable by them
- [ ] Average time-in-queue visible on the E10-S2 dashboard
- **Tests:** an aged task triggers an alert; visibility tests per reviewer
- 🔒 Reviewer staffing/roster

#### Block E7-S8 — Customer 360 patient view (read model) ✅
**Status:** Pending — reset from ✅: code at `fdd4aeb` not provided; not verifiable
`Should · HARDEN · 3 d · 2 pts · Deps: E7-S2 · Arch ✖ (not in the repo's ARCHITECTURE.md; a documentation gap) · DoD-CODE`
- [x] `GET /api/reviewer/c360/{mpi}` returns demographics, dual identifiers (ABHA + legacy MRN) and clinical facts across documents; linked from the worklist at `/reviewer`
- **Evidence (Excel):** verified on-pod against a real dual-identifier patient (pod since recycled). Confirmed in code (`webapp/reviewer.py`), commit `fdd4aeb`.

#### Block E7-S3 — Keyboard-driven review UI ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E7-S1 · Arch ✔ §14 #6 · DoD-CODE`
- [ ] Accept/correct/reject via hotkeys; tab through fields; no mouse needed
- [ ] Measure facts/minute on a timed task; write it into *Sizing & Capacity* (facts/reviewer/min, currently row 45)
- **Tests:** keyboard-only UI smoke test; timed-task log

#### Block E7-S10 — Review-console draft persistence + optimistic concurrency ➕ ⛔
**Status:** Pending
`Must · BLOCKER · 2.5 d · 5 pts · Deps: E7-S1, E7-S2, E7-S3 · Arch ✖ (the concurrency half extends §5 S8 review writes; UI resilience isn't named) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** stays ⛔ HOLD (Arch ✖) — draft persistence + optimistic concurrency is not in ARCHITECTURE.md or any accepted ADR (§15 'Not accepted here'). Needs an explicit owner decision.
Protects the scarcest resource in the plan — reviewer-minutes are the scarcest operating capacity, and
R10 forbids recovering lost time by auto-accepting. The second half closes a lost-update hole on
governed clinical values.
- [ ] In-progress corrections persist locally per claimed item; a network drop or a reload restores the draft, never a blank form
- [ ] Queued decisions replay on reconnect **through the server's version check**, never blind-applied
- [ ] Every fact carries a version/ETag; a stale write is rejected and re-presented for review rather than silently overwriting (interacts with E7-S2 claiming and E7-S5 dual review)
- [ ] A draft is never mistaken for a decision: local state cannot produce `clinician_confirmed` / `corrected` without an acknowledged server write
- [ ] Reviewer identity comes from the E7-S1 session, never from restored local state
- **Tests:** offline-drop-and-restore test; concurrent two-reviewer edit — one write rejected, none lost; a negative test proving local state cannot forge a governed transition

#### Block E7-S5 — Dual review for high-risk facts ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E7-S1 · Arch ✔ §5 S6 check_medication, §14 #6 · DoD-CODE`
- [ ] Config-driven high-risk rule set (e.g. doses above the ceiling, allergies)
- [ ] A high-risk fact can't reach `clinician_confirmed`/`corrected` without a second, different reviewer identity
- [ ] Target-architecture policy (config flag): drug and dose fields always go to HITL and are never auto-accepted; the reviewer-count consequence is accepted (decision 2026-09-20)
- **Tests:** single-reviewer approval is blocked; same-reviewer second approval is rejected
- 🔒 Clinical owner defines the high-risk fact policy

#### Block E7-S6 — Counter-side patient-status board 🔒
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E7-S2 · Arch ✔ [ADR-0005](adr/0005-counter-billing-contract.md) §15.4 (E1-S8 re-score; was ✖ (cites the brief "human in the loop … at the counter"; no billing/counter board in ARCHITECTURE.md)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Screen lists in-flight patients with per-document status (governed / held / count) and an ETA-to-ready
- [ ] Updates live as S8 decisions land (reusing the live re-projection on `GET /api/jobs/{id}/fhir`)
- **Tests:** a review decision updates the board without a manual refresh
- 🔒 Billing-counter workflow owner

#### Block E7-S7 — Billing handoff contract: governed JSON, unresolved facts explicit 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E7-S2, E6-S6 · Arch ✔ [ADR-0005](adr/0005-counter-billing-contract.md) §15.4 (E1-S8 re-score; was ✖ (user requirement "JSON before billing"; ARCHITECTURE.md has no billing contract)) · DoD: contract tests cover partial, held, corrected and fully governed bundles; UI and API use the same state machine and audit trail`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Status API exposes `uploaded_docs`, `processed_docs`, `governed_fact_count`, `held_fact_count`, `identity_status`, `FHIR_validation_status`, `estimated_ready_at`
- [ ] `billing_ready=true` impossible while identity fields, configured billing-critical facts or FHIR validation are unresolved
- [ ] Unresolved non-critical facts serialized separately, never labelled as confirmed
- **Tests:** contract tests for each bundle state
- 🔒 Billing + clinical owner approve the billing-critical field set

#### Block E7-S9 — Annotate-from-scratch mode (anchoring-bias check) ⛔
**Status:** Pending
`Could · OPTIMIZE · 2 d · 1 pt · Deps: E2-S4, E7-S3 · Arch ✖ (from CDI-Adapter-Finetuning-Plan.md §1.4) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** stays ⛔ HOLD (Arch ✖) — annotate-from-scratch mode is not in ARCHITECTURE.md or any accepted ADR (§15 'Not accepted here'). Needs an explicit owner decision.
- [ ] A configurable sample (e.g. random 10% of Tier-A gold annotations) opens with the AI-proposed value hidden
- [ ] Capture both from-scratch and normal-mode answers, joinable per document/fact for the E2-S7 comparison
- **Tests:** a hidden-value item captures both answers

#### Block E7-S4 — Reviewer-copilot suggestions in the review UI
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E12-S4, E7-S3 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 (advisory only) (E1-S8 re-score; was ✖ (§14 #6 lists no copilot)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Show E12-S4 output next to each held fact: pre-filled correction + one-line rationale + evidence citation; the human still clicks accept/edit/reject
- [ ] For ≥ 50% of held facts on E2-S4, the suggestion matches the eventual human decision
- [ ] Timed A/B vs E7-S3 alone shows fewer seconds per fact
- **Tests:** match-rate evaluation + timed A/B

---

# EPIC E8 — ABDM Gateway & Consent

### Phase 8

#### Block E8-S1 — HFR/HPR registration flow ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: — · Arch ✔ §4.1, §13, §14 #7 · DoD-DEPLOY`
- [ ] Register the facility (HFR) + at least one practitioner (HPR) in the ABDM sandbox
- [ ] Store the returned ids; `fhir/resources.py` Organization/Practitioner builders reference them
- **Tests:** sandbox registration; builders emit the stored ids
- 🔒 ABDM sandbox / facility credentials

#### Block E8-S2 — Consent-artifact intake + care-context linking ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 4 d · 8 pts · Deps: E8-S1 · Arch ✔ §4.1, §14 #7 · DoD-CODE`
- [ ] Populate `abdm_care_context` / `abdm_consent` with real logic
- [ ] Store a sandbox consent artifact and link it to the correct `patient_identity` + care context
- [ ] No HIU push (E8-S4) without a valid, unexpired consent
- **Tests:** push without consent is refused; an expired consent is refused
- 🔒 ABDM consent workflow owner

#### Block E8-S3 — Fidelius (ECDH) encryption for the HIU push ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 4 d · 8 pts · Deps: E8-S2 · Arch ✔ §4.1 abdm_transfer, §12, §14 #7 · DoD-SEC`
- [ ] ECDH-encrypt bundles end to end to a simulated HIU
- [ ] Code review confirms no private key material is ever written to disk or logs
- **Tests:** round trip with a test keypair; key-persistence check
- 🔒 Security review

#### Block E8-S4 — HIU push + status callback + retry/dead-letter ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E8-S3, E9-S3 · Arch ✔ §4.1, §14 #7 · DoD-CODE`
- [ ] Complete the transfer flow with a status callback
- [ ] A simulated HIU outage → retry with backoff → dead-letter after N attempts, visible on the E10 dashboard, never silently dropped
- **Tests:** outage simulation

#### Block E8-S5 — Deterministic MPI blocking/scoring + real ABHA verification ⬜ 🔒
**Status:** Pending
`Should · HARDEN · 6 d · 5 pts · Deps: E8-S1, E6-S3 · Arch ✔ §13 Identity/MPI ("no blocking/scoring, no ABHA verification") · DoD-CODE`
- [ ] A deterministic blocking/scoring algorithm (not pure fuzzy ratio) ranks MPI candidates before auto-merge
- [ ] Check a document's ABHA against the live ABDM registry (sandbox first); surface mismatched/unverified results, never silently accept them
- **Tests:** a candidate-ranking fixture; sandbox verify pass/fail
- 🔒 ABDM sandbox credentials

---

# EPIC E12 — Agentic Orchestration Layer

> Lifted from ⛔ HOLD by E1-S8 (2026-09-21): ARCHITECTURE.md §15.2 + ADR-0003 now carry a bounded agentic
> control plane under a persisted deterministic workflow. Previous note: the repo's ARCHITECTURE.md described a deterministic 9-stage
> pipeline and Celery/Temporal queueing (§3.2, §14 #8). It names no agent layer, critic, copilot or
> `agent_trace` table. These stories come from the user's "agentic + no-guess" mandate in the Excel.

### Phase 1

#### Block E12-S1 — Deterministic workflow orchestrator with bounded agent tools
**Status:** Pending
`Must · BLOCKER · 5 d · 8 pts · Deps: — · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 (E1-S8 re-score; was ✖) · DoD: merged via PR with passing CI; state-transition tests cover happy path, timeout, retry, duplicate delivery and illegal transition; ARCHITECTURE.md updated; agent trace linked to fact_provenance`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Persisted, replayable workflow/state machine (per §0.5) owns every document transition
- [ ] Agent/tool calls allow-listed, schema-validated and time-bounded (OCR retry, routing, terminology lookup, grounding check, human escalation)
- [ ] Negative test: an agent can't directly set `auto_accepted` / `clinician_confirmed` / `ready_to_share`
- [ ] The S6 gate stays the only machine authority for fact acceptance
- **Tests:** the state-transition suite above

#### Block E12-S2 — Terminology tool-calling for the extraction agent
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E3-S4, E12-S1 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 rule 2 (E1-S8 re-score; was ✖) · DoD-EVAL`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Agent calls Snowstorm `$lookup` / `$validate-code` as live tools during extraction
- [ ] Higher bound-code rate on E2-S4 than the post-hoc matcher, at equal or lower latency
- **Tests:** A/B on the gold set

#### Block E12-S3 — Grounding/critic agent (no-guess enforcement)
**Status:** Pending
`Must · BLOCKER · 4 d · 8 pts · Deps: E12-S1 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 rule 4 (E1-S8 re-score; was ✖) · DoD-EVAL · the agent form of E4-S4`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Re-read the cropped evidence image for every candidate fact; faithfulness check before auto-accept eligibility
- [ ] Logged as a distinct step in the E12-S5 trace; the critic can only veto/escalate
- [ ] Default verification path is CPU (RapidOCR on the cropped evidence); GPU only for already-escalated pages, so the critic adds no per-fact GPU cost
- **Tests:** ≥ 95% catch rate on injected mis-slotted values

### Phase 3

#### Block E12-S4 — Reviewer-copilot agent
**Status:** In Progress (2026-09-26) — built for **medication lines** at the owner's request: the medication agent (`agent/medication.py`, `docs/medication-agent.md`) suggests the catalogue product with the evidence text, a one-line rationale and the diagnosis it treats; the Reviewer's "Use suggestion" pre-fills the Edit form (no auto-apply; tests `test_agent_*`). Open: other fact types, the E2-S4 gold-set evaluation (DoD-EVAL), and E12-S1 (the orchestrator) underneath.
`Should · HARDEN · 3 d · 5 pts · Deps: E12-S1, E7-S1 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 (E1-S8 re-score; was ✖) · DoD-EVAL`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] For every held fact: suggested value + exact evidence text + one-sentence rationale
- [ ] The human decision is never bypassed or auto-applied
- **Tests:** output-shape tests; a no-auto-apply test

### Phase 7

#### Block E12-S5 — Agent trace logging
**Status:** In Progress (2026-09-26) — `agent_trace` (migration 0010) records every step of the medication agent (model calls, catalogue tool calls, guard decisions) with input, output, confidence and timing, joined to the fact and the `ai_event`; the step sequence is asserted in `test_agent_integration.py`. Open: the `audit_log` join for E12-S1's workflow agents.
`Must · BLOCKER · 2 d · 3 pts · Deps: E12-S1 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 rule 5 (E1-S8 re-score; was ✖ (§4.3 provenance exists; an agent_trace table isn't named)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] New `agent_trace` table (tool called, input, output, confidence) joined to `audit_log` / `fact_provenance`
- [ ] Any document's full agent decision sequence can be rebuilt from `agent_trace` alone
- **Tests:** reconstruction test

#### Block E12-S6 — Agent evaluation harness
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E12-S3, E2-S7 · Arch ✔ [ADR-0003](adr/0003-agentic-control-plane.md) §15.2 (E1-S8 re-score; was ✖) · DoD-EVAL`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] One command replays E2-S4 through the full agent loop (E12-S1–S3)
- [ ] Reports E2-S7 metrics + tool-call precision/recall for E12-S2
- **Tests:** a harness run on a fixture subset

---

# EPIC E15 — Intake Channels & Clinic-System Integration

> ⛔ New epic from the target architecture diagram (2026-09-20). Neither `docs/ARCHITECTURE.md` nor the Excel carry it, so every block here is on HOLD until E1-S8 records a decision. **E1-S8 (2026-09-21): not accepted — both blocks stay ⛔ HOLD pending an explicit owner decision (ARCHITECTURE.md §15).**

### Phase 4

#### Block E15-S1 — WhatsApp document intake for the OPD front desk ➕ ⛔ 🔒 ⬜
**Status:** Pending
`Should · HARDEN · 4 d · 5 pts · Deps: E9-S1, E6-S6 · Arch ✖ (uploads today are the web form and folder-watch only) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** stays ⛔ HOLD (Arch ✖) — WhatsApp intake is not in ARCHITECTURE.md or any accepted ADR (§15 'Not accepted here'). Needs an explicit owner decision.
The target architecture shows the OPD front desk sending prescriptions/documents by WhatsApp as well as by the web upload. It enters through the same canonical intake path, never around it.
- [ ] A WhatsApp Business webhook receives media from the front-desk number and hands it to the same validate → persist → enqueue → ACK path as `POST /api/jobs` (same idempotency key, same content hash dedupe)
- [ ] Every message is bound to a registered patient before extraction (E6-S6 rule). An unbound document goes to a holding queue for the counter; identity is never guessed from the message or the document
- [ ] The original media is durably stored **before** anything is acknowledged; the reply to the sender is a receipt only and contains no clinical content
- [ ] Consent wording shown/recorded at first contact; size, type and rate limits enforced
- **Tests:** duplicate media → one job; unbound patient → holding queue, no extraction; storage-down → no ACK
- 🔒 WhatsApp Business API approval; legal sign-off on the consent text

### Phase 8

#### Block E15-S2 — Clinic/HIS API integration: patient master read, governed write-back ➕ ⛔ 🔒 ⬜
**Status:** Pending
`Should · HARDEN · 6 d · 8 pts · Deps: E6-S6, E7-S7 · Arch ✖ (§13: "Legacy write-back / FHIR façade ❌ not built"; the legacy DB is read-only) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** stays ⛔ HOLD (Arch ✖) — HIS integration is not in ARCHITECTURE.md or any accepted ADR (§15 'Not accepted here'). Needs an explicit owner decision.
The target architecture shows the Clinic/HIS API as the source of authoritative patient facts (read) and the destination of governed updates (write).
- [ ] Read authoritative patient master data through the HIS API; it feeds the registration source of truth used by E6-S6
- [ ] Write back **only** governed facts (never machine-only or held facts), idempotently, with an audit record per write and the provenance id attached
- [ ] **Decision recorded (owner sign-off):** which store is the system of record for governed CDI facts. Recommended default: CDI Postgres keeps facts + provenance (needed by §4.3 provenance, E4-S5, E11-S7 shredding) and the HIS API is a sync target. The diagram's "Postgres holds no facts" would remove those guarantees
- [ ] Record a decision on whether a FHIR façade (a read API over CDI bundles) is needed beyond the E8 push flow — §13 lists "Legacy write-back / FHIR façade"; nothing is built for the façade until it is decided
- [ ] HIS outage never blocks intake or review; writes queue and retry (E9-S8 breaker rules apply)
- **Tests:** held fact is never written; replayed write is a no-op; HIS-down → queued, no data loss
- 🔒 HIS vendor API access; data-owner decision on system of record

---

## 8. Traceability

| Epic | Blocks in this file | Done | In Progress | ⛔ HOLD | ➕ Added |
|---|---|---|---|---|---|
| E2 S4 Extraction Quality & Serving Acceleration | 5 | — | — | — | S10 |
| E3 Real Terminology Service | 5 | — | — | — | — |
| E4 S6 Calibration & No-Guess Governance | 6 | — | — | — | — |
| E5 S7 Longitudinal Reconciliation | 4 | — | — | — | — |
| E6 FHIR/ABDM IG Validation & Correctness | 7 | — | S1 | S8 | — |
| E7 Human-in-the-Loop / Counter-Side Review Hardening | 9 | S8 | S2 | S10, S9 | S10 |
| E8 ABDM Gateway & Consent | 5 | — | — | — | — |
| E12 Agentic Orchestration Layer | 6 | — | — | — | — |
| E15 Intake Channels & Clinic-System Integration | 2 | — | — | S1, S2 | S1, S2 |
| **Total** | **49** | 1 | 2 | 5 | 4 |

### 8.2 ARCHITECTURE.md §14 roadmap → blocks in this file

| §14 item | Blocks |
|---|---|
| #1 Harden + accelerate S4 (quality half) | E2-S4, S5, S7, S9, S10 (serving half: infra file) |
| #2 Real terminology service | E3-S1…S5 |
| #3 Fit the S6 calibrator | E4-S1…S3 |
| #4 S7 longitudinal reconciliation | E5-S1…S4 |
| #5 FHIR/IG validation in-loop | E6-S1, S2, S3, S4, S6, S7, S8; E3-S4 |
| #6 Review console hardening | E7-S2…S10, E4-S1 (AuthN E7-S1: infra file) |
| #7 ABDM edge | E8-S1…S4 |
| §13 Identity/MPI gap | E8-S5, E6-S6 |
| Target architecture: intake + HIS | E15-S1, E15-S2 |
| No-guess governance (user mandate) | E4-S4, E4-S5, E4-S6, E12-S1…S6 |

---

## 9. Change log

| Date | Block | Change | Tests |
|---|---|---|---|
| 2026-09-19 | — | BACKLOG.md rebuilt only from `CDI-Adapter-Production-Backlog-Enhanced.xlsx` (91 stories / 14 epics / 9 phases); each block checked against `docs/ARCHITECTURE.md`. | not run (no code changed) |
| 2026-09-20 | E1-S8, E1-S9, E2-S10, E7-S10, E9-S8, E10-S10, E10-S11, E11-S7 | 8 blocks added from an end-to-end production-reliability review. | not run (no code changed) |
| 2026-09-20 (rev 2) | all | **Split into feature and infra files** (49 + 54 blocks). Applied decisions: Blackwell baseline, ₹75k/month, reviewer pool not a blocker. Resequenced E11-S1, E11-S4, E10-S7, E6-S8, E4-S6, E13-S5 to clear every phase-dependency conflict. Added E15-S1, E15-S2, E1-S10, E13-S6; retargeted E13-S1, E13-S2; CPU-by-default critic task on E4-S4/E12-S3; drug/dose HITL flag on E7-S5. | not run (no code changed) |
| 2026-09-21 | all | Source files verified (§B). `**Status:**` line added to all 49 + 54 blocks; ✅/🔄 blocks reset to Pending (code not provided). Epic E0 / Phase 0 (16 blocks) added. E1-S8 mandate decisions recorded. | not run (no code changed) |
| 2026-09-21 | E0-S1 | **Completed.** Project skeleton, `Settings` (all §9 keys), structlog, cgroup-aware thread caps, `.env.example`, README. | 19 passed / 0 failed; ruff + format + mypy clean; §9 traceability 24/24 |
| 2026-09-21 | — | Owner decision: **no Docker**; Postgres/MinIO/Redis run as native processes (ARCHITECTURE §3.3). §B environment note updated. | not run (docs only) |
| 2026-09-21 | E0-S2 | **Blocked.** Schema (20 tables), 5 views, migrations 0001–0004, `db.py`, `repo.py` written. Integration tests need a native PostgreSQL 16, which is not installed. | 31 passed, 7 skipped (integration: no server); ruff + format + mypy clean |
| 2026-09-21 | E0-S2 | Still **Blocked.** Native PostgreSQL 16.15 (official EDB zip) downloaded; Smart App Control blocks its unsigned `initdb.exe`. MinIO: the official server returns HTTP 410 Gone for Windows binaries (affects E0-S3). `scripts/dev/pg_local.ps1` added (dev/test only). | integration not runnable |
| 2026-09-21 | E0-S2 | **Completed.** Switched to PostgreSQL 16.14 (official EDB zip, SHA-256 `98AF1417…A2F04663`), whose `initdb.exe` passes Smart App Control by reputation; 16.15 removed. Server runs natively on 127.0.0.1:5432. | integration 7/7 passed; full suite 38/38; `alembic upgrade head` → 0004; ruff + format + mypy clean |
| 2026-09-21 | E0-S3 | **Blocked.** `storage.py`, `ingest/pages.py`, `ingest/service.py`, S1 repo queries written. Real-MinIO integration is impossible on this machine (no official binaries; local builds blocked by Smart App Control). Owner decision: no substitute servers. Real-MinIO verification added to E0-S16 (pod). | unit 31/31; suite 69 passed / 4 skipped (MinIO); ruff + format + mypy clean |
| 2026-09-21 | E0-S4 | **Completed.** 8 JSON schemas, ML client (extract/repair/validate/retry/`_partial`), gateway with stub + hf backends. Gateway verified as a native process over HTTP. | unit 77/77; suite 146 passed / 4 skipped (MinIO); ruff + format + mypy clean |
| 2026-09-21 | E0-S5 | **Blocked** (MinIO). RapidOCR + VLM transcription + `ocr_document`; OCR DAL; `CDI_OCR_MIN_CONF`. Real RapidOCR verified locally; DAL verified on PostgreSQL. | unit 15/15; PG DAL 5/5; suite 166 passed / 6 skipped (MinIO); ruff + format + mypy clean |
| 2026-09-21 | E0-S6 | **Blocked** (MinIO, 2 chain tests). Scored heuristic + VLM fallback + `doc_classification`. Heuristic path verified on PostgreSQL; the dose-strength signal was added after that test exposed a score of 8 vs §5's 9+. | unit 13/13; PG heuristic 1/1; suite 179 passed / 8 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S7 | **Completed.** Patient registry + MPI + shared date parser. 3 bugs found by tests and fixed. | 63/63 (real PostgreSQL); suite 242 passed / 8 skipped (MinIO); ruff + format + mypy clean |
| 2026-09-21 | E0-S8 | **Blocked** (MinIO, 1 chain test). S4 prompts, fact handlers, medication parsing, `write_extraction`, `extract_document`, S4 DAL. | unit 51/51; PG 7/7; suite 300 passed / 9 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S9 | **Completed.** Seed terminology (129 concepts) + binding service. §8.2 codes reproduced exactly on PostgreSQL. | unit 28/28; PG 2/2; suite 330 passed / 9 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S10 | **Completed.** S6 rules + calibration + governance gate + conflicts + review tasks. Appendix D behaviour reproduced on PostgreSQL. | unit 49/49; PG 6/6; suite 386 passed / 9 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S11 | **Blocked** (MinIO, 1 inline-scan test). FHIR builders, gated projection, `_lint`, Python ABDM conformance with HAPI auto-switch. Gate enforced at S9, verified on PostgreSQL. | unit 26/26; PG 6/6; suite 419 passed / 10 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S12 | **Completed.** S8 review API + console page + theme. Fixed Decimal-as-string JSON defect found by the tests. | 10/10 (PG) + 1 unit; suite 430 passed / 10 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S13 | **Blocked** (MinIO: end-to-end job, page/original endpoints). Web app, job orchestrator, upload page, registry + FHIR endpoints. Fixed a `/healthz` hang; faster test DB reset. Verified as a native process. | jobs unit 6/6; PG 11/11; suite 447 passed / 11 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S14 | **Completed.** Canonical promotion, `/reviewer` workbasket/worklist, canonical FHIR bundle, `/admin` inspector; job → workbasket. Fixed 2 audit-sampling-dependent flaky tests. | PG 7/7 (corrected from 12) + jobs unit 7/7; suite 455 passed / 11 skipped ×3 runs; ruff + format + mypy clean |
| 2026-09-21 | E0-S8, S9–S16 | **Reverted to match ARCHITECTURE.md** (owner request): one BP fact with FHIR components; 700 KB inline constant + s3:// URL; 5-task Celery chain; uncoded Condition = lint error; both facts pulled back on contradiction; caller's `mime_type` used; approve per §4.5 (held facts not promoted, no verification); only §7.1 endpoints (`/original`, `/api/admin/*` removed; `/admin` server-rendered); boto3 default timeouts; random audit sampling; no number conversion; no OCR-tolerant S2 anchors. Details in `BACKLOG_AS_BUILT.md` §5. | 495 passed / 11 skipped; ruff + format + mypy clean |
| 2026-09-21 | — | Added `docs/BACKLOG_AS_BUILT.md`: a backlog derived from the code (modules, collected test counts, routes, open work). Corrected the E0-S14 test count. | not run (docs only) |
| 2026-09-21 | E0-S15 | **Blocked** (real Redis + MinIO). Celery worker (6 tasks), folder watcher + `cdi-ingest-watch`, ingest API. Fixed a security defect: client-declared MIME type was trusted. | 15/15 + 3 regression; suite 474 passed / 11 skipped; ruff + format + mypy clean |
| 2026-09-21 | E0-S16 | **Blocked** (needs the RunPod pod). Pod scripts (MinIO from source), compose alt-deploy (quay.io images, tags verified), Makefile, `.env.runpod`, sample docs, smoke/batch scripts, `verify_s3.sql`. OCR-tolerant S2 anchors. | ops 15/15 + PG 1/1; suite 491 passed / 11 skipped; ruff + format + mypy clean |
| 2026-09-21 | Phase 0 | **All 16 E0 blocks built.** Completed: S1, S2, S4, S7, S9, S10, S12, S14. Blocked on the pod (real MinIO / Redis / GPU): S3, S5, S6, S8, S11, S13, S15, S16 — each has code complete and every non-object-store path verified; the 11 skipped tests are exactly the pod-only ones. | 491 passed / 11 skipped |
| 2026-09-21 | E1-S8 (infra file) | HOLD re-score applied here: lifted ✔ E4-S4, E7-S4, E7-S6, E7-S7, E12-S1…S6; lifted ◐ E5-S4; **kept ⛔** E6-S8, E7-S10, E7-S9, E15-S1, E15-S2 (16 → 5). | not run (docs only) |
| 2026-09-25 | E0-S3 | **Completed** (was Blocked on a real MinIO). No code change. Checked on the Colab L4 with the real MinIO (pinned source build) and PostgreSQL 16.15. | Colab, `CDI_TEST_REQUIRE_INFRA=1`: `test_ingest_integration.py` 4/4 passed; ingest re-render test passed; `test_pages.py` 33/33 on Linux; live bucket: 50 documents, 50 pages, every original/page/thumbnail object present at the §4.1 layout, 0 missing. |
| 2026-09-25 | E0-S5 | **Completed** (was Blocked on a real MinIO). No code change. Checked on the Colab L4 with the real MinIO, PostgreSQL 16.15 and the GPU gateway. | Colab, `CDI_TEST_REQUIRE_INFRA=1`: `test_ocr_integration.py` 2/2, `test_repo_integration.py` 5/5, `test_ocr_unit.py` 15/15 (real RapidOCR). Live: 50 documents, 457 blocks, min conf 0.83, 0 boxes outside the page, 54/54 OCR runs ok. Real VLM transcription (Qwen2.5-VL-3B, 16.9 s) of a synthetic page: all 3 lines exact, superseding the RapidOCR blocks. |
| 2026-09-25 | E0-S13 | **Completed** (was Blocked on a real MinIO). Added the missing `GET /api/documents/{id}/original` (clinical roles; its policy entry is in `auth.POLICY`), and the end-to-end test now reads the page image and the original back and checks the intake role is refused. | Local: lint/format/types clean; auth + web tests 51 passed (the MinIO test skips here). Colab, `CDI_TEST_REQUIRE_INFRA=1`: `test_webapp_integration.py` 12/12, unit tests 82/82. Live through the real site on the GPU: upload 202, job at review in 30 s, page PNG + byte-identical original from MinIO, FHIR download ok. The 3B model's extraction was `_partial` (0 facts released): extraction quality (E2), not the web app. |
| 2026-09-25 | S4 extraction on the `hf` backend (fix; E0-S8 / E2 area) | **Found:** uploads gave 0-1 facts. Preprocessing and OCR were fine and the 7B read the page correctly, but the `hf` gateway ignored `json_schema` (only vLLM enforces it, E2-S3) and the prompt named the schema without its keys, so the model invented its own layout (`{"prescription": {...}}`, `drug`, `dose_pattern`, a flat single medicine) and the repair dropped it all: `{"confidence": 0.5}`, `_partial`, 0 facts. **Fixed:** `mlserve/backends.py` `with_schema` appends the schema's exact key layout to the prompt on `hf` (quantities shown as text "as written", codes never asked of the model, dose/route "only if written"; 3 rounds, below); `ml/client.py` `repair_payload` now unwraps one wrapper key, turns a lone flat item into its list, maps item-field aliases (`dose_pattern`->`frequency`, `age`->`age_text`, ...), top-level aliases (`date`, `doctor`), loose vitals (`blood_pressure`, `weight`, ...) and follow-up lines, and logs by name any key it still drops. Tests `tests/test_extract_repair_unit.py` (10); check script `scripts/extraction_check.py`. | Local CI GREEN (874 passed); Colab unit 105/105. **Live 7B on Colab, 5 synthetic scanned prescriptions (3-8 medicines, syrups/injection/drops, table layout, allergy), real site -> worker -> gateway, saved payload scored against the page.** Before: medicines 1/26, fields 1/3, dx 3/9, vitals 0/11. Round 1 (keys shown): medicines 26/26 but dose patterns scrambled (numeric `dose` + shifted `frequency`), BP split, C1 only required keys. Round 2 (text values, hints): 81/82 fields, but `dose "1 tab"` / `route "oral"` invented. **Round 3 (final): medicines 26/26, fields 82/82, dx 9/9, vitals 11/11, allergy 1/1, 0 values invented, 0 lost in repair, saved payload = repaired model answer in 5/5.** Remaining: see the E2 notes (hf not grammar-locked; a comma-joined diagnosis line can stay one condition; 76-131 s per page on hf; handwritten photos not yet tested with de-identified samples). |
| 2026-09-25 | FHIR download (fix; E6 / S9 area) | **Found:** the downloaded file was not FHIR (a wrapper `{status, issues, bundles[...]}`) and held every document of the patient, not the upload's; the MedicationRequest dropped what was read (strength, duration, "after food" put in `route`); `"authoredOn": null` could reach the file; a line such as "Type 2 diabetes mellitus, Hypertension" became ONE uncoded Condition (held, then a lint error once confirmed). **Fixed:** `webapp/app.py` `job_bundles` keeps the upload's own documents; `download_bundle` returns the document Bundle itself (several documents: a `collection` Bundle of them), `application/fhir+json`; review state stays in `/api/jobs/{id}/fhir`. `fhir/resources.py` `medication_request`: name + strength, the dosage line as written or rebuilt ("500 mg · 1-0-1 · for 30 days · after food"), `patientInstruction`, `timing.repeat.boundsDuration`; `prune()` drops null / empty values at every depth. `extract/facts.py`: food/timing phrases move from route to instructions; `split_joined` splits a diagnosis line only when the whole line codes to nothing and every part codes (so "Diabetes mellitus, type 2" stays one). Page link reads "Download FHIR record (this upload only ...)". Goldens regenerated (only `boundsDuration` added). Tests: `test_fhir_download_unit.py` (3), 2 new in `test_fhir_unit.py`, 1 in `test_extract_unit.py`; `test_python_conformance...` now pins the Python engine (it ran HAPI on hosts with the validator). Live check `scripts/fhir_download_check.py`. | Local CI GREEN. Colab: goldens byte-identical to local; `fhir_golden.py --validate` (HAPI 6.10.4, IG ndhm.in 6.5.0) no new errors on all 5 artifacts; FHIR/extract unit tests pass. **Live 7B on Colab, real site -> queue -> worker -> gateway:** two uploads of a synthetic Rx for the same patient; the second job's download = one document Bundle (Composition first, 14 entries: 3 MedicationRequest, 2 Condition coded SNOMED 44054006/E11 and 59621000/I10, 2 vital Observations, DocumentReference = this upload's file only), 0 null/empty values, 0 lint, 0 conformance errors; 7 facts all auto-accepted (0 held). Before the split fix the same page gave 1 uncoded Condition. |
| 2026-09-25 | Review / Reviewer tabs (fix; E0-S14 / S8 area) + migration 0008 | **Found (owner report: new uploads not reflected):** the same image uploaded again is the same document (sha256 dedupe), re-extracted: `purge_document_facts` deletes its old facts and their `rv_item_element` rows (ON DELETE CASCADE), but `create_rv_item_for_document` returned the existing workbasket item untouched, so the Reviewer tab showed it with **0 elements** (the owner's item: 0 elements, 7 current facts). Both lists were also oldest first, so a new upload sat below every older item (19th of 20 on Review). **Fixed:** `canon/repo.py` `_refresh` adds an element for every current fact the item does not already resolve to (corrections are versions, not new facts), updates `job_id`, records `rv_action` `refresh` (migration **0008** adds it to the action CHECK); `list_items` orders by latest activity, newest first; `review.open_tasks` orders priority, then newest first. Test `test_a_document_uploaded_again_refreshes_its_item_and_tops_the_workbasket`; migration-head tests -> 0008. | Local CI GREEN. Colab (real PostgreSQL 16.15): `alembic upgrade head` 0007 -> 0008 on the live DB; reviewer/review/schema/metrics/validate integration **56/57** (the new test passed; the 1 failure is the known Colab-only HAPI one: an approved canonical bundle stays `draft` because the real validator rejects it, E6). Live: the owner's item refreshed to 7 elements / 7 held and is first on both /review and /reviewer. |
| 2026-09-25 | Reviewer: download the approved FHIR bundle (E0-S14 area) | **Found (owner):** after "Approve & promote" there was no way to download the canonical FHIR bundle; the item leaves the worklist and `/api/reviewer/items/{id}/bundle` returns the stored row (bundle + validation + status), not a FHIR file. **Added:** `GET /api/reviewer/items/{id}/bundle/download` (the Bundle itself, `application/fhir+json`, attachment `fhir_<artifact>_<item>.json`; 404 before approval) and `GET /api/reviewer/approved` (latest 20 approved items, newest first, with the bundle's status); both clinical-only in `auth.POLICY`. The Reviewer page gets an **Approved** section with a "Download FHIR bundle" link per item and the same link in the message after approving. `canon.list_items` takes a `limit`. Tests: download + approved list in the full workbasket flow, 404 before approval. | Local CI GREEN (882 passed). Colab (real PostgreSQL): reviewer + auth integration and auth unit **83/83** (the validator jar pointed away so the flow reaches `ready_to_share` with the Python engine; the real-HAPI run keeps the known `draft`). Live: the owner's approved item (`24.jpeg.jpeg`, CFP-2026-000042) listed under Approved as `draft`; its download = FHIR document Bundle, 18 entries (9 MedicationRequest, 1 Condition, 1 Observation), `application/fhir+json`. |
| 2026-09-25 | S2 classification of hf answers (fix) + upload-page status | **Found (owner: uploads not reaching Review/Reviewer):** on the live 7B, three uploads answered `"is_handwritten": "true"` (a string); the classification failed `classification.v1`, fell back to `other` (0.5), extraction was skipped, 0 facts, so nothing reached either review queue, and the upload page still showed "All facts governed". **Fixed:** `ml/client.py` `_scalar` in `repair_payload` (top level and inside items): a plain boolean or number written as text (`"true"`/`"yes"`/`"false"`/`"no"`, `"1"`, `"0.9"`) becomes the schema's type, only where the schema does not accept text; anything else stays as written. `webapp/page.py`: a skipped/failed document shows "not processed" with the reason; no facts shows "No facts extracted", never "All facts governed". | Local CI GREEN (892 passed). Test `test_a_boolean_or_number_written_as_text_is_read_as_the_schema_type`; page test in `test_fhir_download_unit.py`. Live re-check on Colab after the bundle sync. |
| 2026-09-25 | Reviewer: Edit a fact (S8 / E0-S14 area) | **Owner:** an edit option for each fact on the Reviewer page. The S8 correction existed, but only for held facts (an accepted fact answered 409 "already adjudicated"), and a medication correction changed only the fact's text: its `medication_detail`, which the canonical EMR and FHIR carry, was copied unchanged. **Added:** `review.py`: `correct` also on `clinician_confirmed` / `corrected` facts until the fact's review item is **approved** (then 409: its values are in the canonical EMR); medication corrections of `drug_text`, strength, dose, frequency, duration, route, instructions (numbers checked, 422 otherwise; `frequency_per_day` and as-needed re-read from the corrected frequency; units to UCUM where known); a changed text is coded again (`bind_text`), never keeps the old text's code; `rxlike_*` follows. `GET /api/reviewer/items/{id}/elements` carries each medication's detail. Reviewer page: an **Edit** button on every fact (not rejected) opening a form (text; value/unit for vitals and labs; the medication fields), Save sends only the changed fields with an optional reason; the medication detail line shows under its name. Every edit is a new fact version that supersedes the old, signed by the session user, audited. | Local, real PostgreSQL 16.14: reviewer + review + schema integration **35/35** (new: accept -> edit medication -> edit again -> approve -> canonical `cn_medication` carries 500 mg / 2 per day -> edit after approval 409; bad number 422). |
| 2026-09-25 | Reviewer: approve once, one patient per document, Approved once per document; patient merge tool | **Found (live, owner's `4.jpeg.jpeg`):** (1) approve runs the validator for seconds; clicks during that time each read the item as still `claimed`, so one item was promoted **5 times** (5 copies of each fact in the canonical EMR, 5 bundles); the panel kept Edit / Approve buttons after approval; (2) the same image uploaded again (same bytes = same document, extracted again) was filed under a **second patient** "Rahul": a name-only reading never matches, so `resolve_identity` minted a new record; (3) that re-upload's new approval listed the document twice under Approved. **Fixed:** `canon/promote.py`: approve and reject lock the item's row (`SELECT … FOR UPDATE`) before the claimed check: a second request waits, then gets 409 "item is approved". Reviewer page: buttons disabled while approving; afterwards the panel is the result (promoted counts, status, **Download FHIR bundle**), no Edit/Approve left. `extract/service.py`: a document extracted again keeps the patient it was filed under (`identity = "document"`); a different patient is an MPI merge. `GET /api/reviewer/approved`: one line per document, its latest approval. **New `scripts/merge_patients.py`** (owner chose: merge into the first record): dry run by default; `--apply` in one transaction removes named **approved** items with everything their approval wrote (foreign keys read from `pg_constraint`), re-points every reference to the dropped patient, deletes it, audits. | Local, real PostgreSQL: reviewer/review/extract/merge integration pass (new: concurrent approve waits and is refused with 1 bundle; same document keeps its patient, no new record; Approved lists a document once; merge moves the documents/facts, removes the extra approval's cn rows and bundle, keeps the other; refuses a non-approved item); identity + jobs 78 passed. |
