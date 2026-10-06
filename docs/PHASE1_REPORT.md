# Phase 1 — Status Report

**Project:** CDI-Adapter (EMR_PROJECT1) · **Date:** 22 September 2026 · **Scope:** Phase 1 only

---

## 1. Summary

Phase 1 has **28 blocks** across the two backlog files.

| Backlog file | Blocks | ✅ Completed | ⛔ Blocked (built, waiting) | ⏳ Pending (not started) |
|---|---:|---:|---:|---:|
| `backlog infra 1.md` | 12 | **3** | 9 | 0 |
| `backlog 1.md` | 16 | 0 | 2 | 14 |
| **Total** | **28** | **3** | **11** | **14** |

**What the statuses mean:**
- **✅ Completed:** built, tested and verified on this machine. Nothing left to do.
- **⛔ Blocked:** the code and tests are finished and pass here. The final check needs something this
  machine does not have (the GPU pod, a git host, a VM, or the owner's sign-off).
- **⏳ Pending:** not started yet.

**In short:**
- Every infra block of Phase 1 has been built: **3 are fully done, and 9 wait only on outside
  resources.**
- The feature blocks are next: **2 are built** (they were needed as dependencies), and 14 are not
  started.

---

## 2. Status of every Phase 1 block

### 2.1 `backlog infra 1.md` (12 blocks)

| # | Block | Status | What it is waiting on |
|---|---|---|---|
| 1 | E1-S8 Reconcile ARCHITECTURE.md with the mandate | ⛔ Blocked | Owner sign-off on ADR-0002 … ADR-0005 |
| 2 | E1-S1 CI: lint + unit tests | ⛔ Blocked | A git host (the code is kept in a folder only) |
| 3 | E1-S2 CI integration stage | ⛔ Blocked | A real MinIO (on the pod) and a git host |
| 4 | E1-S5 Secrets store + rotate the RunPod key | ⛔ Blocked | Owner rotates the RunPod key, plus one pod run |
| 5 | E1-S6 Golden-path regression suite | ⛔ Blocked | MinIO and the GPU pod |
| 6 | E1-S3 Fallback host outside RunPod | ⛔ Blocked | A non-RunPod Ubuntu 24.04 GPU host |
| 7 | E1-S4 Version-pinned pod bootstrap | ⛔ Blocked | Two fresh boots on the GPU pod |
| 8 | E1-S9 Stable counter address (ingress) | ⛔ Blocked | An India VM, a domain name, and the pod |
| 9 | E1-S7 Load test, 30 patients × 5–10 docs | ⛔ Blocked | A measured run against the pod |
| 10 | **E6-S5 IG version pin + upgrade process** | ✅ **Completed** | — |
| 11 | **E11-S1 Login and access control** | ✅ **Completed** | — |
| 12 | **E11-S4 De-identification** | ✅ **Completed** | — |

### 2.2 `backlog 1.md` (16 blocks)

| # | Block | Status | Note |
|---|---|---|---|
| 1 | E3-S1 SNOMED CT India + LOINC/ICD-10 in Postgres | ⏳ Pending | needs the licensed terminology releases from the owner |
| 2 | E3-S2 SapBERT/BioLORD candidate generation | ⏳ Pending | |
| 3 | E3-S3 Rule reranker + local-code fallback | ⏳ Pending | |
| 4 | E3-S4 FHIR `$validate-code` / `$translate` | ⏳ Pending | |
| 5 | E3-S5 Move binding to the new terminology service | ⏳ Pending | |
| 6 | E4-S5 DB rule: no evidence ⇒ never auto-accepted | ⏳ Pending | |
| 7 | E4-S4 Grounding verifier before auto-accept | ⏳ Pending | |
| 8 | E6-S1 HL7 validator + pinned ABDM IG | ⛔ Blocked | built for E6-S5; needs one pod run |
| 9 | E6-S2 Make S9 use the real validator | ⏳ Pending | |
| 10 | E6-S3 ABHA identifier test suite | ⏳ Pending | |
| 11 | E6-S4 Golden FHIR bundles | ⛔ Blocked | built for E6-S5; the bundles still have 120 IG errors (see §5) |
| 12 | E6-S6 Patient identity source of truth before FHIR | ⏳ Pending | |
| 13 | E6-S7 Composition sections against the live profile | ⏳ Pending | |
| 14 | E12-S1 Workflow orchestrator with bounded agent tools | ⏳ Pending | |
| 15 | E12-S2 Terminology tool-calling for the extraction agent | ⏳ Pending | |
| 16 | E12-S3 Grounding / critic agent | ⏳ Pending | |

---

## 3. How each block was built

The same rules applied to every block:
- Build **exactly** what `docs/ARCHITECTURE.md` describes.
- **Native processes, no Docker.**
- **Never a stand-in** for a real component: no fake MinIO or Redis, for example.
- Pin every version and verify it by checksum.
- Test first on this machine.
- If the final check needs something that isn't here, mark the block **Blocked**, not Completed.

### 3.1 Completed blocks

#### E6-S5 — IG version pin + upgrade process ✅
- **Problem found first:** the IG package name in the config, `nrces.fhir.r4.ndhm`, does not exist
  in the FHIR registry. The real NRCeS ABDM IG is **`ndhm.in#6.5.0`**. The old code also dropped
  the version number. Both are fixed.
- **Built:**
  - The IG, the HL7 validator (6.10.4) and all 10 packages the validator uses are pinned with
    checksums (`infra/fhir/packages.lock`).
  - Validation runs fully offline.
  - `scripts/fhir_ig.py` does **stage → check → promote**. A new IG version is tested in a separate
    folder against the golden bundles, and it can only be promoted if it adds no new errors.
  - Runbook: `docs/ig-upgrade.md`.
- **Verified with the real validator:**
  - Staging the current version **passed** and would change nothing.
  - Staging an older version **failed** (51 new errors), and promotion was **refused**.

#### E11-S1 — Login and access control ✅
- **Built:**
  - Staff accounts with three roles: `intake` (counter), `reviewer` (clinicians) and `admin`.
  - Every one of the 31 routes has an explicit rule. A route without a rule is refused.
  - Passwords are stored as salted hashes; the account locks after 5 wrong tries.
  - Sessions expire after 30 minutes idle or 12 hours at most.
  - Cross-site requests are refused, and every sign-in and sign-out is audited.
  - Account management: `python -m cdi_adapter.webapp.users`. Runbook: `docs/auth.md`.
- **Verified:**
  - 38 unit tests and 28 tests on real PostgreSQL pass.
  - 15 live checks against the running app with `curl` were all correct.

#### E11-S4 — De-identification ✅
- **Built:**
  - Presidio + spaCy + the clinical de-identification model `obi/deid_roberta_i2b2`, pinned.
  - Indian recognizers: ABHA, Aadhaar (checksum), PAN, phone, PIN, UHID/MRN and address.
  - Names get consistent fake names, dates are shifted, and IDs become labels.
  - Scanned pages are blacked out, **re-read, and cleaned again**.
  - Runbook: `docs/deid.md`.
- **Verified by a red-team attack:** an attacker who knows the real identities searched the
  output, including the re-read images.
  - Final run: **96 documents across 4 samples, 0 leaks.**
  - The same attack on the originals always found the identifiers, which proves it works.
  - Earlier runs found 15 real leaks. All were fixed, and each has its own test.

### 3.2 Blocked blocks (built and tested, waiting on outside resources)

| Block | How it was built | Tested here |
|---|---|---|
| E1-S8 | Re-scored the ⛔ HOLD blocks; recorded the mandate decisions as ADR-0002 … 0005 | Documents updated |
| E1-S1 | `scripts/ci.py` gate (lint, format, types, secret scan, unit) + GitHub workflow | Gate runs **GREEN** here |
| E1-S2 | Integration stage with real PostgreSQL + real MinIO + stub model | PostgreSQL part passes; MinIO part needs the pod |
| E1-S5 | File-based secrets store (0700/0600), no passwords on command lines, secret scan | Unit + misuse tests pass |
| E1-S6 | Golden-path run on a fixed sample patient with drift alerts | Passes with the stub model on PostgreSQL |
| E1-S3 | The same pod scripts run on any Ubuntu 24.04 GPU host (native, no Docker) | Script tests pass |
| E1-S4 | Every package pinned (Ubuntu snapshot, pip hash lock, Go and MinIO checksums) + a drift check that stops the boot | Both locks reproduce exactly; all wheels hash-verified |
| E1-S9 | Caddy with HTTPS on an India VM + a reverse SSH tunnel from the pod; real 503 when down | 33 tests (tunnel script run for real) |
| E1-S7 | Locust load test: 30 patients × 5–10 documents, exact p50/p95/p99 against the 5-minute SLA | Ran the full burst against the real app (it stopped at MinIO) |
| E6-S1 | Java 21 + HL7 validator + pinned IG, offline; the pod bootstrap installs and proves them | Real validator runs here |
| E6-S4 | 5 golden FHIR bundles from the real builder code + a no-new-errors check | Checked by the real validator |

---

## 4. Test results (latest full run)

| Check | Result |
|---|---|
| Lint · format · type check · secret scan | ✅ all pass |
| Unit tests | ✅ **643 passed**, 4 skipped (opt-in model/validator tests and a Linux-only test) |
| Integration tests on real PostgreSQL 16 | ✅ **121 passed**, 11 skipped (exactly the tests that need MinIO) |
| CI gate (`python scripts/ci.py`) | ✅ **GREEN** |
| Real HL7 validator on the golden bundles | ✅ no new errors |
| De-identification red team | ✅ 96 documents, 0 leaks |

---

## 5. Important findings during Phase 1

1. **The ABDM IG package name was wrong everywhere.** `nrces.fhir.r4.ndhm` is not a registry id;
   the correct one is `ndhm.in`. Fixed.
2. **The FHIR output is not yet ABDM-conformant.** The official validator reports 120 errors across
   the 5 golden bundles. The old built-in check had called them valid. Fixing them belongs to
   E6-S7, E6-S6, E6-S2 and E8-S1. They are tracked as a baseline that may only shrink.
3. **De-identification needs its own red-team test.** The first attempts leaked dates of birth,
   phone numbers and names, because OCR joins words and drops lines. The self-checking image step
   fixed this.
4. **Windows Smart App Control** blocks some newly compiled libraries on this machine. The
   workarounds stay inside `.venv` only, and Smart App Control itself was never changed.

---

## 6. What is needed to finish Phase 1

| Provide | Unblocks |
|---|---|
| **The GPU pod**, with the RunPod API key rotated first | E1-S4, E1-S5, E1-S6, E1-S7, E6-S1, and the MinIO part of E1-S2 |
| **A small India VM + a domain name** | E1-S9 |
| **Sign-off on ADR-0002 … ADR-0005** | E1-S8 |
| **A git host** | E1-S1 and E1-S2 |
| **A non-RunPod Ubuntu GPU host** | E1-S3 |
| **Licensed SNOMED CT India / LOINC / ICD-10 releases** | E3-S1 (feature file) |

After that, the 14 pending feature blocks in `backlog 1.md` are next: terminology, FHIR
correctness and the agent layer.
