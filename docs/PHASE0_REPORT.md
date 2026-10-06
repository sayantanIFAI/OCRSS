# Phase 0 — Status Report

**Project:** CDI-Adapter (EMR_PROJECT1) · **Date:** 22 September 2026 · **Scope:** Phase 0 only
(Epic E0, "As-Built Baseline Rebuild", in `docs/backlog 1.md`)

---

## 1. What Phase 0 is

Both backlogs assumed the system already existed (code at commit `fdd4aeb`). That code was never
provided, and the project folder was empty. So Phase 0 was added first, with this goal:

> **Rebuild exactly what `docs/ARCHITECTURE.md` and `docs/TECH_STACK.md` describe as built:
> nothing more, nothing less.**

This is the working base that every later phase builds on: the upload → OCR → extract → validate →
review → FHIR pipeline, the web app and the pod scripts.

---

## 2. Summary

| Phase 0 blocks | ✅ Completed | ⛔ Blocked (built, waiting) | ⏳ Pending |
|---:|---:|---:|---:|
| **16** | **8** | **8** | **0** |

**All 16 blocks are built.**
- **8 are fully done:** built, tested and verified here.
- **8 are finished in code and tested here**, but their last test needs something this Windows
  machine cannot run:
  - **a real MinIO** (object storage). MinIO publishes no binaries any more, and a locally built
    one is blocked by Windows Smart App Control;
  - **Redis 7**, which has no Windows build;
  - or **the GPU pod**.

  All of them are planned to finish on the RunPod pod, through block E0-S16.

**Rule followed:** no stand-in servers. A fake S3 or fake Redis would pass the tests but prove
nothing, so those tests wait for the real thing (owner decision).

---

## 3. Status of every Phase 0 block

| # | Block | What it is | Status | What it is waiting on |
|---|---|---|---|---|
| 1 | E0-S1 | Project skeleton, settings, logging, CPU thread caps | ✅ **Completed** | — |
| 2 | E0-S2 | Database schema (36 tables, 5 views) + migrations + data access | ✅ **Completed** | — |
| 3 | E0-S3 | File storage + S1 ingest (PDF/image → pages) | ⛔ Blocked | real MinIO (4 tests) |
| 4 | E0-S4 | AI model client, JSON schemas, model gateway | ✅ **Completed** | (a real Qwen run is listed under E0-S16) |
| 5 | E0-S5 | S3 OCR (RapidOCR + AI transcription) | ⛔ Blocked | real MinIO (2 tests) |
| 6 | E0-S6 | S2 document classification | ⛔ Blocked | real MinIO (2 tests) |
| 7 | E0-S7 | Patient registry + master patient index (MPI) | ✅ **Completed** | — |
| 8 | E0-S8 | S4 fact extraction | ⛔ Blocked | real MinIO (1 test) |
| 9 | E0-S9 | S5 terminology (seed code map) | ✅ **Completed** | — |
| 10 | E0-S10 | S6 validation rules + governance gate | ✅ **Completed** | — |
| 11 | E0-S11 | S9 FHIR projection + ABDM conformance check | ⛔ Blocked | real MinIO (1 test) |
| 12 | E0-S12 | S8 human review console (`/review`) | ✅ **Completed** | — |
| 13 | E0-S13 | Web app, job model, upload page, `/healthz` | ⛔ Blocked | real MinIO (end-to-end job + page images) |
| 14 | E0-S14 | Canonical EMR, `/reviewer` workbasket, `/admin` | ✅ **Completed** | — |
| 15 | E0-S15 | Folder-watch path (Celery worker, watcher, ingest API) | ⛔ Blocked | Redis 7 + MinIO |
| 16 | E0-S16 | Pod scripts, Makefile, sample documents | ⛔ Blocked | the RunPod GPU pod |

---

## 4. How each block was built

The same method for every block:
1. Read the ARCHITECTURE section it cites.
2. Build only that.
3. Write unit tests.
4. Write integration tests on **real PostgreSQL 16** (running natively; no Docker).
5. Run the whole test suite, plus `ruff` and `mypy`.
6. Record the result in the backlog.

### ✅ Completed blocks

| Block | How it was built | Verified by |
|---|---|---|
| **E0-S1** Skeleton | `pyproject.toml`, one `Settings` class with every ARCHITECTURE §9 key, structured logging, CPU thread caps read from the container limit | 19 tests; all 24 §9 keys present |
| **E0-S2** Database | `db/schema.sql` (the 20 per-document tables of §4.1) + 5 views; Alembic migrations 0001–0004 (MPI, patient registry, canonical EMR); raw-SQL data layer | 12 unit + 7 integration tests on **real PostgreSQL 16.14**, incl. downgrade/upgrade round trip |
| **E0-S4** AI client + gateway | 8 JSON schemas; a client that extracts, repairs and validates model JSON (never loses a document to a formatting slip); gateway with `stub` and `hf` (Qwen2.5-VL) backends | 77 tests; gateway run as a real process |
| **E0-S7** Identity / MPI | Registry lookup (CareFlow id / ABHA / mobile), MPI match (ABHA, then fuzzy name + DOB), minted `CFP-YYYY-NNNNNN` ids, evidence merge that never overwrites a registered identity | 63 tests on real PostgreSQL |
| **E0-S9** Terminology | Seed map of 129 codes (SNOMED CT, LOINC, ICD-10, Indian brand names); exact → alias → fuzzy binding; unknown terms are never force-coded | 28 tests; all 14 worked-example codes of §8.2 reproduced exactly |
| **E0-S10** Validation gate | 6 clinical rules (ranges, units, dose ceilings, dates, evidence, terminology), confidence calibration, the §14 gate (auto-accept / audit / review), conflicts, review tasks | 49 unit + 6 integration tests; all 9 gate-table rows of §14 |
| **E0-S12** Review console | `/review` page: scan + highlighted box + fact + code + findings, then accept / correct / reject, with reviewer-signed provenance and audit | 10 tests on real PostgreSQL; a correction re-projects the FHIR live |
| **E0-S14** Canonical EMR | Review items → claim → approve → promotion into the canonical tables (`cn_*`) + AI-suggestion trail + canonical FHIR bundle; `/reviewer` workbasket, `/admin` inspector | 7 tests on real PostgreSQL; approval refused while any fact is still held |

### ⛔ Blocked blocks (built and tested here; last test needs MinIO / Redis / the pod)

| Block | How it was built | Tested here | Still to run |
|---|---|---|---|
| **E0-S3** Ingest | Storage layer (boto3 → MinIO), PDF/image/TIFF rendering, deskew + denoise, SHA-256 dedupe, pages + thumbnails | 31 tests (rendering, deskew accuracy, exact S3 request shapes) | 4 tests against real MinIO |
| **E0-S5** OCR | RapidOCR with confidence filter + reading order; AI transcription for handwriting | 15 unit tests (**real RapidOCR reads a generated page**) + 5 DB tests | 2 end-to-end tests |
| **E0-S6** Classify | Scored keyword heuristic (8 document types) with AI fallback for unclear pages | 13 unit tests + heuristic path on real PostgreSQL | 2 chain tests |
| **E0-S8** Extract | Prompts, per-type handlers, medication parsing (`1-0-1` → 2/day), evidence boxes, confidence, identity guard | 51 unit + 7 DB tests | 1 full-chain test |
| **E0-S11** FHIR | FHIR R4 builders for every resource, the ABDM artifact map, gated projection (only governed facts are shared), Python conformance check | 26 unit + 6 DB tests (S4 → S5 → S6 → S9) | 1 test (scan inlined from MinIO) |
| **E0-S13** Web app | All §7.1 endpoints, job orchestrator (phase 1 parallel, phase 2 serial), upload page, registry, `/healthz` | 6 unit + 11 DB tests; runs as a native process | end-to-end job + page-image endpoints |
| **E0-S15** Folder watch | Celery worker (6 tasks, retries), folder watcher, ingest API | 15 tests incl. a real file-system watcher | a real Redis round trip |
| **E0-S16** Pod ops | `bootstrap_pod.sh` (PostgreSQL restore, Redis, **MinIO built from source**, cron snapshots), `start_all.sh`, Makefile, sample-document generator | 15 tests (script syntax, persistence model, snapshots) | run on the pod; lift the MinIO/Redis/GPU tests |

### Reconciliation pass

After the first build, the owner asked that everything match ARCHITECTURE.md exactly. Blocks
E0-S8 and S9–S16 were revised to follow the document. Examples:
- one blood-pressure fact with FHIR components;
- the 700 KB inline-scan rule;
- only the §7.1 endpoints.

The list of changes is in `docs/BACKLOG_AS_BUILT.md` §5.

---

## 5. Test results for Phase 0

| Check | Result |
|---|---|
| Unit tests for the E0 code | ✅ all pass |
| Integration tests on real PostgreSQL 16.14 | ✅ all pass |
| Tests waiting for real MinIO / Redis | ⏸ **11 tests**, skipped on purpose (they run on the pod) |
| `ruff` lint + format, `mypy` types | ✅ clean |

*At the end of Phase 0: 491 tests passed and 11 were skipped. The project total is now higher,
because Phase 1 added more tests.*

---

## 6. Problems found and fixed during Phase 0

1. **Security:** the ingest step trusted the file type the client declared. A program could be
   uploaded as "PDF". The type now comes from the file bytes only, with 3 regression tests (E0-S15).
2. **`/healthz` hung for minutes** when storage was down. It now answers within seconds (E0-S13).
3. **Classification gap:** a real prescription scored 8, not the 9+ in §5. Fixed with an added
   signal (E0-S6).
4. **Wrong MinIO image tags:** the tags first written from memory did not exist. They were
   corrected to verified tags on quay.io (E0-S16).
5. **OCR on noisy scans** runs headings together ("VITALSIGNSCHART") and reads "SpO2" as "Sp02".
   The classifier now tolerates both (E0-S16).
6. **Flaky tests** that assumed one review task per document are fixed. The 5% audit sampling adds
   a second task (E0-S14).

**Environment:** PostgreSQL 16.14 runs natively (`scripts/dev/pg_local.ps1`). Windows Smart App
Control blocked 16.15 and was **never changed or bypassed**. Tools run as `python -m <tool>`.

---

## 7. What is needed to finish Phase 0

| Provide | Result |
|---|---|
| **The RunPod GPU pod** | E0-S16 runs `start_all.sh`, builds and starts MinIO and Redis, and runs one real Qwen2.5-VL request |
| Then, on the pod | the 11 waiting MinIO/Redis tests run, and **E0-S3, S5, S6, S8, S11, S13, S15 and S16 can move to Completed** |

After that pod run, all 16 Phase 0 blocks would be Completed.
