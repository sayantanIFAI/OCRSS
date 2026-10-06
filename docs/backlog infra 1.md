# BACKLOG — CDI Adapter: INFRASTRUCTURE, NON-FUNCTIONAL & PRODUCTION-READINESS STORIES

> **Source of truth:** `CDI-Adapter-Production-Backlog-Enhanced.xlsx` (91 stories), restated as
> **Epic → Phase → Block → Task**, then extended with the blocks marked ➕ (production review
> 2026-09-20, plus the target-architecture additions of the same day). One Excel story = one block.
> **Companion file:** `backlog 1.md` (feature stories). Nothing is duplicated between the two; the split rule is in §0.6.
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

Status values, the source-file verification findings, the owner decisions and the environment are
recorded once in `backlog 1.md` §B (not duplicated here). Summary: every block carries a
`**Status:**` line (`Pending` · `In Progress` · `Completed` · `Blocked`); E2-S1/S2/S3 were reset from
✅ to `Pending` because the code they cite was not provided; **Phase 0 (Epic E0, in `backlog 1.md`)
runs before Phase 1**; E1-S8 is the first Phase 1 block.

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
| Epics / blocks | 9 / **54** |
| Done / In Progress / Backlog | 3 / 0 / 51 |
| Total effort | **139.5 person-days · 233 points** |
| Must-only effort | **34 blocks · 90.5 person-days** |
| Go-live blockers remaining (Must, not Done) | **32** |
| Blocks on ⛔ HOLD (not in ARCHITECTURE.md) | **0** (14 lifted by E1-S8, 2026-09-21) |
| Calendar envelope, this file alone @0.7 focus | **301 days** |
| **Combined plan (both files, one engineer)** | **103 blocks · 296 person-days · 468 points · 616 calendar days · 53 blockers** |

**Decisions applied in this revision**

- **GPU:** production baseline is the **Blackwell 32 GB** running the current Qwen2.5-VL-7B (bf16). No T4, no quantization needed to start; quantization (E2-S6) and a cheaper GPU class (E13-S2) become optional later savings.
- **Budget:** **₹75,000/month** for Voice + CDI combined (envelope and sensitivity in `backlog infra 1.md` §2.2).
- **Reviewers:** a small reviewer pool is acceptable. The Sizing sheet's reviewer gate is reported, not blocking; unreviewed facts stay held with an ETA.
- **Resequenced** (§3.1): E11-S1, E11-S4 → Phase 1; E10-S7 → Phase 6; E6-S8 → Phase 3; E4-S6 → Phase 9; E13-S5 → Phase 7. No dependency now points at a later phase.
- **Open decisions (🔒):** which store is the system of record for governed facts (E15-S2); WhatsApp Business approval (E15-S1); India-region provider + residency sign-off (E1-S10); Voice-project sign-off on the two-GPU topology (E13-S6).

### 1.1 Findings carried over from checking the Excel against this repo

1. **Story count mismatch in the Excel.** The *Backlog* sheet title and *Phase Plan* note say "84 stories"; the sheet has 91.
2. **Stale Sizing-tab row references.** E2-S6 says "rows 26/27", E13-S1 "rows 26–28"; the GPU strategy rows are now **30–32**.
3. **E1-S3 premise confirmed:** `infra/compose/docker-compose.yml` has no `webapp` or `mlserve` service.
4. **Done confirmed in code:** `VLLMBackend` + XGrammar (E2-S1/S3) and `extract_concurrency` (E2-S2). vLLM gave no throughput gain and is dormant; `hf` stays the active backend.
5. **Two workbooks are in circulation.** The `D:\Agentic AI\CDI Adapter\` copy has 87 stories; the current one has 92 (adds E13-S6 as "shared GPU", now superseded here by the two-GPU topology). Sync them.

### 1.2 Blocks added beyond the Excel that belong to this file

| Block | Phase | Why it isn't already covered |
|---|---|---|
| **E1-S8** Reconcile ARCHITECTURE.md ➕ | 1 | 30 blocks sit on ⛔ HOLD because the repo's ARCHITECTURE.md lacks §4.5, the 30-patient load figure, the agentic control plane and the cost policy. Nothing in the file owns fixing that — and it gates Phase 1, all of E12/E13, and E14-S5, the final gate. |
| **E1-S9** Stable ingress / DNS ➕ | 1 | Risk R7 names it as a mitigation; no block delivers it. ARCHITECTURE §3.3 is explicit that RunPod reassigns IP/port on every restart. A billing counter cannot have the URL change. |
| **E9-S8** Circuit breakers + governed degradation ➕ | 4 | E9-S3 handles a poison *document*; E3-S5/E8-S4 handle two dependencies individually. No shared breaker, timeout budget or bulkhead, and no rule that a degraded mode may not widen auto-accept. |
| **E10-S10** Credential rotation drill ➕ | 7 | E1-S5 rotates once, E11-S3 rotates KMS keys. Nothing proves a live DB/MinIO credential can be rotated without dropping in-flight jobs. |
| **E10-S11** Restore / DR drill ➕ | 7 | E10-S6 *promises* RPO < 1 min; nothing ever restores anything. ARCHITECTURE §3.3's whole persistence model is `pg_restore` from a 15-minute dump, and E14-S5's failure runs omit data-tier loss. |
| **E11-S7** Retention, crypto-shredding & tumbling ➕ | 6 | E11-S5 states the retention/erasure *policy*. Its execution collides with E10-S7 (WORM rejects deletes), §4.3 provenance, §4.4 `bundle_hash`, scans base64-inlined into bundles by §5 S9, and anything already pushed by E8-S4. No block resolves that. |
| **E1-S10** India-resident production hosting ➕ | 4 | RunPod is the pilot; both RFQs and DPDP require a host with documented India residency, and no block delivered one. |
| **E13-S6** Two-GPU topology + ₹75k guard ➕ | 5 | The target diagram fixes the GPU layout (protected Voice GPU, escalation-only CDI GPU); nothing enforced isolation or tracked the budget. |

**Reframed rather than adopted as proposed:** *secret hot-reload* → **E10-S10 as a rotation drill.** Once E9-S1 (durable queue) and E10-S9 (multi-instance) land, a rolling restart gives zero-downtime rotation with no hot-reload machinery. Build hot-reload only if the drill misses the SLA window.

---

## 2. Capacity, cost and risk

Numbers are labelled **MEASURED / ESTIMATE / ASSUMPTION**. The rule: *don't ship from an ESTIMATE*.

### 2.1 Demand and SLA

| Input | Value | Type | Measured by |
|---|---|---|---|
| Concurrent patients | 30 | ASSUMPTION | E1-S7 / E14-S5 |
| Documents per patient | 5–10 (design burst 10) | REQUIREMENT | E1-S7 / E14-S5 |
| Design peak documents | **300** (225 average is for economics only) | FORMULA | — |
| SLA: last upload → governed JSON/FHIR **or explicit hold with ETA** | 5 min | ASSUMPTION | E10-S8 / E14-S5 |
| Max planned utilization | 0.7 | ASSUMPTION | E9-S7 / E14-S5 |
| Measured extract time, current 7B on Blackwell 32 GB (HF backend, one pass at a time) | ≈ 14–29 s/doc, 74.2 s for a 3-doc job | **MEASURED** (ARCHITECTURE §7.3) | — |
| GPU escalation fraction | 0.3 | ESTIMATE — today every document uses the GPU | E2-S9 |
| CPU docs/min/worker · workers | 8 · 12 → 11 needed | ESTIMATE | E2-S9 / E9-S2 / E9-S5 |
| Warm GPU floor in OPD | 1 per GPU role | ASSUMPTION | E9-S7 / E13-S6 |
| Off-hours | Scale the CDI GPU to zero; pre-warm before opening | POLICY | E9-S7 / E13-S3 |
| Autoscale signal | Oldest queue age + predicted remaining GPU-seconds | POLICY | E9-S7 |
| Reviewers available · needed at target | 3 · 12 | ASSUMPTION / FORMULA — **informational, not gating** (decision 2026-09-20) | E14-S5 |

> **Honest limit on the 5-minute target.** On one CDI Blackwell GPU with the current 7B, extraction is
> ~21 s/doc serial (HF). A full 300-document burst is therefore ~100 minutes if every document reaches
> the GPU. The 5-minute target is only reachable by (a) E2-S9 sending only gate-1 failures to the GPU,
> (b) batching, and/or (c) reading the SLA as *machine-ready results plus an explicit hold with an ETA*,
> which is what E7-S7 already specifies. E13-S1 replaces these with measured numbers.

### 2.2 Cost envelope — ₹75,000/month, Voice + CDI combined, Blackwell 32 GB

**ASSUMPTION:** GPU rate **₹75/hour** (no quote yet — enter the real rate; sensitivity below). Non-GPU lines are ESTIMATES from Azure India-region price patterns, not quotes. Carrier SIP-trunk minutes are out of scope.

| Line | Hours/month | ₹/month |
|---|--:|--:|
| Voice GPU (Blackwell), 12h/day | 360 | 27,000 |
| CDI GPU (Blackwell) — **scenario A**: escalation-only, ~6h/day (after E2-S9) | 180 | 13,500 |
| CDI GPU (Blackwell) — **scenario B**: every document reaches it, 12h/day (**today**) | 360 | 27,000 |
| CPU workers (base + burst) for preprocess / OCR / extract | — | 9,240 |
| Voice/API control plane (2 small VMs, HA) | — | 2,160 |
| Managed PostgreSQL (single-AZ budget tier, PITR) | — | 1,200 |
| Object storage (starting 500 GB) | — | 750 |
| Queue | — | 300 |
| Container/model registry | — | 1,700 |
| Load balancer + static IP + telephony edge | — | 2,000 |
| Network / bandwidth | — | 2,500 |
| Monitoring / security / support (basic tier) | — | 3,000 |
| **Non-GPU subtotal** | | **22,850** |
| **Total, scenario A** | | **63,350** — fits, ₹11,650 headroom |
| **Total, scenario B** | | **76,850** — over by ₹1,850 |

**Sensitivity:** the envelope holds up to about **₹97/hr** in scenario A and **₹72/hr** in scenario B. A burst GPU beyond the two above is not included.
**Reading it:** until E2-S9 lands the system runs scenario B, which sits right at the ceiling. E2-S9 (CPU-first routing) is what moves it to scenario A, so it is also the main cost lever.

**Cost order of operations:** (1) CPU-first routing (E2-S9); (2) schedule GPUs to clinic hours (E13-S3); (3) batching / continuous batching where it measurably helps (E2-S8; the vLLM trial showed no gain); (4) quantization / smaller GPU only if headroom is needed (E2-S6, E13-S2); (5) interruptible capacity for overflow only after failure tests (E13-S4).

### 2.3 Risk register

| ID | Risk | Score | Mitigation | Epics |
|---|---|---|---|---|
| R1 | vLLM / GPU serving build risk | 12 | `hf` is the active backend; benchmark before relying on any vLLM number | E2, E13 |
| R2 | Hallucination survives schema constraints | 20 | Evidence binding + S6 gate + grounding critic (CPU by default) | E2, E4, E12 |
| R3 | Single-node / durability failure | 15 | Durable storage, idempotent queueing, HA/DR, restore drill | E9, E10 |
| R4 | Gold-set coverage insufficient | 12 | Balanced gold set, shadow samples, regression gates, replenishment | E2, E14 |
| R5 | Calibration untrusted without adjudicated data | 20 | Fit only on clinician-adjudicated data | E4, E14 |
| R6 | DPDP / ABDM compliance needs external authority | 15 | Explicit legal / ABDM sign-offs | E8, E11 |
| R7 | Endpoint / infra instability | 9 | Stable ingress/DNS (E1-S9), India-region host (E1-S10), health checks | E1, E10 |
| R8 | One engineer can't compress external dependencies | 15 | External sign-offs exposed per block | All |
| R9 | CPU-first router under-escalates | 15 | Route by evidence/confidence; per-doc_type non-inferiority | E2, E4 |
| R10 | Few reviewers → many facts held at the counter | 12 (was 20) | **Accepted** (2026-09-20): facts stay held with an ETA; never auto-accept to save time | E4, E7 |
| R11 | Cold start / preemption breaches SLA | 12 | Warm on-demand floor in OPD; interruptible = overflow only | E9, E13, E14 |
| R12 | Voice and CDI contend for one host resource | 12 | Hard isolation: separate GPUs (E13-S6), combined-load test | E13 |

---

## 3. Build order

| Phase | Name | Epics | Blocks | Person-days | Calendar days (@0.7 focus) |
|---|---|---|---|---|---|
| 1 | Foundation, CI, Patient Identity, FHIR & Core No-Guess Safety | E1, E6, E11 | 12 | 22.5 | 49 |
| 2 | Extraction Acceleration + CPU-First Selective-GPU Routing | E2 | 5 | 13 | 28 |
| 3 | Human-in-the-Loop / Counter Billing Contract | E7 | 1 | 2 | 7 |
| 4 | Scale, Queueing, Deadline Scheduling, Autoscaling & Production Hosting | E1, E9 | 9 | 27 | 56 |
| 5 | Cost Optimization / Two-GPU Topology | E13 | 5 | 13 | 28 |
| 6 | Security, AuthN/AuthZ & DPDP Compliance | E10, E11 | 6 | 16 | 35 |
| 7 | Observability, HA/DR, SLOs & Agent Trace | E10, E13 | 11 | 28 | 56 |
| 8 | ABDM Gateway, Clinic Integration & Longitudinal Reconciliation | — | 0 | 0 | 0 |
| 9 | Calibration, Shadow Pilot, 30×10 Soak & Cutover | E14 | 5 | 18 | 42 |
| | **Total** | | **54** | **139.5** | **301** |

> Calendar days use the Excel's own formula: `ROUNDUP((person-days ÷ 0.7) ÷ 5) × 7`, applied per phase to *this file's* blocks. Combined across both files: **616 days**.

**Block sequence inside each phase** (respects dependencies; ✅ done, 🔄 in progress, ➕ added, ⛔ HOLD):

- **P1:** E1-S8➕ → E1-S1 → E1-S2 → E1-S5 → E1-S6 → E1-S3 → E1-S4 → E1-S9➕ → E1-S7 → E6-S5 → E11-S1 → E11-S4
- **P2:** E2-S1✅ → E2-S2✅ → E2-S3✅ → E2-S6 → E2-S8
- **P3:** E7-S1
- **P4:** E9-S1 → E9-S2 → E9-S3 → E9-S8➕ → E9-S4 → E9-S5 → E9-S6 → E9-S7 → E1-S10➕
- **P5:** E13-S1 → E13-S2 → E13-S3 → E13-S4 → E13-S6➕
- **P6:** E10-S7 → E11-S6 → E11-S2 → E11-S3 → E11-S5 → E11-S7➕
- **P7:** E10-S1 → E10-S2 → E10-S3 → E10-S4 → E10-S5 → E10-S6 → E10-S11➕ → E10-S9 → E10-S10➕ → E10-S8 → E13-S5
- **P9:** E14-S5 → E14-S1 → E14-S2 → E14-S3 → E14-S4

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

# EPIC E1 — Foundation & Engineering Baseline

### Phase 1

#### Block E1-S8 — Reconcile ARCHITECTURE.md with what is actually mandated ➕ ⬜
**Status:** Blocked — every task is done; DoD-LEGAL needs the product owner's sign-off on ADR-0002…0005 (see §9, 2026-09-21)
`Must · BLOCKER · 1.5 d · 3 pts · Deps: — · Arch ◐ (closing this gap is the block's whole purpose) · DoD-LEGAL`
**Run this first.** 30 blocks sit on ⛔ HOLD purely because `docs/ARCHITECTURE.md` doesn't cover
what the Excel mandates. Until it does, Phase 1 can't close, all of E12 and E13 stay frozen, and
E14-S5 — the final go-live gate — is itself on HOLD.
- [x] Add the canonical-EMR layer to `docs/ARCHITECTURE.md` (the §4.5 equivalent: `cn_*`, `ops_*`/`fin_*`, `ai_*`, `rv_*`, the promotion path, `/reviewer`, `/reviewer/c360`, `/admin`) — re-derived from a live `pg_dump --schema-only`, not copied from the source document's own caveated note
- [x] Reconcile §4.1–§4.4 with it so the per-document and longitudinal models are both authoritative
- [x] Record a decision — accept into the architecture, or reject with an ADR — for every mandate the document doesn't currently carry: the 30 × 10 / 5-minute load requirement, the agentic control plane (E12), the cost/GPU-class policy (E13), and the counter/billing contract (E7-S6/S7)
- [x] Re-mark every ⛔ HOLD block ✔ / ◐ / ✖ against the updated document and lift the ones now backed
- [x] Schedule the endpoint/schema reconciliation pass the source architecture document asks for, for the next time a pod is live
- **Tests:** none (documentation + decisions). DoD is a reviewed PR with the HOLD list re-scored and every rejected mandate carrying an ADR

#### Block E1-S1 — Stand up CI (lint + unit tests) ⬜
**Status:** Blocked — the gate and workflow are built and proven locally; merge-blocking, the badge and a CI run on a real PR need a git host (owner: "save the code in folder only", 2026-09-21). Steps in `docs/ci.md`.
`Must · BLOCKER · 1 d · 2 pts · Deps: — · Arch ◐ §11 Testing · DoD-CODE`
Run the existing ~32 unit tests and linting on every push; block merge on red.
- [x] GitHub Actions (or equivalent) runs `pytest -m 'not integration'` + a linter on every PR — `.github/workflows/ci.yml` → `scripts/ci.py` (lint, format, mypy, unit); verified locally, not yet on a host
- [ ] Branch protection: a PR can't merge while CI is red — 🔒 needs the git host (`docs/ci.md` step 2)
- [ ] CI badge in `README.md` — 🔒 needs the repository URL (placeholder comment in README)
- **Tests:** CI green on a PR; a deliberately failing test blocks the merge

#### Block E1-S2 — CI integration-test stage (Postgres + MinIO + stub VLM) ⬜
**Status:** Blocked — built; the Postgres half is verified locally (88 passed), but the 11 MinIO tests and the pipeline audit need a real MinIO (not runnable on this Windows box: Smart App Control; no substitutes) → run on the pod or a CI host. Also depends on E1-S1's git host.
`Must · BLOCKER · 1 d · 2 pts · Deps: E1-S1 · Arch ◐ §11 · DoD-CODE`
Wire the existing `-m integration` tests into CI using service containers.
- [x] CI job starts Postgres + MinIO service containers — as **native processes**, not containers (owner: "no docker"; ARCHITECTURE §3.3): `integration-gate` job installs PostgreSQL 16 from apt and builds MinIO from the pinned tag, same as `bootstrap_pod.sh`
- [x] Runs the integration tests against `StubMLClient` — `ci.py --suite integration` (`CDI_MLSERVE_BACKEND=stub`); `CDI_TEST_REQUIRE_INFRA=1` makes missing Postgres/MinIO **fail** instead of skip
- [x] Fails the build on any `pipeline_run` stage ≠ `ok` — `scripts/ci_pipeline_audit.py` (§8.2 samples S1→S9; also fails on a missing stage; audits only its own documents)
- **Tests:** `pytest -m integration` green in CI

#### Block E1-S5 — Secrets management + rotate the exposed RunPod API key ⬜ 🔒
**Status:** Blocked — store, pod wiring, secret scan and misuse tests are built and verified locally; still needs (a) 🔒 the RunPod account owner to rotate `RUNPOD_API_KEY` and (b) the pod run of the new `bootstrap_pod.sh` step 0 (Redis `requirepass`, MinIO root-credential switch, `/proc` check). Runbook: `docs/secrets.md`.
`Must · BLOCKER · 1.5 d · 3 pts · Deps: — · Arch ✔ §12 Secret hygiene · DoD-SEC`
Move all credentials (DB/MinIO/Redis/RunPod) out of plaintext `.env` into a vault.
- [x] Move the credentials into a secrets store — `cdi_adapter.secretstore` (0700 dir / 0600 files, via pydantic-settings `secrets_dir`; `CDI_SECRETS_DIR`); no vault product is named in ARCHITECTURE/TECH_STACK, so no new server. `.env.runpod` carries no credential; pod scripts read the store
- [ ] Confirm no credential is in plaintext in the repo, in a committed `.env`, or in a world-readable `/proc` path — repo ✅ and `.env` ✅ (`scripts/secret_scan.py` CLEAN, now a CI stage); `/proc`: no password on any command line by design (pgpass, `requirepass` file, SQL on stdin) — **pod check pending**
- [ ] Rotate the flagged `RUNPOD_API_KEY` — 🔒 owner action; steps in `docs/secrets.md`
- **Tests:** secret scan of the repo + git history is clean; a documented misuse test
- 🔒 Key rotation needs the RunPod account owner

#### Block E1-S6 — Golden-path regression suite ⬜
**Status:** Blocked — the golden path is built and verified on PostgreSQL with the stub (S2→S9); the full S1→S9 CI run needs a real MinIO, and the nightly real-model run + drift alert need the GPU pod. **Owner note:** the stub baseline is 21 facts, not the 15 written here — see the task below and ARCHITECTURE §8.2 note.
`Must · BLOCKER · 2 d · 3 pts · Deps: E1-S2 · Arch ◐ §8.2 Worked example · DoD-CODE`
Script the 3-document "Anjali Das" example (§8.2) end to end.
- [x] CI run against the stub backend asserts **15 `clinical_fact` rows, 15 `fact_provenance` rows, 3 bundles, 0 structural errors** — asserts the stub baseline in `tests/golden/anjali_das.json`: **21 / 21 / 3 bundles / 0 structural errors** (+ 37 resources, artifacts, `ready_to_share`). 15 is the §8.2 run of the real 7B, which read only the allergy off the vitals sheet (§13 weak spot); the stub follows §5 S4 and emits the 6 numeric vitals too. The 15 is kept as the nightly `real_vlm` baseline. Seeded mode verified locally; full mode is CI stage `golden` (needs MinIO)
- [ ] Nightly run against the real VLM — built: `infra/runpod/golden_nightly.sh` + cron 02:30 in `bootstrap_pod.sh` (fresh `cdi_golden` DB, `cdi-golden` bucket, skips unless mlserve is the loaded `hf` backend); **not run: needs the GPU pod**
- [ ] Alert on any count drift in the nightly run — built: exit 1 + a line in `/workspace/logs/golden_alerts.log` + `golden_last.json` (unit-tested); a real alert route is E10; **not run nightly yet**
- **Tests:** the golden-path test itself, in CI and nightly

#### Block E1-S3 — Containerize app + mlserve for a non-RunPod fallback host ⬜
**Status:** Blocked — built as a **native** fallback (owner decision 2026-09-21: "based on the architecture, don't use docker"; ARCHITECTURE §3.3): the pod scripts now run on any Ubuntu 24.04 GPU host. Needs a non-RunPod Ubuntu host to run `start_all.sh` + the golden path. Runbook: `docs/fallback-host.md`.
`Should · HARDEN · 3 d · 5 pts · Deps: E1-S2 · Arch ◐ §1, §3.3, Appendix A · DoD-CODE`
Production-grade means the system can run somewhere else if this pod dies.
- [x] Add `webapp` and `mlserve` services to `infra/compose/` (currently missing) — **replaced by owner decision (no Docker):** webapp + mlserve are started natively by `start_all.sh` / `start_mlserve.sh` on the fallback host, as on the pod; the RunPod-only parts are removed (torch 2.8.0+cu128 installed when not inherited, host-aware address in `infra/runpod/lib.sh`)
- [ ] `docker-compose up` brings up webapp + mlserve + Postgres + MinIO + Redis on a non-RunPod host — **as `sudo bash infra/runpod/start_all.sh`** (no Docker); built, **not run: needs a non-RunPod Ubuntu 24.04 host**
- [ ] Golden-path suite (E1-S6) passes against that stack — step 5 of `docs/fallback-host.md`; **needs that host**
- [x] *(from E1-S5)* the compose stack reads credentials from the secrets store instead of the dev defaults, and `infra/compose/` is removed from `DEV_SCOPED` in `scripts/secret_scan.py` — **superseded:** compose is not a deployment path (owner decision), so the native fallback reads the secrets store through the same `bootstrap_pod.sh` step 0; `infra/compose/` stays dev-only in the scan (comment updated)
- **Tests:** E1-S6 against the compose stack

#### Block E1-S4 — Idempotent, version-pinned pod bootstrap (IaC) ⬜
**Status:** Blocked (2026-09-22). Pinning and drift detection are built and verified locally. Every locked wheel was downloaded for the Linux target and hash-checked. The acceptance run (two fresh-pod boots, manifests compared) needs the **RunPod pod** (or two fresh Ubuntu 24.04 hosts), which this build cannot reach. Runbook: `infra/runpod/README.md` "Pinned versions and drift detection".
`Should · HARDEN · 2 d · 3 pts · Deps: — · Arch ◐ §3.3, §10.1/§10.2 · DoD-DEPLOY`
Harden `start_all.sh` / `bootstrap_pod.sh` with pinned package versions and drift detection.
- [x] Pin package versions in the bootstrap. `infra/runpod/versions.env` is the single place for pins:
  - Ubuntu archive snapshot `20260920T000000Z` plus the exact apt versions of all 9 installed packages
  - Python 3.12, Go 1.27.1 plus go.dev's sha256 (now mandatory), MinIO tag plus commit `9e49d5e7`
  - torch 2.8.0+cu128, torchvision 0.23.0+cu128, transformers 5.16.1 (App. B)

  `infra/runpod/requirements.lock` holds 97 Python packages with exact versions and wheel sha256, generated by `scripts/lock_pod_deps.py` (`make lock`). The bootstrap now:
  - installs apt packages only from the snapshot
  - installs pip packages only with `--require-hashes --no-deps`, so no resolver runs on the pod
  - sources the pins after `.env`, so the environment cannot override them
  - requires the MinIO tag to resolve to the pinned commit
  - builds with `GOTOOLCHAIN=local -mod=readonly -trimpath`
- [x] Add drift detection. `src/cdi_adapter/podmanifest.py` (stdlib) runs as bootstrap step 7:
  - It records the dpkg packages, Python distributions (venv and inherited), Go, the MinIO binary sha256 and the cached model revisions to `/workspace/logs/manifests/`.
  - A pin that does not hold prints `DRIFT:`, adds a line to `drift.log` and exits 1, so the web app never starts. `CDI_ALLOW_DRIFT=1` is the logged operator override.
  - A change since the previous boot that no pin covers prints a warning and the boot continues.
  - `podmanifest compare A B` is the two-boot check.
- [ ] A new pod reaches "LIVE URL printed" unattended from a clean checkout, **twice in a row, with identical versions**. Built, **not run: needs the pod.** The steps are in the runbook "Acceptance" section.
- **Tests:** two consecutive fresh-pod boots; version manifests compared. Locally: `tests/test_bootstrap_pins_unit.py`, and the `compare` CLI on two real manifests from this machine prints "identical versions". **Owner / pod:** run the runbook's four acceptance steps and return both `last.json` files.

#### Block E1-S9 — Stable ingress / DNS for the counter workflow ➕ ⬜
**Status:** Blocked (2026-09-22). Built and verified locally. The acceptance run needs an **ingress VM in India, a domain name and the pod**, none of which this build can reach. Design, chosen from the architecture (owner delegated the choice): Caddy on our own India-region VM, reached over a reverse SSH tunnel that the pod opens. TLS and patient data stay on hosts we run (§12, E1-S10 residency); native processes, no Docker; RunPod needs no inbound port. Runbook: `docs/ingress.md`.
`Must · BLOCKER · 1 d · 2 pts · Deps: E1-S3 · Arch ✔ §3.3 ("RunPod reassigns IP/port on every restart"); Risk R7 · DoD-DEPLOY`
R7 names stable ingress as a mitigation, but no block delivers it. A billing counter cannot have
its URL change every time the pod restarts.
- [ ] A stable hostname fronts the web app; replacing the pod/host does not change the URL staff use. **Built, not run (needs the VM + domain + pod):**
  - `infra/ingress/Caddyfile` serves `{$CDI_INGRESS_HOSTNAME}` and forwards to the tunnel port.
  - `infra/runpod/start_tunnel.sh` keeps an autossh reverse tunnel open. It is idempotent, pins the VM's host key, and uses a persistent key in `/workspace/secrets/tunnel/`. `start_all.sh` runs it.
  - A new pod or the E1-S3 host opens the same tunnel, and a dead pod's forward is dropped within about 45 s.
  - `infra/ingress/setup_ingress_vm.sh` sets up the VM idempotently: pinned, sha512-checked Caddy 2.11.4; a forwarding-only `cdi-tunnel` user; `sshd -t`, then `caddy validate`, before any reload; ufw.
- [ ] Health-checked ingress, so a dead backend returns a real error instead of RunPod's HTTP-200 fake-502 page (§10.4). **Built, not run:**
  - Caddy actively checks `/` for the page marker `Agentic EMR generation` every 10 s, and marks the backend down on the first failed dial.
  - A dead backend gets a **503** with `X-CDI-Ingress: backend-down` and the body `CDI_BACKEND_DOWN: …`.
  - `scripts/check_ingress.py` reports UP / DEGRADED / BACKEND_DOWN / UNREACHABLE, exit 0/2/3/4, and never trusts the status code alone.
- [ ] TLS terminated at the stable hostname; the runbook (§10.2) no longer treats the per-pod LIVE URL as the address of record. **Built, not run:**
  - Caddy gets the Let's Encrypt certificate automatically and sends HSTS.
  - `start_all.sh` prints `COUNTER URL: https://… (address of record)` once `check_ingress` says UP. The LIVE URL is now "this pod only".
  - Updated: ARCHITECTURE §3.3, §3.5 and §12 Transport; TECH_STACK; `docs/fallback-host.md`; `infra/runpod/README.md`.
  - The UI is limited to clinic CIDRs (`CDI_INGRESS_ALLOW`, required, never `0.0.0.0/0`) until E11-S1 adds a login. `/healthz` stays open for monitors.
- **Tests:** restart/replace the backend — the same URL keeps serving; a stopped backend returns a distinguishable health failure. The steps are in `docs/ingress.md` "Acceptance test". **Owner:** provide the VM, the domain and its DNS `A` record, then the pod. Locally: `tests/test_ingress_unit.py`.

#### Block E1-S7 — Load-test harness (30 × 5–10 concurrent documents)
**Status:** Blocked (2026-09-22). The harness is built and verified. It ran the full 30-patient, 237-document burst against the real web app on this machine. The dry run cannot reach a ready state here because every document stops at ingest on the missing **real MinIO**, the same reason as E0-S3. The measured run needs the pod: `python scripts/load_test.py --host <pod or ingress URL>`. Runbook: `docs/load-test.md`.
`Must · BLOCKER · 2.5 d · 5 pts · Deps: E1-S6 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 (E1-S8 re-score; was ✖ (cites "Brief's own load figure"; ARCHITECTURE.md has no 30-patient load figure; §7.3 is single-job timing)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [x] k6/Locust script uploads N synthetic patients × 5–10 docs concurrently (N configurable, default 30).
  - Locust (TECH_STACK), one user = one patient: `POST /api/jobs` with all files, poll until the job leaves `running`, then `GET …/fhir`.
  - All patients spawn at once; the run stops by itself when the last job is done.
  - `scripts/loadtest/synthetic.py` generates deterministic scanned documents: 5 types the S2 heuristic classifies without a VLM call, no real data.
  - Extra `load`; `make loadtest HOST=…`.
- [x] Reports a p50/p95/p99 upload→bundle-ready latency histogram against the Sizing SLA.
  - `scripts/loadtest/report.py`: exact nearest-rank p50/p95/p99, 30 s histogram bins with bins past the SLA marked, counts by outcome, every sample.
  - The SLA is 300 s (ADR-0002). `review` with no failed document is the "explicit hold"; failed documents, `mismatch`, `error` and timeouts never count as ready.
  - SLA met = every patient ready and p95 ≤ SLA. `--enforce-sla` exits 1 when it is missed.
- [x] Runnable against any environment URL (`--host`: the pod, the E1-S9 stable hostname, the fallback host, local).
- **Tests:** a harness dry run against a local stack. **Partly run.**
  - The harness ran against the real web app here, natively, with Postgres 16.14 and the stub backend: 30 patients, 237 documents, all uploads accepted, every job terminal.
  - Every document failed at ingest on `EndpointConnectionError` to MinIO :9000, so no latency could be measured. **Needs real MinIO (pod / E0-S16).**
  - `tests/test_loadtest_unit.py` (12) runs Locust for real against a §7.1 job-API contract server with controlled timings, checking percentiles, outcomes, timeout, rejected upload and `--enforce-sla`.

### Phase 4

#### Block E1-S10 — Production hosting on an India-resident cloud with a Blackwell-class GPU ➕ 🔒 ⬜
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E1-S3, E1-S4, E1-S9 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖ (ARCHITECTURE.md describes the RunPod pilot pod only; §12 states data residency as "all inference + PHI on the pod")) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
RunPod is the pilot. Production needs a host whose region is documented as India-resident (DPDP, both RFQs).
- [ ] Provider + region chosen with a written residency confirmation for compute, object storage, database, backups, logs and model artefacts
- [ ] GPU class is Blackwell 32 GB or better (decision 2026-09-20); the same image/weights as the pilot, so no re-validation of the model
- [ ] Whole stack reproduced from IaC (E1-S4) in that region: private network, private endpoints, VPN to the clinic, no public inference endpoints
- [ ] Golden-path suite (E1-S6) passes on the new host; RunPod stays as dev/pilot only
- **Tests:** E1-S6 on the production host; a residency checklist signed against the provider's written confirmation
- 🔒 Provider quote and legal residency sign-off

---

# EPIC E2 — S4 Extraction Quality & Serving Acceleration

### Phase 2

#### Block E2-S1 — vLLM AsyncLLMEngine backend for mlserve ✅
**Status:** Blocked (2026-09-22). Rebuilt (the `fdd4aeb` code was not provided) and verified against a fake engine with vLLM's streaming interface. The real engine needs **Linux + the GPU pod**: vLLM publishes no Windows wheels, and the A/B needs the Blackwell GPU. **Version decision (from the architecture):** no vLLM release fits both App. B pins. vLLM 0.11.0 is the last on torch 2.8 but predates transformers 5; vLLM releases supporting transformers 5 need torch 2.11–2.13. §3.4 makes the gateway its own process "swappable without touching pipeline code", so **vLLM 0.11.0 runs the gateway from its own venv** (`.venv-vllm`, transformers 4.57.6, `infra/runpod/requirements-vllm.lock`, 133 packages, hash-locked). The pipeline venv and App. B pins are untouched. `hf` stays the active backend.
`Must · BLOCKER · 4 d · 8 pts · Deps: E1-S3 · Arch ✔ §3.4, §14 #1a · DoD-CODE`
- [ ] `CDI_MLSERVE_BACKEND=vllm` serves the same `/vlm/generate` contract; `hf` selectable with one env flip. **Built; verified locally with a fake engine; not yet run with real vLLM (needs the pod).**
  - `VLLMBackend` in `mlserve/backends.py`: `AsyncLLMEngine` on its own event-loop thread, so concurrent gateway requests are batched while `generate` stays synchronous.
  - Engine settings: Qwen2.5-VL chat template, image as `multi_modal_data`, greedy sampling as `hf`, `max_pixels` cap, 7B→3B fallback on OOM. New settings `CDI_VLLM_GPU_MEMORY_UTILIZATION` / `_MAX_MODEL_LEN` / `_MAX_NUM_SEQS` (0.88 / 16384 / 4).
  - Pod: `bootstrap_pod.sh` step 3b builds `.venv-vllm` from its lock (and pinned cu128 torchaudio) and drift-checks it in `manifests-vllm`; `start_mlserve.sh` starts the gateway from it when `vllm` is selected.
  - `lock_pod_deps.py --target vllm`.
  - Tests (`tests/test_vllm_unit.py`, 11):
    - same request/response and `/healthz` contract
    - 4 concurrent requests all in flight together (wall < 2× one request), while `hf` peaks at 1
    - greedy sampling, chat prompt and image passed
    - OOM → 3B; other start-up errors surface
    - one env value selects `stub` / `hf` / `vllm`
    - vLLM is never imported unless used
    - the lock is exact and hashed (vllm 0.11.0, transformers 4.x, no torch family); the pipeline lock keeps transformers 5.16.1
    - bootstrap and start script wiring
  - **On the pod:** set `CDI_MLSERVE_BACKEND=vllm`, run `start_all.sh`, then the A/B in `infra/runpod/README.md` (golden path + load test on each backend).
- **Evidence (Excel):** built and A/B tested on-pod; **no throughput gain** (72–74 s, unchanged). Reverted to `hf`; the code is kept, dormant. Confirmed in code: `VLLMBackend` in `mlserve/backends.py`.

#### Block E2-S2 — Remove the Phase-2 serialisation lock in `webapp/jobs.py` ✅
**Status:** Blocked (2026-09-22). Rebuilt and verified here with pluggable stages and on real PostgreSQL. The throughput A/B needs the real vLLM engine on the GPU pod (E2-S1).
`Must · BLOCKER · 1 d · 2 pts · Deps: E2-S1 · Arch ✔ §7.2, §14 #1b · DoD-CODE`
- [x] With vLLM, documents in a job extract in parallel; with `hf`, the lock stays active
  - **Built:**
    - `CDI_EXTRACT_CONCURRENCY` (`extract_concurrency`, default 0 = auto) and `webapp.jobs.extract_slots()`:
      - `hf` is always 1;
      - `vllm` uses the setting, or `vllm_max_num_seqs` (4) when it is 0;
      - `stub` is 1 unless set.
    - `JobRunner`:
      - k jobs at once, and k extractions at once through one semaphore shared by all jobs;
      - with k = 1 it behaves exactly as before (one job at a time, serial Phase 2);
      - with no chosen patient, documents run one by one until one fixes the patient, then the rest run in parallel;
      - once a mismatch is found, no new extraction starts.
    - **Race found and fixed:** jobs running in parallel could both miss a new patient and mint two MPI records. `mpi.resolve_identity` now takes a transaction-scoped advisory lock (`pg_advisory_xact_lock`), so look-up + mint is one step.
  - **Tests:**
    - `test_jobs_unit.py` 14 (7 new): slot rules per backend; 4 documents in flight together with vLLM; serial with 1 slot; first document alone then the rest in parallel; skipped first document hands over; slots shared across jobs (peak = 2 of 2); mismatch stops new starts.
    - `test_identity_integration.py`: two concurrent resolves of one new patient mint once. With the lock removed, the same test fails (`UniqueViolation` on `ux_patient_identity_abha`).
  - **On the pod:** after E2-S1, run the same job with `hf` and with `vllm` and compare the wall time (`infra/runpod/README.md`).
- **Evidence (Excel):** same A/B as E2-S1. Confirmed in code: `extract_concurrency` in `config.py`.

#### Block E2-S3 — Grammar-locked decoding (guided_json / XGrammar) ✅
**Status:** Blocked (2026-09-22). Built and proven here with the real XGrammar 0.1.25 (the version vLLM 0.11.0 pins) and the real Qwen2.5-VL tokenizer. The last step, the grammar inside the real vLLM engine, needs the GPU pod (E2-S1).
`Should · HARDEN · 3 d · 5 pts · Deps: E2-S1 · Arch ✔ §3.4, §14 #1c · DoD-CODE`
- [x] Schema-invalid JSON impossible by construction (grammar rejects the token)
  - **Built:**
    - `src/cdi_adapter/mlserve/grammar.py`: schema → exact XGrammar EBNF (cached per schema).
    - `VLLMBackend` sends it as `StructuredOutputsParams(grammar=…)`.
    - The engine uses `StructuredOutputsConfig(backend="xgrammar", disable_fallback=True)`, so vLLM never switches to another backend or runs a request unconstrained.
    - Setting `CDI_VLLM_GUIDED` (default `true`).
  - **Gap found in XGrammar 0.1.25 and fixed:**
    - It does **not** enforce a `number` maximum: for 0–1 it lets `1.8` through. vLLM's own feature check does not catch this.
    - All 35 bounded numbers in our schemas are 0–1 `confidence` fields. Each gets an exact hand-written rule (`0`, `0.x` up to 6 decimals, `1`, `1.0`).
  - **Fails loudly:** a schema feature the grammar cannot enforce exactly (vLLM's list plus a non-0–1 number range, `pattern`, `maxItems`, a leftover `$ref`, …) is refused with `GrammarError` → HTTP 422, before the engine is asked.
  - **Tests** (`test_grammar_unit.py` 36, `test_vllm_unit.py` +5):
    - all 7 schemas lock exactly;
    - the plain conversion accepts `1.8` and the exact grammar refuses it;
    - 8 invalid continuations are rejected at the first bad token (unknown key, wrong type, enum, >1, negative, page 0, prose, a Markdown fence), each checked against a valid prefix;
    - a missing required key cannot be closed;
    - key order is kept;
    - random generation through the real token mask gives **105 of 105 finished outputs schema-valid**.
  - **Still possible:** output cut off at `max_tokens` is incomplete. The client's retry and `_partial` path stays for that case, and for `hf`.
  - **Not done here:** re-tightening the v3 schemas (ARCHITECTURE §14 #3), and the `_partial` rate on the E2-S4 gold set.
- **Evidence (Excel):** same A/B as E2-S1. Confirmed in code: `vllm_guided` / XGrammar.
- **Note:** the acceptance criterion "`_partial` rate on the E2-S4 gold set drops to 0" can't be measured until E2-S4 exists.

#### Block E2-S6 — Quantize + benchmark the fine-tuned DSLM ⬜
**Status:** Blocked (2026-09-24). The quantize and benchmark tooling is built and tested here. The block's input, the **E2-S5 checkpoint**, does not exist yet: E2-S5 is Pending on the E2-S4 gold set, which waits on a clinical annotator. A `MEASURED` row also needs the **32 GB target GPU**. Runbook: `docs/quantization.md`; one command on the GPU host: `infra/runpod/dslm_quantize_bench.sh`.
`Should · HARDEN · 3 d · 5 pts · Deps: E2-S5 · Arch ✔ §14 #1d (AWQ/INT4) · DoD-EVAL`
- [ ] AWQ/INT4 the E2-S5 checkpoint. **Tool built; waits on E2-S5.**
  - `cdi_adapter.dslm.quantize`: AWQ `W4A16_ASYM` (int4, group 128), llm-compressor 0.7.1.4 in its own venv `.venv-quant` (`requirements-quant.lock`, 56 packages, hash-locked, transformers 4.55.2, torch 2.8).
  - **Version decision (from the architecture):** 0.7.1.4 is the release that writes compressed-tensors **0.11.0**, the exact version vLLM 0.11.0 reads (E2-S1), and that runs on torch 2.8 (App. B). Newer releases need torch ≥ 2.9 or transformers 5.
  - **Gap found and fixed:** llm-compressor has no AWQ mapping for Qwen2.5-VL, and its generic fallback would also smooth the vision tower's `up_proj → down_proj`. The mappings are anchored to the decoder's `layers.N`; the vision tower and `lm_head` stay bf16.
  - Calibrated on the real S4 requests (`cdi_adapter.dslm.corpus`: normalized page image at the serving `max_pixels` + the `[bN]` prompt), synthetic or de-identified only. It refuses a non-Qwen2.5-VL or already-quantized source, and checks that the output is compressed-tensors int4/g128.
  - `cdi_quantization.json` records source, recipe, calibration digest and tool versions.
  - The output serves with no new setting: `CDI_VLM_MODEL_ID=<dir>` (vLLM detects the format).
- [ ] Measure latency/VRAM vs the bf16 7B baseline: extract-seconds/doc and concurrent slots at 32 GB. **Tool built; needs the checkpoint and the target GPU.**
  - `cdi_adapter.dslm.bench run` replays the corpus through the production `VLLMBackend`: same engine settings, prompt, image handling and XGrammar lock as the gateway. The 7B→3B fallback is off, so a variant that does not fit fails. One process per variant.
  - It records:
    - s/doc (median/p95, after a warm-up)
    - docs/min at concurrency 1/2/4
    - VRAM in use
    - vLLM's KV cache in tokens
    - slots = KV tokens ÷ p95 request footprint (and a worst case at the full `max_tokens`)
  - `VLLMBackend` gained `generate_with_usage` (prompt/output token counts) and `kv_cache_tokens()`. `generate` and the `/vlm/generate` contract are unchanged.
- [ ] Write the results into *Sizing & Capacity* (DSLM row, currently row 32), tagged MEASURED. **Row produced; the workbook is not in the repo.**
  - `bench compare` produces the comparison and a Sizing row per variant. The tag is `MEASURED` only on the RTX PRO 4500 32 GB; any other GPU is tagged `MEASURED on <gpu> (not the 32 GB target)`.
  - Output agreement with bf16 is shown as a regression signal only. Accuracy is E2-S7 on the E2-S4 gold set (DoD-EVAL).
  - The Excel is not in this repository, so the row is copied by hand from the archived `.md`.
- **Tests:** benchmark script output archived. `compare --archive docs/bench/e2-s6` writes a dated `.json` (summary + every raw result) and `.md`. `tests/test_dslm_unit.py` has 24 tests: the corpus matches the pipeline's request, data-class refusal, real RapidOCR on the samples, AWQ mappings cover every decoder projection and no vision layer, recipe/source/output checks, calibration round-robin, manifest, the backend's token/KV hooks, and the run (production backend, no fallback, slots arithmetic, concurrency 4 in flight). Also: compare (speed-up, schema-valid, agreement, tag never passed off), the archive, the quant lock matching vLLM's compressed-tensors, and the run script's venv order.
- **Risk to check on the pod:** vLLM 0.11.0's int4 (Marlin) kernels on the Blackwell GPU. If `dslm-awq` fails to start there, record the failure. The fix is a vLLM pin change (App. B), not a silent change of method.

#### Block E2-S8 — Batch small documents/pages into fewer VLM calls ⬜
**Status:** Blocked (2026-09-24). Batching is built and verified here, including on real PostgreSQL, where a batched extraction writes the same facts as single calls. It is **off by default** (`CDI_EXTRACT_BATCH_MAX=1`). The two measurements the block asks for need things this build does not have: the docs/minute A/B needs the **GPU pod** (E1-S7 harness), and the accuracy check needs the **E2-S4 gold set** and the E2-S7 harness.
`Could · OPTIMIZE · 2 d · 3 pts · Deps: E2-S1, E1-S7 · Arch ◐ §7.4 Performance plan · DoD-CODE`
- [x] Group short documents (e.g. single-page vitals sheets) into one VLM call where the schema allows
  - **Design, chosen from the architecture (owner delegated the choice):** one call carries several documents as **separate sequences in one batched forward pass**, not one merged prompt.
    - Speed: extraction time is mostly output tokens (§7.4). Separate sequences produce them in the same decode steps; a merged prompt would still produce them one after another.
    - Safety: a merged prompt risks evidence from one document being tied to another (§0.5 "evidence proves"). Separate sequences keep each document's prompt, image, schema and evidence map apart.
  - "Where the schema allows" = the same doc_type (the same schema, so similar output lengths and no sequence waits long on another). "Short" = one page and at most `CDI_EXTRACT_BATCH_MAX_BLOCKS` (40) OCR blocks. Groups hold up to `CDI_EXTRACT_BATCH_MAX` documents.
  - **Built:**
    - Gateway: `POST /vlm/generate_batch` (1–8 items, texts returned in order, same validation and 422 rules as `/vlm/generate`).
      - `hf`: one left-padded `generate` for the whole batch, still one pass at a time on the GPU; each row cut to its own `max_tokens`. A batch that runs out of GPU memory is split into single calls, not dropped.
      - `vllm`: all items reach the engine together, each with its own grammar lock.
    - Client: `vlm_json_batch`. Each batched answer goes through the same repair and validation; one that fails continues on exactly the single-call path (re-ask with its errors, same retries, same `_partial` fallback).
    - Extract service: `extract_documents`. The single and batched paths share the same steps (`_start` / `_request` / `_complete` / `_record_failure`), so each document still gets its own run, provenance, identity check and audit. One document's failure never touches the others, and a failed batch call falls back to one call per document.
    - Job runner: groups only in the parallel part of Phase 2, after the first document has fixed the patient. A batch takes one GPU slot. A mismatch inside a batch stops the job exactly as a single one does. A document whose shape cannot be read goes alone. `repo.document_shape` gives the page and OCR-block counts.
    - Settings: `CDI_EXTRACT_BATCH_MAX` (default 1 = off), `CDI_EXTRACT_BATCH_MAX_BLOCKS` (40).
- [ ] Measurably higher docs/minute/GPU on the E1-S7 harness vs one call per document. **Needs the GPU pod.** Run the E1-S7 burst with `CDI_EXTRACT_BATCH_MAX=1` and `=3` (`infra/runpod/README.md`). The expected gain is mostly on `hf`, but that is not measured yet, so it stays off.
- [ ] No drop in per-field accuracy on E2-S4. **Needs the E2-S4 gold set + the E2-S7 harness.** Here, with the stub model, a batched extraction writes exactly the facts, values, units, confidences, review states and evidence blocks that single calls write, on real PostgreSQL.
- **Tests:**
  - `test_batch_unit.py` (18):
    - the endpoint answers each item as its own request, with the same validation
    - `hf`: one pass for the batch under the GPU lock; out of memory → split; other errors raise
    - `vllm`: all items in flight together, each with its own grammar
    - the client: one call, only the invalid answer is asked again; a batch of one is the single path; stub batched = single; HTTP body and count check
    - the extract service: per-document isolation (skip, missing image, mismatch); a failed batch falls back to single calls; one pending document is not batched
    - the job runner: grouping by type, shortness, page count and size limit; off by default; a mismatch in a batch; a failed batch; an unreadable shape; the real stages wired
  - `test_batch_integration.py` (Postgres): batched = single, document by document.
  - E1-S7 A/B + E2-S7 accuracy comparison: pending, as above.

---

# EPIC E6 — FHIR/ABDM IG Validation & Correctness

### Phase 1

#### Block E6-S5 — IG package version-pin + upgrade process ⬜
**Status:** Completed (2026-09-22). Built, tested, and dry-run with the real HL7 validator against real registry packages. Its dependencies E6-S1 and E6-S4 (`backlog 1.md`) were built first, as the architecture requires; both are Blocked for their own reasons (pod run; builder conformance). This block's procedure works either way, because it gates on "no new IG errors" against the E6-S4 baseline. **Finding:** `nrces.fhir.r4.ndhm` is not a registry id; the pin is now `ndhm.in#6.5.0`. Runbook: `docs/ig-upgrade.md`.
`Should · HARDEN · 1 d · 2 pts · Deps: E6-S4 · Arch ◐ §9 Configuration · DoD-DEPLOY`
- [x] Runbook: upgrade the IG package in staging → re-run E6-S4 → only then flip production `CDI_IG_PACKAGE` (currently `nrces.fhir.r4.ndhm#6.5.0`).
  - `docs/ig-upgrade.md` and `scripts/fhir_ig.py`, with three steps:
    1. `stage --ig <id#ver> --home <staging>` (refused on the production home). The validator discovers the candidate's packages online, once; they are reinstalled from the registry, then the E6-S4 goldens are re-run offline.
    2. `promote --dry-run` lists the changes.
    3. `promote` refuses unless staging passed for exactly that IG and the staging cache is unchanged, then rewrites `infra/fhir/packages.lock`, `FHIR_IG`/`FHIR_IG_SHA256` and `CDI_IG_PACKAGE` together.
  - Production flips only when the pod boots with the new pins: `bootstrap_pod.sh` 5b refuses a mismatch and seeds and proves the locked set.
- **Tests:** a staging dry run of the procedure. **Run for real here** (validator 6.10.4, fresh staging caches):
  - **Pass path** `ndhm.in#6.5.0`: PASSED, 0 new / 0 fixed errors on all 5 goldens. `promote --dry-run` would change no package (the fresh stage reproduced `packages.lock` exactly), with identical pin values.
  - **Refusal path** `ndhm.in#6.0.0`: FAILED, 51 new errors (40 fixed). `promote` **refused**.
  - Unit: `tests/test_fhir_ig_unit.py` (21, incl. promote refusals, dry run writes nothing, staging never targets production).

---

# EPIC E7 — Human-in-the-Loop / Counter-Side Review Hardening

### Phase 3

#### Block E7-S1 — AuthN for the /review console ⬜ 🔒
**Status:** Completed (2026-09-24). Built and tested on real PostgreSQL with the real web app. The 🔒 (identity provider / user directory) was resolved by E11-S1: accounts are local to the app (`web_user`), since the architecture names no identity provider. A clinic SSO can replace the password check later without changing this block. Runbook: `docs/auth.md` "Who a decision is recorded under".
`Must · BLOCKER · 2 d · 3 pts · Deps: E11-S1 (resolved by resequencing, §3.1) · Arch ✔ §12, §4.3, §14 #6 · DoD-SEC`
- [x] `/review`, `/api/review/*` and `/api/facts/*/review` require an authenticated staff session
  - Already enforced by E11-S1's fail-closed policy (`reviewer` or `admin` role). This block adds tests on real PostgreSQL: no session → 401 (a browser is sent to `/login`); an `intake` session → 403; nothing is decided either way.
- [x] The reviewer comes from the session and can no longer be typed by the client (`fact_provenance.agent`)
  - `auth.actor(request)` gives the signed-in user. `review.decide(conn, fact_id, decision, reviewer)` takes the reviewer from it, and `ReviewDecision` no longer has a `reviewer` field, so a client-sent one is ignored.
  - The same rule is applied everywhere a clinical decision is recorded, not only S8:
    - `/reviewer` claim, approve, reject and "my worklist" (`?reviewer=` is ignored);
    - Generate EMR edits (`POST /api/jobs/{id}/generate`, previously recorded as `job-editor`).
  - **Gap found and fixed:** approve and reject checked that an item was claimed, but not by whom, so once identity was real any reviewer could approve another's claimed item. Now only the claimer may decide an item (403 otherwise). An `admin` may override, and the decision is recorded under the admin's own name.
  - Pages: the "Reviewer id" boxes are gone from `/review` and `/reviewer`; `/reviewer` shows who is signed in (`/api/me`) and loads the worklist by itself.
- **Tests:** unauthenticated → rejected; a spoofed reviewer parameter is ignored
  - `test_review_integration.py` now runs on the real signed-in app (12 tests, 3 new):
    - a spoofed `reviewer: "dr.mallory"` → provenance agent and audit actor are the session user;
    - no session / `intake` → 401 / 403 on every review route, nothing decided;
    - Generate EMR edits are signed by the session user despite a spoofed reviewer.
  - `test_reviewer_integration.py` (14 tests, 4 new):
    - a spoofed reviewer on claim is ignored; a worklist is always the caller's own;
    - a second reviewer gets 403 on approve and reject and the item stays claimed;
    - an admin override is recorded under the admin;
    - no session / `intake` → 401 / 403 on every console route.
  - DoD-SEC: the misuse cases above are the documented negative tests. No plaintext secret was added (secret scan CLEAN).

---

# EPIC E9 — Scale, Queueing & Autoscaling for 30-Concurrent Load

### Phase 4

#### Block E9-S1 — Move web-app ingestion onto the durable queue ⬜
**Status:** In Progress (2026-10-03). Every acceptance item is built; the kill test is the only thing left and it needs the pod (E1-S7 is Blocked there). The first two items came with E10-S9, which needed them; the migrate-under-load item was built on 2026-10-03: `cdi_adapter.migration` + migration 0021 (`schema_gate`, `migration_run`), `make migrate-live` / `migrate-rollback` / `schema-status`, documented in docs/ha.md "A schema migration with the cluster up". Verified on real PostgreSQL: `tests/test_migration_integration.py`, 8 passed.
`Must · BLOCKER · 3 d · 5 pts · Deps: E1-S7 · Arch ✔ §13, §14 #8 · DoD-CODE`
- [x] Route `/api/jobs` through the existing Celery/Redis chain instead of the in-process pool (`_jobs` dict in `webapp/jobs.py`). Done in E10-S9: task `cdi.web_job` on queue `cdi-web`, the job in `web_job`.
- [x] A webapp restart mid-job loses no in-flight uploads; queued jobs resume. Done in E10-S9: uploads are durable before the 202, and a job whose runner dies is resumed (Colab drill: 0 of 12 lost).
- [x] ➕ A forward-incompatible Alembic migration can be applied with workers in flight, and rolled back, without corrupting or double-processing queued jobs. A migration goes through the **gate** (`schema_gate`): one migrator (advisory lock), no new claim while it is closed (`claim` and the orphan sweep both refuse, with `expires_at` bounding the hold), a bounded drain of everything in flight — web jobs with a live lease *and* open `pipeline_run` rows, so the folder-watch path counts too — and nothing applied if it does not drain; the gate reopens even when the migration fails. Running jobs are never interrupted. Each window is recorded in `migration_run`.
- **Tests:** kill the webapp during the E1-S7 load test → zero lost documents (⛔ waiting on the pod: E1-S7); a migrate-under-load test ✅ `tests/test_migration_integration.py` — a real forward-incompatible revision applied and rolled back with three jobs queued and one in flight, then each queued job runs exactly once; an aborted migration changes nothing; one migrator at a time; a closed gate never takes a job from a live runner; an expired hold gates nothing

#### Block E9-S2 — Separate the CPU pool (ingest/OCR/classify) from the GPU pool (extract) ⬜
**Status:** In Progress (2026-10-03). The split is built; the throughput measurement is the only thing left and it needs the pod (it is E9-S6's load test, Blocked there with E1-S7). Documented in ARCHITECTURE §7.5 and docs/ha.md.
`Must · BLOCKER · 3 d · 5 pts · Deps: E9-S1 · Arch ✔ §7.3, §7.4, §14 #8 · DoD-CODE`
- [x] CPU-stage and GPU-stage worker counts independently configurable and scalable. Three queues: `cdi` (folder-watch CPU stages) and `cdi-web` (web-app Phase 1) are the CPU pool, `cdi-gpu` (`cdi.extract_document` and the web job's Phase 2) the GPU pool. `worker.queues_for_role` / `role_for_queues` / `concurrency_for_role` are the one source of the queue names and the auto-sizing rule; `CDI_WORKERS` × `CDI_WORKER_CPU_CONCURRENCY` and `CDI_GPU_WORKERS` × `CDI_WORKER_GPU_CONCURRENCY` size the two sides, and `start_apptier.sh` starts both pools (`make worker-cpu` / `worker-gpu`; `make worker` and compose stay one worker on all three queues).
- [x] A web job crosses the boundary once, repeating no work: the CPU runner saves Phase 1, releases its lease and re-queues the job to `cdi-gpu` (`jobstore.dispatch_extract`); the GPU runner resumes at extraction because `jobs.phase1_done` says that work is in the database, so no staged upload is read again. The handoff claim does not spend an attempt; a lost handoff is an ordinary orphan, and a CPU-pool sweep sends a Phase-2-only job on instead of extracting it.
- [ ] Scaling only the CPU pool measurably raises Phase-1 throughput without touching GPU spend — ⛔ waiting on the pod (the measurement is E9-S6 / E1-S7)
- **Tests:** load-test comparison (⛔ pod) · ✅ `tests/test_pool_split_unit.py` — 12 tests: routing (only extraction on `cdi-gpu`), queue lists and roles derived from the consumed queues, each pool sized independently, the CPU pool running Phase 1 and handing over, the GPU pool resuming with no Phase-1 call and no staged read, one worker on both queues doing both phases, inprocess ignoring the role, and a lost handoff recovered by the sweep · ✅ `tests/test_apptier_integration.py` on real PostgreSQL — a phase-boundary claim is not another attempt and the lease rules are unchanged

#### Block E9-S3 — Priority + dead-letter queues, idempotency keys ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E9-S1 · Arch ✔ §5 S1 dedupe, §14 #8 · DoD-CODE`
- [ ] Submitting an identical multipart `/api/jobs` request twice → exactly one job outcome
- [ ] A poison document goes to a dead-letter queue after N attempts instead of blocking the queue
- [ ] Priority queues
- **Tests:** duplicate-submit test; poison-document test

#### Block E9-S8 — Circuit breakers, timeout budgets & governed degradation ➕
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E9-S1, E9-S3 · Arch ◐ §14 #8 (priority/dead-letter queues) + [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 ("explicit hold with an ETA"); breaker thresholds themselves not specified (E1-S8 re-score; was ✖ (§3.4 names only the 7B→3B OOM fallback; no breaker or degradation model)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ◐. External 🔒 dependencies, if any, are unchanged.
E9-S3 handles a poison *document*. This handles a sick *dependency*: a 5xx storm that thrashes
retries and exhausts connection pools.
- [ ] Breaker + timeout budget + bounded retry around every external call: `mlserve`, the terminology service (E3), MinIO, Postgres, the ABDM gateway (E8)
- [ ] Bulkhead pool isolation, so one failing dependency cannot exhaust shared connection pools
- [ ] A declared degraded mode per dependency, each mapped onto the governance rule: terminology down → `local_only` **and facts stay held** (never auto-accept an unmapped critical fact); `mlserve` down → documents hold, never a partial guess; MinIO down → refuse the upload ACK rather than acknowledge an unstored file
- [ ] An open breaker applies queue backpressure instead of retry-thrash; breaker state exposed as an E10-S1 metric
- [ ] **Negative test: no degraded path may widen the auto-accept set**, verified by injecting each outage during a governed run
- **Tests:** injected 5xx storm per dependency (no pool exhaustion, no thrash); the governance negative test; feeds E14-S5's terminology-outage failure run

#### Block E9-S4 — Autoscale the GPU worker pool on queue depth ⬜
**Status:** Pending
`Must · BLOCKER · 4 d · 8 pts · Deps: E9-S2, E2-S1 · Arch ✔ §14 #8 ("autoscaling") · DoD-DEPLOY`
- [ ] GPU workers scale out when queue depth/wait time breaches a threshold, and back to the off-peak floor (0 or 1 warm) when it drains
- [ ] Policy thresholds in config, not hardcoded
- **Tests:** E1-S7 load test shows scale-out and scale-in

#### Block E9-S5 — Autoscale the CPU (ingest/OCR) pool independently ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E9-S2 · Arch ✔ §14 #8 · DoD-DEPLOY`
- [ ] CPU pool scales 0→N on the same queue-depth signal, with its own sizing and a more generous threshold
- **Tests:** load test shows independent CPU scaling

#### Block E9-S6 — Load-test the autoscaled system against the 30 × 5–10 scenario
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E9-S4, E9-S5 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 (E1-S8 re-score; was ✖ (the 30 × 5–10 figure is from the brief)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Run E1-S7 against the fully autoscaled deployment; publish p95 upload→bundle-ready latency against the SLA
- [ ] On a miss, identify the responsible stage (Phase-1 CPU / Phase-2 GPU / review queue)
- **Tests:** a published load-test report

#### Block E9-S7 — Patient-aware deadline queueing + predictive autoscaling/pre-warm
**Status:** Pending
`Must · BLOCKER · 5 d · 8 pts · Deps: E9-S2, E9-S4, E1-S7 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 (E1-S8 re-score; was ✖ (beyond §14 #8's generic autoscaling)) · DoD: policy parameters config-driven; queue-age, predicted-work and replica-count metrics in Grafana; runbook has overload + degraded-mode procedures`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Each patient gets a deadline = last upload + SLA; the scheduler is fair across patients and prioritizes those nearest their deadline
- [ ] Scale on oldest queue age + estimated remaining GPU-seconds, not raw depth
- [ ] Scheduled warm on-demand floor during OPD; pre-warm before opening; scale to zero only off-hours
- [ ] Cold-start and node-loss tests publish p95/p99 bundle latency + missed-deadline count
- **Tests:** a 30 × 10 burst fairness test; cold-start test; node-loss test

---

# EPIC E10 — Observability, Ops & Governance

### Phase 6

#### Block E10-S7 — WORM audit export ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: — · Arch ✔ §12 Audit, §14 #9 · DoD-SEC`
- [ ] Replicate every `audit_log` row to an append-only, tamper-evident store within a defined SLA (e.g. 5 min)
- [ ] A deletion attempt against the WORM copy is rejected
- **Tests:** replication-lag test; deletion-rejection test

### Phase 7

#### Block E10-S1 — Prometheus metrics ⬜
**Status:** Completed (2026-09-24). Built, tested, and deploy-checked on the Colab L4 fallback host (Ubuntu 24.04, `docs/fallback-host.md`) at the owner's direction, in place of the pod. `/healthz` was OK on both processes. `/metrics` was bound to 127.0.0.1 only (9464, 8077), and staff port 8888 returned 404 for `/metrics`. During a live job every stage counted once (extract 57.8 s), GPU utilization peaked at 100% with 18.4 GB in use, `cdi_extract_in_flight` reached 1, the gate counted 3 `in_review`, the backlog rose by 1 held task and 1 workbasket item, and the gateway counted 2 `ok` calls. Runbook: `docs/metrics.md`. The Prometheus server that scrapes these is E10-S2.
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §13, §14 #9 · DoD-DEPLOY`
- [x] `/metrics` exposes per-stage latency histograms, queue depth, GPU utilization %, and S6 gate `auto_accepted` vs `in_review` counts (plus reviewer throughput)
  - **Design, chosen from the architecture (owner delegated the choice):** `prometheus-client` (0.26.0, the version the vLLM lock already pins), exposed by the two processes that do the work, and **loopback only** (§12):
    - the web app on its own port `CDI_METRICS_PORT` (9464, bound to 127.0.0.1), never on the staff port 8888 that the ingress and the RunPod proxy expose, so nothing needs a sign-in or an ingress rule to protect it;
    - the model gateway on its existing loopback port (8077).
  - Metrics (`src/cdi_adapter/metrics.py`):
    - per stage: `cdi_stage_seconds{stage}` (histogram, 0.25 s–10 min) and `cdi_stage_failures_total{stage}` for ingest / ocr / classify / extract / terminology / validate; `extract` covers the GPU work only;
    - queue depth: `cdi_extract_waiting`, `cdi_extract_in_flight`, `cdi_jobs{state}`, and the human queue `cdi_review_backlog{queue}` (held-fact tasks, audit tasks, workbasket, claimed; read from PostgreSQL at scrape time);
    - GPU: `cdi_gpu_utilization_percent{gpu}` and memory used/total, read from `nvidia-smi` at scrape time;
    - S6 gate: `cdi_gate_decisions_total{decision}` (auto_accepted / in_review);
    - reviewer throughput: `cdi_review_decisions_total{action}` and `cdi_review_items_total{action}`;
    - gateway: `cdi_vlm_generate_seconds{kind}` and `cdi_vlm_requests_total{kind,outcome}` (ok / refused / error).
  - Rules: labels come only from fixed lists, so no PHI can become a label. A database or GPU failure leaves that metric out of the scrape; the scrape itself never fails.
  - **Found while testing:** with one GPU slot, waiting documents sit in the thread pool's own queue and never reach the slot, so counting "waiting for the slot" showed 0 while documents waited. Queue depth is now counted per job, from the end of Phase 1 until a slot is held; skipped documents and anything left at the end of the job are subtracted.
  - **Dependency choice:** `nvidia-ml-py` would fall under the lock's `nvidia-*` torch-family rule, which assumes the torch image supplies it, and it doesn't. `nvidia-smi`, installed with every driver, needs nothing new.
  - `requirements.lock`: only `prometheus-client==0.26.0` was added. A fresh resolve would also have bumped six unrelated packages (starlette 1.6→1.7, boto3, botocore, filelock, mako, wcwidth), and those were left unchanged.
- **Tests:** scrape test for each metric
  - `test_metrics_unit.py` (15), all parsed back from the real exposition format:
    - every stage timed across a job; a raising stage counts a failure;
    - queue depth peaks while documents wait for one slot and returns to 0;
    - jobs by state; review backlog, including a database outage leaving it absent;
    - GPU from recorded `nvidia-smi` output (`[N/A]` skipped; no GPU → absent);
    - the gateway `/metrics` counting ok / refused / batch calls;
    - the web app's loopback server serving every metric, while the staff app has no `/metrics` and it isn't in the policy table;
    - port 0 serves nothing; labels from the fixed list only.
  - `test_metrics_integration.py` (2, real PostgreSQL): the gate counters match what `validate_document` decided; claim, accept and approve counters and the backlog query move with a real reviewer session.

#### Block E10-S2 — Grafana dashboards + alerting ⬜
**Status:** Completed (2026-09-24). Built, tested, and deploy-checked on the Colab L4 fallback host at the owner's direction, as for E10-S1. Runbook: `docs/alerts.md` (on-call); `docs/metrics.md` (the stack). **Open, owner's choice:** where alerts are delivered. The `oncall` receiver has no channel yet, so alerts show in Alertmanager and Grafana but page no one; add one integration (email, webhook, pager) with its credential in the secrets store.
`Must · BLOCKER · 2 d · 3 pts · Deps: E10-S1 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [x] 4 alert rules: SLA-window breach, GPU OOM, queue backlog above threshold, `_partial` rate above rolling baseline
  - `infra/monitoring/rules/cdi-alerts.yml`:
    - `CdiSlaWindowBreach` (page): a job still running past 300 s, or one that finished past 300 s in the last 10 min (ADR-0002);
    - `CdiGpuOom` (page): any CUDA out-of-memory in 15 min;
    - `CdiQueueBacklog` (page): `cdi_extract_waiting` > 30 for 5 min. That is ~7.5 min of work at the 57.8 s/page measured in E10-S1 with up to 4 slots, so the SLA is already lost;
    - `CdiPartialRateAboveBaseline` (ticket): the last hour's `_partial` share > 1.5 × its 7-day rate + 5 points, with ≥ 10 extractions.
  - New inputs in `cdi_adapter.metrics`: `cdi_job_seconds` (300 s bucket exact), `cdi_job_oldest_running_seconds` (so a breach shows while it happens), `cdi_vlm_oom_total{where}` (load / batch / generate), `cdi_extractions_total{outcome}`.
  - **Stack, chosen from the architecture (native, pinned, loopback; owner delegated the choice):** Prometheus 3.14.0, Alertmanager 0.34.1 and Grafana 13.2.2 from the projects' release tarballs, with sha256 in `versions.env`, checked before unpacking. `infra/monitoring/start_monitoring.sh` is idempotent and starts all three on 127.0.0.1 (9090 / 9093 / 3000). Prometheus keeps 30 days (the 7-day baseline needs it). Dashboard "CDI-Adapter overview" (13 panels, alert list first) is provisioned read-only.
  - **Found on the Colab deploy check, fixed:** Grafana 13.2 ignores the admin-password `__FILE` override and silently created **admin/admin**. The script now sets the admin password from the secrets store after every start (`grafana cli admin reset-admin-password --password-from-stdin`), passes credentials to curl on stdin only, and **fails the start (and stops Grafana) if admin/admin is still accepted**. The password is `cdi_grafana_admin_password` in the secrets store (E1-S5), generated by `init` and strength-checked by `check`.
- [x] A documented on-call response for each
  - `docs/alerts.md`: numbered steps per alert, what each Grafana panel tells you, and the fix per cause (`where` of an OOM, a stuck vs. a busy queue, what changed for `_partial`). The standing rule: never lower a gate threshold or auto-accept to beat the SLA; a hold with an ETA is always an acceptable answer (§0.5, ADR-0002).
- **Tests:** a synthetic trigger for each alert
  - `infra/monitoring/rules/cdi-alerts.test.yml`: a firing and a quiet case per alert. Quiet cases: jobs inside the SLA, no OOM, a burst that drains, a steady baseline, too few extractions to judge. Run by the pinned `promtool test rules`.
  - `tests/test_monitoring_unit.py` (15). It runs promtool when present and also checks:
    - every metric a rule or panel uses exists;
    - every alert has a runbook section with steps, and every expected annotation matches the rule's own;
    - the dashboard is provisioned against the local Prometheus;
    - everything listens on loopback;
    - every download is pinned and hash-checked;
    - the Grafana password comes from the store and admin/admin is refused;
    - the new counters move (job time, oldest running job, OOM at load / batch / generate).
  - `tests/test_metrics_integration.py` (+1, PostgreSQL): an extraction counts as `complete` or `partial`.

#### Block E10-S3 — Centralized logs + OTel tracing ⬜
**Status:** Completed (2026-09-24). Built, tested, and deploy-checked on the Colab L4 fallback host at the owner's direction, as for E10-S1 and E10-S2. Runbook: `docs/tracing.md`.
`Should · HARDEN · 3 d · 5 pts · Deps: E10-S1 · Arch ✔ §10.4, §14 #9 · DoD-DEPLOY`
- [x] One trace id follows a document from `POST /api/jobs` through every stage to the FHIR bundle
  - `cdi_adapter.tracing` (OpenTelemetry SDK 1.44.0, OTLP/HTTP). `POST /api/jobs` starts a `job` span and returns its `trace_id` (also in `GET /api/jobs/{id}`).
  - Every stage of every document is a child span: `stage.<name>` with `cdi.document_id`. It comes from the same `metrics.stage` timer as `cdi_stage_seconds`, so traces and metrics always agree.
  - Pool threads are given the job's context explicitly, since worker threads don't inherit it.
  - The web app sends W3C `traceparent` to the model gateway, which continues the trace (`vlm.generate` / `vlm.generate_batch` under the document's `stage.extract`).
  - The FHIR projection (`/fhir`, Generate EMR) is a later request, attached to the job's stored span context (`fhir.project`).
  - Every structlog line carries `trace_id` / `span_id`. Attributes are ids and fixed names only; no PHI.
  - Dependencies: only the 7 OTel packages (including `googleapis-common-protos`) were added to `requirements.lock` and `requirements-vllm.lock`, hash-pinned. `protobuf` stays at 7.36.2, and unrelated bumps from a fresh resolve were left out.
  - **Race found and fixed:** `JobRunner.wait()` returned when `state` left `running`, but a mismatch sets `state` while other documents are still extracting. A job now has a `finished` event, set only when its run has fully ended.
- [x] Viewable in one place (Loki/Tempo or equivalent)
  - Tempo 3.0.3 (traces) and Loki 3.7.8 (logs over OTLP, trace ids as structured metadata), pinned with sha256 and started by `start_monitoring.sh` on 127.0.0.1. Grafana links a log line's `trace_id` to Tempo, and a span to its logs in Loki.
  - Settings `CDI_OTEL_TRACES_ENDPOINT` / `CDI_OTEL_LOGS_ENDPOINT`: off by default, on in `.env.runpod`.
  - **Found on the Colab check, fixed:** Tempo 3.0 moved retention from `compactor` to `backend_worker`. Also, a Grafana already running from before did not know the new data sources, because provisioning is read only at start; the script now reloads provisioning on every run.
- **Tests:** end-to-end trace-continuity test
  - `tests/test_tracing_unit.py` (5). A 2-document job runs through the real runner, the real gateway app over HTTP, and the web app's FHIR projection. It checks:
    - one trace for everything;
    - every stage of every document under the job;
    - each gateway span under its document's extract span;
    - FHIR under the job;
    - every log line with the job's trace id and its stage's span id;
    - two jobs are two traces;
    - a shipped log record carries its trace context;
    - export is off by default;
    - no patient name in any span.
  - `tests/test_monitoring_unit.py` (+6): Tempo and Loki on loopback, OTLP ports matching `.env.runpod`, Grafana links both ways, pins and hashes, OTel in both venvs, the provisioning reload.

#### Block E10-S4 — Drift monitors ⬜
**Status:** Completed (2026-09-24). Built, tested, and deploy-checked on the Colab L4 fallback host at the owner's direction, as for E10-S1 to E10-S3. On-call: `docs/alerts.md#cdiconfidencedrift`.
`Should · HARDEN · 3 d · 5 pts · Deps: E10-S1, E3-S3 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [x] Confidence-distribution shift alert (same day)
  - New metric `cdi_fact_confidence{fact_type}`: the calibrated confidence of every fact the S6 gate decides. Its buckets have the gate lines exact (0.95 audit, 0.985 auto-accept); `fact_type` comes from a fixed list or is `other` (no PHI).
  - Alert `CdiConfidenceDrift` (ticket): the Kolmogorov-Smirnov distance between the last 6 hours and the 7 days before them is over 0.25, with at least 50 facts, for 30 minutes.
- [x] Human-override rate and unmapped-code rate as trend lines on the E10-S2 dashboard
  - New metric `cdi_terminology_bindings_total{outcome}` (`bound` / `local_only`) from S5.
  - Dashboard: 17 panels (was 13). New: KS distance with the 0.25 line, confidence quantiles p10/p50/p90, human-override rate (corrected or rejected / all reviewer decisions, 1 day), unmapped-code rate (`local_only` / all bindings, 1 day).
  - `start_monitoring.sh` now sends SIGHUP to a running Prometheus / Alertmanager, so an upgrade loads new rules without a restart.
- **Tests:** an injected distribution shift in staging triggers the alert
  - `scripts/drift_staging_check.py`: 7 days of normal history plus a same-day shift are backfilled (`promtool tsdb create-blocks-from openmetrics`) into a throwaway staging Prometheus on 127.0.0.1:9091 that loads the real rule file. The shift must make the alert active; a control without the shift must stay inactive. The live Prometheus is not touched.
  - `cdi-alerts.test.yml` (+3 promtool cases): a shift fires; a steady distribution stays quiet; a shift on too few facts stays quiet.
  - Unit (+4) and integration (+2) tests: KS values, the staging history, the rule's window and volume guard, the reload lines, the gate observes every fact, the bindings are counted.

#### Block E10-S5 — Model registry + canary/rollback ⬜
**Status:** Blocked (2026-09-24). Everything is built, tested, and drill-checked on the Colab L4 with real models. What is blocked: comparing on the **E2-S7 gold-set metrics**, because E2-S7 (and the E2-S4 gold set it needs) are not built yet. The registry already stores each version's E2-S7 report and gates promotion on it once reports exist. Runbook: `docs/model-rollout.md`.
`Should · HARDEN · 2 d · 3 pts · Deps: E2-S7 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [ ] Deploy a new model version to a small traffic percentage first; compare E2-S7 metrics live against the incumbent. Built except the E2-S7 metrics:
  - Registry: migration 0006. `model_version` stores the model id, pinned revision, backend, quantization and the E2-S7 report. `model_rollout` is an append-only log that a trigger refuses to UPDATE or DELETE; every change is also written to `audit_log`.
  - `python -m cdi_adapter.modelops` commands: `register`, `serve`, `deploy`, `canary --percent 1-50`, `set-percent`, `compare`, `promote`, `rollback`, `status`.
  - Each version runs in its own gateway process. A gateway joins the rollout only if its `/healthz` reports exactly the registered version, model and backend.
  - The web app routes by document id (`sha256 mod 100 < canary %`) and re-reads the state every 5 s. A canary that fails (unreachable or 5xx) hands the call to the primary. A batch never mixes the two sides.
  - `extraction.model_version` records the version that answered. New metrics: `cdi_model_extractions_total{role,outcome}`, `cdi_model_canary_fallbacks_total`, `cdi_model_canary_percent`. Dashboard: 2 new canary panels (19 in total).
  - `compare` works on live documents over the same window. It gates on the `_partial` rate (+5 points), failed extractions (+2), reviewer rejections (+5) and corrections (+10), with at least 20 canary documents. It adds the E2-S7 offline report when one is registered (at most 1 point worse). Blocked part: no E2-S7 reports exist yet.
- [x] One command rolls back to the previous registered version
  - `python -m cdi_adapter.modelops rollback`. With a canary deployed, it drops the canary. Otherwise it returns to the previous primary in the log, starting its gateway first when it is down, and switches only once that gateway answers as that version.
- **Tests:** canary + rollback drill
  - `tests/test_modelops_unit.py` (24 tests): routing share, cache and database outage, fallback (5xx vs 4xx), gateway identity, the gate, the CLI, the revision pin.
  - `tests/test_modelops_integration.py` (7 tests; Postgres, real gateways over HTTP, stub models): the full drill including a stopped gateway, no document lost when the canary breaks, no mixed batches, the gate, the exact-version checks, the append-only log, and a real `serve` process.
  - `scripts/model_rollout_drill.py`: the same drill with real models on the GPU; **passed on Colab**.

#### Block E10-S6 — Postgres primary + standby, MinIO multi-node ⬜
**Status:** Blocked (2026-09-24). Built and tested. Both drills a single host can run passed on the Colab L4: a Postgres failover with RPO 0 s, and a MinIO drive loss with no data lost. What is blocked: **a whole MinIO node (host) lost**, which needs MinIO on separate hosts, the same second host as E1-S3. MinIO refuses loopback multi-node inside a container, and §12 keeps every service on loopback. A standby on another host (for losing the whole pod) needs that host too. Runbook: `docs/ha.md`.
`Should · HARDEN · 4 d · 8 pts · Deps: E1-S3 · Arch ✔ §3.3, §10.3, §13, §14 #9 · DoD-DEPLOY`
- [x] Postgres failover to a standby with RPO < 1 minute (vs today's 15-minute dump)
  - `cdi_adapter.ha.pg` + `python -m cdi_adapter.ha pg setup-standby | status | failover | rejoin | drill`.
  - The hot standby streams WAL (`pg_basebackup -R`) through the physical replication slot `cdi_standby`. The replication role `cdi_repl` takes its password from the secrets store (new secret `cdi_replication_password`, kept in a postgres-owned `.pgpass-replication`).
  - **Fenced failover:** the old primary is stopped and gets `cdi.fenced`, and `bootstrap_pod.sh` refuses to start a fenced data folder (no split brain). Then the standby is promoted. `rejoin` rebuilds the old primary as the new standby and keeps its old folder aside.
  - **The app follows by itself:** new setting `CDI_DATABASE_HOSTS` with `target_session_attrs=read-write` in `db.engine_url()`, plus `pool_pre_ping`.
  - The bootstrap builds the standby when `CDI_PG_STANDBY_PORT` is set (opt-in).
  - Monitoring: `cdi_pg_standbys_streaming`, `cdi_pg_replication_lag_seconds` and `cdi_pg_replication_slots_inactive`. Alerts `CdiPgReplicationLag` (page, above 30 s) and `CdiPgStandbyDisconnected` (ticket).
  - On one host this covers a crashed or damaged database. Losing the whole pod needs the standby on a second host.
- [ ] MinIO survives one node loss without data loss. **Blocked on a second host.** One host is covered, and multi-host is built:
  - `cdi_adapter.ha.minio`, `CDI_MINIO_MODE=erasure`: one MinIO process on 127.0.0.1 over 4 erasure-coded drives, protecting against a lost or corrupted drive.
  - `CDI_MINIO_MODE=cluster`: one process per host of `CDI_MINIO_ADDRESSES`, protecting against a lost node. This needs real hosts.
    - At the pinned release (`cmd/endpoint.go`), MinIO never treats `127.0.0.1` as a node when `/.dockerenv` exists, as on the pod and Colab.
    - Binding to a non-loopback address would break §12; the owner said to follow the architecture.
  - The app's `ObjectStore` takes a comma-separated `CDI_S3_ENDPOINT_URL`, one client per host, and a node that does not answer (or answers 503) passes the call to the next one.
  - `minio migrate` copies every object into the erasure set and reads each copy back.
  - Monitoring: `cdi_minio_drives` and `cdi_minio_drives_offline` (MinIO's own gauges on loopback), `cdi_minio_nodes` and `cdi_minio_nodes_online`. Alert `CdiMinioDegraded`.
- **Tests:** failover drill; node-loss drill
  - Postgres: `tests/test_ha_integration.py` runs the real drill with PostgreSQL 16 servers: crash the primary, fail over, measure RPO and RTO, rejoin. `python -m cdi_adapter.ha pg drill` runs the same on Colab.
  - MinIO: `tests/test_ha_unit.py` covers failover in the object store, the command lines, the collectors, fencing in the bootstrap and the CLI. The drive-loss drill passed on Colab.
  - The node-loss drill needs the second host.
  - 3 alerts with a firing and a quiet promtool case each (4 cases for `CdiMinioDegraded`).

#### Block E10-S11 — Restore / DR drill with a measured RPO and RTO ➕ ⬜
**Status:** Blocked (2026-09-24). Everything is built, and the restore drill **passed on the Colab L4** with real PostgreSQL 16 and a real MinIO: RPO 38.6 s, RTO 8.5 s, every scan byte-identical, golden path green. What is blocked: two items that depend on other blocks. **E1-S6** is Blocked (the golden path `--mode full` needs its drift explained, so the drill runs `--mode seeded`). **E14-S5** is Pending, and data-tier loss is now in its list. Also, a backup that survives losing the pod needs `CDI_BACKUP_DIR` on the second host (E1-S3 / E10-S6). Runbook: `docs/dr.md`.
`Must · BLOCKER · 2 d · 3 pts · Deps: E10-S6 · Arch ✔ §3.3 / §10.3 (the pg_dump + auto-restore persistence model), §13 "HA/DR: single pod" · DoD-DEPLOY`
E10-S6 *promises* RPO < 1 min. Nothing in this backlog ever restores anything, and the whole
documented persistence model is a 15-minute dump replayed at boot.
- [x] Rehearsed restore of Postgres **and** the MinIO scan bytes into a clean environment, from backups alone
  - `snapshot.sh` (every 15 min) now also backs up the objects: `python -m cdi_adapter.dr backup` copies every new MinIO object to `objects/<key>`, checks each by sha256, and writes the manifest `objects.json`. It then writes `last_backup.json`; a failure is recorded too, through a `trap`.
  - `python -m cdi_adapter.dr drill` starts from empty servers and uses the backup folder alone:
    1. `pg_restore --exit-on-error` of the newest good dump;
    2. `alembic upgrade head`;
    3. every object restored, checked against its sha256;
    4. **every document's original scan checked against `source_document.sha256`**;
    5. the golden path.
- [x] Measured RPO and RTO published against the documented targets; the golden-path suite (E1-S6) passes against the restored environment
  - Targets are in `docs/dr.md`: **RPO 20 min** (the 15-min cadence plus the dump) and **RTO 30 min**. They are proposed for the owner to confirm.
  - The report goes to `$WS/logs/dr/restore-<time>.json` and `last.json`, and to `/metrics` (`cdi_dr_rpo_seconds`, `cdi_dr_rto_seconds`, `cdi_dr_restore_last_ok`, `cdi_backup_last_success_timestamp_seconds`), plus a Grafana "Backups" panel.
  - The golden path runs as `--mode seeded` on the restored server. `--mode full` is E1-S6's own open drift.
- [x] Runbook entry for data-tier loss, stating which derived rows are reproducible by re-running the pipeline and which are not
  - `docs/dr.md` covers the procedure and a table of every table.
  - Reproducible by re-running the pipeline: OCR, classification, extraction, machine facts, FHIR, AI events.
  - Not reproducible after the snapshot: reviewer decisions, canonical EMR edits, `audit_log`, registry and identity links, visits and billing, ABDM consents, web users, the model registry.
- [x] Add data-tier loss to E14-S5's failure-injection set: done in E14-S5's "Failure runs" line, pointing at this drill.
- **Tests:** the restore drill itself, re-run on a schedule; a backup that fails to restore raises an alert
  - **Scheduled:** the bootstrap's crontab runs `python -m cdi_adapter.dr drill` weekly (Sunday 03:30).
  - **Alerts:** `CdiRestoreDrillFailed` (page) when the last drill failed; `CdiBackupStale` (page) when there has been no good backup for 45 min; `CdiRestoreDrillOverdue` (ticket) when no drill has run for 8 days. Each has promtool cases.
  - `tests/test_dr_integration.py` (real PostgreSQL 16) checks that:
    - the drill rebuilds from a `pg_dump` plus object backups, 4/4 scans intact, golden path green;
    - a corrupted object backup fails the drill;
    - a truncated dump fails the drill.
  - `tests/test_dr_unit.py` has 14 tests.

#### Block E10-S9 — Application-tier HA: multi-instance webapp/mlserve ⬜
**Status:** Completed (2026-09-25). Built, tested, and drill-checked on the Colab L4 with real PostgreSQL, MinIO, Redis and two real model gateways. This block could not work without its dependency E9-S1 (jobs lived in one process's memory), so E9-S1's core was built here: jobs are durable and run on the Celery queue. E9-S1 is now In Progress; its migrate-under-load item is still open. Design chosen from the architecture (owner delegated the choice): Caddy, the architecture's ingress, as the balancer; Celery + Redis for the jobs; the job in PostgreSQL with a lease. Runbook: `docs/ha.md` "Application tier".
`Should · HARDEN · 4 d · 3 pts · Deps: E9-S1 · Arch ◐ §12/§13 "HA/DR: single pod" · DoD-CODE`
- [x] webapp runs as 2+ stateless instances behind health-aware traffic distribution
  - `infra/runpod/start_apptier.sh` starts `CDI_WEBAPP_INSTANCES` (2) web app instances on 127.0.0.1:8881+ and the pinned Caddy (`Caddyfile.lb`, the same release as the E1-S9 ingress, sha512-checked) on the public port 8888. `start_all.sh` calls it.
  - Health-aware: an active check of the new `/readyz` (database, object store, queue; a content marker, every 2 s, 2 s timeout), plus a passive check that removes an instance on its first failed connection. A request whose connection fails goes to another instance.
  - Stateless: jobs are in the new `web_job` table (migration 0007); sessions already were (E11-S1). An upload is staged in MinIO (`uploads/<job>/<n>`) and written to `web_job` **before** the 202. If either fails, the answer is 503 and nothing is kept.
  - Jobs run on `CDI_WORKERS` (2) Celery workers, queue `cdi-web`, threads pool (one runner per worker shares the GPU slots, E2-S2). A runner claims a job with a lease (`CDI_JOB_LEASE_S` 30 s), saves it at every step, and renews the lease from a background thread. A sweep in every runner takes over jobs whose lease ran out (the runner died) or that nobody claimed (the queue message was lost), and resumes them: finished documents stay, a document past extraction continues from there, the rest start again from the staged files. A runner whose lease was taken stops at its next step (`LeaseLost`). A job that keeps killing its runner is stopped after 3 attempts.
  - Ingest runs one at a time per file hash (advisory lock) and renders again the pages an interrupted ingest never stored.
  - mlserve: `CDI_MLSERVE_URL` (and a model-registry rollout side, E10-S5) can list several replicas. A replica that refuses the connection, drops it, or answers 502/503/504 is skipped for 15 s, and the call goes to the next one.
  - Monitoring: `cdi_jobs` is now read from `web_job` (every instance reports the same numbers; take `max`). New: `cdi_jobs_orphaned`, `cdi_job_resumes_total`, `cdi_mlserve_failovers_total`. Prometheus finds each instance's and worker's `/metrics` through target files. Alerts `CdiWebappInstanceDown` (ticket) and `CdiJobsOrphaned` (page); dashboard panel 23.
- [x] A killed instance leaves rotation within the health-check window with zero dropped acknowledged uploads
  - Colab drill (below): out of rotation **1.9 s** after SIGKILL (window 4 s); **0 of 12** acknowledged uploads lost.
- **Tests:** kill-instance drill under load
  - `scripts/app_ha_drill.py` **passed on Colab**. 12 synthetic patients (28 scans, E1-S7 corpus) uploaded through the balancer over 20 s; SIGKILL of web app `web1` at 6 s, then of job worker `worker1` and model gateway replica 8077 at 14 s. 12/12 uploads got 202 (3 by `web1` before it died, 9 by `web2`); 6,143 probe requests through the balancer during the drill, **0 failed**; **12/12 jobs finished at review, 28/28 documents done**; 1 job taken over from the killed worker (attempts 2); gateway calls moved to the live replica; one extraction row and one open review item per document; `web1` back in rotation 5.4 s after `start_apptier.sh`.
  - `tests/test_apptier_unit.py` (28): lease, resume, take-over, lost lease, give-up, the sweep, refused uploads, replica failover, `/readyz`, the balancer config, the scripts.
  - `tests/test_apptier_integration.py` (7, real PostgreSQL; 2 also need real MinIO): the lease rules; an upload MinIO cannot store is refused with 503 and leaves nothing; any instance reads a job; **a runner killed inside an extraction is taken over with no loss and no doubles**; the ingest re-render.
  - Limits, recorded in `docs/ha.md`: a runner that is frozen (not killed) past its lease can finish the one stage it was in; the same upload sent twice makes two jobs (idempotency keys are E9-S3); losing the whole host needs the second host (E10-S6, E1-S3).

#### Block E10-S10 — Credential rotation drill (zero-downtime via rolling restart) ➕ ⬜
**Status:** Pending
`Should · HARDEN · 1 d · 2 pts · Deps: E1-S5, E9-S1, E10-S9, E11-S3 · Arch ◐ §12 Secret hygiene · DoD-DEPLOY`
Rotate a live credential without dropping in-flight work. Deliberately scoped as a **drill on the
durable queue**, not as in-process secret hot-reload: once E9-S1 and E10-S9 are in, a rolling
restart achieves the same result without the connection-pool bugs hot-reload invites.
- [ ] Runbook: rotate DB / MinIO / vault credentials by rolling restart across the E10-S9 instances
- [ ] Drill executed during an active E1-S7 load run — zero lost acknowledged uploads, zero duplicated facts, jobs resume from the E9-S1 queue
- [ ] Rotation window measured against the 5-minute SLA
- [ ] Decision recorded: build true in-process hot-reload **only if** the drill misses the SLA window
- **Tests:** the rotation drill under load; an old-credential connection is proven rejected after rotation

#### Block E10-S8 — End-to-end SLO, saturation and cost telemetry
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E10-S1, E9-S7, E7-S7 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 + [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖ (user SLA/cost mandate)) · DoD: SLIs documented, tested with synthetic traffic, referenced by the cutover checklist; no PHI in metric labels`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] p50/p95/p99 for machine-ready and governed-ready latency; per-stage queue age; GPU route fraction; GPU-seconds/doc; reviewer overrides; deadline misses
- [ ] Alerts on SLO burn, sustained saturation, abnormal escalation fraction, reviewer backlog
- [ ] Segmentable by doc_type without exposing patient data
- **Tests:** synthetic-traffic SLI test; PHI-in-labels check

---

# EPIC E11 — Security, AuthN/AuthZ & DPDP Compliance

### Phase 1

#### Block E11-S1 — Web-app AuthN/AuthZ ⬜
**Status:** Completed (2026-09-22). Built, tested and verified on real PostgreSQL and on the running web app. Design, chosen from the architecture (owner delegated the choice): the architecture names no identity provider and none exists, so accounts are local to the app. A clinic SSO (OIDC) can replace the password check later without changing roles or policy. Runbook: `docs/auth.md`.
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §12 (top row), §13, §14 #9 · DoD-SEC`
- [x] Roles: intake staff / clinician reviewer / admin.
  - `intake` (the counter): upload, follow the job, FHIR download, registry.
  - `reviewer` (clinicians): `/review`, `/reviewer` and their APIs, facts, *generate* (a clinical decision, §7.2), documents.
  - `admin`: everything, including `/admin`.
  - Accounts live in `web_user` (migration 0005) and are managed with `python -m cdi_adapter.webapp.users` (add / passwd / role / disable / enable / unlock / list; passwords from a hidden prompt or stdin, never argv). Every change is audited.
- [x] Every route in the §7.1 endpoint table requires an authenticated session with a role check.
  - One global dependency `webapp/auth.authorize` covers every route, with the explicit fail-closed `POLICY` table (31 routes; public: only `/login` and `/healthz`).
  - Sessions are server-side (`web_session` holds the token sha256 only; HttpOnly + Secure + SameSite=Strict; 30 min idle / 12 h max).
  - Passwords: salted scrypt with constant-time compare; lockout after 5 failures for 15 min.
  - Writes need a same-origin `Origin`/`Referer`; `next` is never an open redirect; `/docs` and `/openapi.json` are off.
  - Sign-in, failure and sign-out are audited, and every page has a Sign out button.
- **Tests:** an unauthenticated request to any `/api/*` route is rejected (negative test); role-check tests.
  - `test_auth_unit.py` (38): the negative test runs over **every** protected route (29, including those in included routers), plus policy coverage both ways, CSRF, open redirect, passwords and docs off.
  - `test_auth_integration.py` (28): real PostgreSQL. Role matrix, lockout, idle and absolute expiry, disable ends the session, sign-out invalidates a copied cookie, audit rows, user CLI.
  - Live run of the native web app with `curl`: 15 checks, all correct.

  Knock-on changes: the E1-S9 Caddy health check and `start_all.sh` use `/login` (the only public page with the marker); the E1-S7 load test signs in (`--user` plus `CDI_LOAD_PASSWORD`); `start_all.sh` warns while no account exists. The web test clients now sign in (`tests/helpers.signed_in`).

  Not here: the S8 reviewer name still comes from the request body, and binding it to the session is E7-S1.

#### Block E11-S4 — De-identification pipeline for training/eval data ⬜
**Status:** Completed (2026-09-22). Built, tested with the real models, and red-teamed. The models were chosen from the architecture ("Presidio + clinical NER", §12; owner delegated the choice). Runbook: `docs/deid.md`.
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §12 De-identification · DoD-SEC`
- [x] Presidio + clinical NER strips direct identifiers before any document is used in E2-S4/E2-S5.
  - **Detection:** `cdi_adapter.deid` = Presidio 2.2.364 + spaCy `en_core_web_lg` 3.8.0 + clinical NER `obi/deid_roberta_i2b2` (MIT, pinned commit `78f2152e`, local files only), plus Indian recognizers: ABHA number and address, Aadhaar with Verhoeff check, PAN, phone (OCR-glued too), labelled PIN, MRN/UHID values, MPI, `Address:` value, e-mail, dates.
  - **Replacement:** consistent per-document surrogate names; a per-document date shift that keeps format and intervals; typed placeholders; ages over 89 become 90+; JSON identity fields are always replaced. Nothing reversible is stored.
  - **Images:** identifier OCR lines are blacked out, then the image is re-OCR'd (no confidence cut-off) and cleaned again, up to 3 passes. Lines caught only on the image are removed from the text too.
  - **Kept for training use:** form labels, durations, prescription abbreviations, lab values and ranges.
  - **CLI:** `python -m cdi_adapter.deid scans|text|fetch-models`.
  - **On the pod:** the `deid` extra and the spaCy model (URL + sha256) are in `requirements.lock` (121 packages; numpy 2.4.6, which Presidio needs). The NER model is fetched by commit.
- **Tests:** a red-team re-identification attempt on a sample fails. **It fails, as required.**
  - `scripts/deid_redteam.py` builds synthetic scanned registration forms, OPD notes and lab reports with answer keys, carrying every identifier kind.
  - The attacker knows the truth and searches the de-identified text, the answer key and the redacted image re-OCR'd. It looks for ID digits (also the last 6), name tokens (also with one typo), DOB forms, addresses and e-mails.
  - The control, the same attack on the original, must find the identifiers.
  - **Final: 4 samples, 96 documents (seeds 11 / 29 / 5 / 47; 47 never inspected): every control found identifiers, 0 leaks.**
  - Earlier runs found 10 and then 5 real leaks: OCR-glued text, lines OCR dropped as low confidence, a split Aadhaar, a DOB swallowed by an "age" hit, a partial address. Each fix has a unit test. Two red-team false alarms were fixed in the attacker itself (`Hills`~`chills`; digits joined across separate numbers).
  - `tests/test_deid_unit.py` (24, no models) and `tests/test_deid_models.py` (2, real models, `CDI_TEST_DEID_MODELS=1`).
  - Limits, stated plainly: the sample is synthetic (no real documents exist here), and a person still reviews a sample before a corpus is released (the E2-S4 🔒 approval).

### Phase 6

#### Block E11-S6 — Rate limiting + basic WAF rules ⬜
**Status:** Pending
`Must · BLOCKER · 1 d · 2 pts · Deps: E11-S1 · Arch ✔ §13 (rate limiting); ◐ WAF · DoD-SEC`
- [ ] Protect `POST /api/jobs`: a burst above the configured rate gets 429, not a crash or queue flood
- **Tests:** E1-S7 harness at 2× its configured concurrency

#### Block E11-S2 — mTLS between internal services ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E1-S3 · Arch ✔ §12 Transport · DoD-SEC`
- [ ] webapp↔mlserve and webapp↔Postgres/MinIO/Redis use mTLS when deployed across more than one host
- [ ] Plaintext localhost stays acceptable only for a single pod
- **Tests:** a plaintext connection is refused in multi-host mode

#### Block E11-S3 — Encryption at rest + KMS ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: — · Arch ✔ §12 At rest · DoD-SEC`
- [ ] Postgres data directory, MinIO buckets and the periodic DB dump encrypted with KMS-managed keys
- [ ] A documented, tested key-rotation procedure
- **Tests:** rotation drill; an unencrypted read attempt fails

#### Block E11-S5 — DPDP Act 2023 workflow ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 5 d · 13 pts · Deps: — · Arch ✔ §12 DPDP · DoD-LEGAL`
- [ ] Consent capture
- [ ] A purpose-limitation tag on every stored fact
- [ ] Documented retention policy with automatic enforcement
- [ ] Data-principal access/erasure request handler
- **Tests:** access-request export; erasure request; retention enforcement
- 🔒 Legal / DPDP compliance sign-off (Risk R6)

#### Block E11-S7 — Retention enforcement, crypto-shredding & archival tumbling ➕ ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 4 d · 5 pts · Deps: E11-S3, E11-S5, E10-S7 · Arch ◐ §12 DPDP (names retention and data-principal rights, not the mechanism) · DoD-LEGAL`
E11-S5 states the policy. This executes it — and resolves its collision with the immutable
audit/provenance chain, which nothing else in this backlog addresses.
- [ ] MinIO lifecycle rules: originals + page renders tumble to cold storage at T1 and are securely destroyed at T2 (statutory retention), per bucket class
- [ ] Erasure is **crypto-shredding** (destroy the per-patient data key from E11-S3's KMS) plus a tombstone row — **no `DELETE`** against `fact_provenance`, `audit_log` or the E10-S7 WORM copy
- [ ] A written precedence rule for erasure vs. E10-S7 WORM retention vs. §4.3 medico-legal provenance, signed off by legal
- [ ] Bundles that inlined the scan (§5 S9 `DocumentReference.content.attachment.data`) are enumerated on erasure and re-projected or tombstoned — deleting the MinIO object alone does not erase those copies
- [ ] Bundles already pushed to an HIU (E8-S4) are reported as out-of-scope-for-erasure in the data-principal response, never silently claimed as erased
- **Tests:** shred test (post-shred read fails, the audit hash row survives); lifecycle simulation across T1/T2; E10-S7's deletion-rejection test still passes; an erasure request produces a complete and honest disposition report
- 🔒 Legal sign-off on the erasure-vs-retention precedence rule (Risk R6)

---

# EPIC E13 — Cost Optimization / Cheapest-Viable-GPU Path

> Lifted from ⛔ HOLD by E1-S8 (2026-09-21): ARCHITECTURE.md §15.3 + ADR-0004 now carry the GPU-class
> and cost policy. (Previously: the Excel's *Cost Model* sheet only; no cost content in ARCHITECTURE.md.)

### Phase 5

#### Block E13-S1 — Benchmark the current 7B on the Blackwell 32 GB baseline → real ₹/document
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E1-S7 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖) · DoD-EVAL`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Measure docs/minute/GPU and extract-seconds/doc at concurrency 1, 2, 4, 8 for the **current Qwen2.5-VL-7B (bf16) on the Blackwell 32 GB** production baseline (decision 2026-09-20); re-run on a quantized/fine-tuned model only if E2-S6 lands
- [ ] Write the results into *Sizing & Capacity* (GPU strategy rows, currently 30–32), replacing the estimates, and derive ₹/document and ₹/patient-visit against the ₹75,000/month envelope (§2.2)
- **Tests:** benchmark report archived

#### Block E13-S2 — Evaluate a smaller/cheaper GPU class for a quantized model (optional — baseline is Blackwell)
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E13-S1 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (cost lever 4) (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
**Deferred optimisation.** The production baseline is the Blackwell 32 GB GPU running the current model; this block only runs if a quantized model (E2-S6) exists and the ₹75,000/month envelope needs more headroom.
- [ ] Benchmark the quantized DSLM on at least one 16–24 GB class (L4/A10-class or workstation)
- [ ] Update the *Cost Model* GPU-class column with the recommended class and its quoted $/hour
- **Tests:** benchmark vs SLA

#### Block E13-S3 — Scale-to-zero / warm-standby scheduling tied to OPD hours
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E9-S4 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Outside documented OPD hours the GPU pool drops to the off-peak floor (0 or 1 warm), and comes back up before opening, with no manual steps
- **Tests:** schedule simulation across an open/close cycle

#### Block E13-S4 — On-demand base + spot/interruptible burst evaluation
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E9-S3, E9-S4 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (cost lever 5) (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] A simulated burst scales beyond the on-demand floor onto interruptible capacity
- [ ] A forced preemption loses zero documents and requeues unfinished work; the on-demand floor keeps serving
- [ ] SLA impact measured; spot-only serving during OPD explicitly rejected unless proven
- **Tests:** preemption drill

#### Block E13-S6 — Two-GPU topology on Blackwell: protected Voice GPU + escalation-only CDI GPU, ₹75k budget guard ➕ 🔒 ⬜
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E13-S1, E13-S3, E9-S4 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
Replaces the earlier "one shared GPU" idea in the workbook with the topology in the target diagram: hard isolation, two GPUs, CDI GPU used only for what fails confidence gate 1.
- [ ] The Voice GPU never runs CDI work (no shared VRAM, compute or scheduling)
- [ ] The CDI GPU is scheduled to upload hours and scales to zero outside them (E13-S3). Until E2-S9 lands every document reaches it (cost scenario B, §2.2); after E2-S9 only gate-1 failures do (scenario A)
- [ ] Monthly spend is tracked against the ₹75,000 envelope with an alert at 80%
- [ ] Combined-load test: 30 live Voice sessions + a 30 × 10 upload burst — Voice p95/p99 does not regress
- **Tests:** the combined-load test; a schedule simulation across open/close
- 🔒 Voice-AI project owner sign-off (separate codebase)

### Phase 7

#### Block E13-S5 — Publish the Cost Model as a living dashboard
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E10-S1, E13-S3 · Arch ✔ [ADR-0004](adr/0004-cost-and-gpu-policy.md) §15.3 (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] Compare actual GPU-hours/day and cost/patient-visit (from E10) monthly against the Cost Model
- [ ] A > 20% variance triggers a re-sizing review
- **Tests:** variance-trigger test on synthetic data

---

# EPIC E14 — Shadow-Mode Pilot & Production Cutover

### Phase 9

#### Block E14-S5 — 30 × 10 production-readiness soak + failure-injection gate 🔒
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E9-S7, E10-S8, E7-S7, E6-S6 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 (E1-S8 re-score; was ✖ (exact user load requirement)) · DoD: signed production-readiness report with raw load-test artifacts, SLO graphs, failed-case evidence, rollback decision and explicit go/no-go approval`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] ≥ 3 repeated 30 × 10 runs (mixed typed/image/handwritten, concurrent review, realistic retries) meet the configured p95 SLA
- [ ] Zero lost acknowledged uploads, zero duplicate final facts, zero schema-invalid ready bundles, zero illegal auto-accepts
- [ ] Failure runs: web restart, worker restart, one GPU loss, terminology outage, one poison document, **data-tier loss** (added by E10-S11: Postgres and its standby lost, restored from the backups alone with `python -m cdi_adapter.dr drill` / `docs/dr.md`, with the measured RPO and RTO in the report)
- [ ] Report reviewer-capacity results against the Sizing sheet. **Informational, not a gate** (decision 2026-09-20): a reviewer shortfall leaves facts held with an ETA at the counter; it does not block rollout
- **Tests:** the soak + failure-injection suite
- 🔒 Clinical + operations go-live approval

#### Block E14-S1 — Shadow-mode run on real historical documents ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 5 d · 8 pts · Deps: E4-S6, E6-S4, E7-S6, E11-S4 · Arch ✔ §14 #10 · DoD-DEPLOY`
- [ ] A representative batch of real, de-identified (E11-S4) historical documents runs through the full pipeline
- [ ] Clinicians adjudicate every held fact; nothing is patient-facing
- **Tests:** a run report with adjudication coverage
- 🔒 Clinician adjudicators + de-identified real documents

#### Block E14-S2 — Go/no-go checklist against eval thresholds ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E14-S1 · Arch ◐ §14 #10 · DoD-LEGAL`
- [ ] Score a written go/no-go checklist against shadow-mode gate outcomes, FHIR validity (E6) and the E2/E4 thresholds
- [ ] A "no-go" blocks E14-S3 until the failing metric is fixed and re-tested
- **Tests:** checklist scored and signed
- 🔒 Clinical / operations go/no-go sign-off

#### Block E14-S3 — Staged rollout (1 counter → all counters) ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E14-S2 · Arch ◐ §14 #10 · DoD-DEPLOY`
- [ ] Stage 1: one counter, real patients, for an agreed period
- [ ] Update Sizing assumptions (rows 5–11) with observed values before stage 2 (remaining counters)
- **Tests:** stage-1 observation report
- 🔒 Counter operations

#### Block E14-S4 — Post-cutover hypercare (2 weeks) 🔒
**Status:** Pending
`Must · BLOCKER · 5 d (part-time over 2 weeks) · 8 pts · Deps: E14-S3 · Arch ◐ §14 #10 (shadow-mode pilot before trust); a 2-week hypercare window itself is not specified (E1-S8 re-score; was ✖) · DoD-DEPLOY`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ◐. External 🔒 dependencies, if any, are unchanged.
- [ ] Daily review of every `corrected`/`rejected` fact for the first 2 weeks; findings logged
- [ ] At least one calibrator refresh (E4-S2) triggered from real post-cutover data
- **Tests:** daily log + calibrator refresh record
- 🔒 Clinical reviewer availability

---

## 8. Traceability

| Epic | Blocks in this file | Done | In Progress | ⛔ HOLD | ➕ Added |
|---|---|---|---|---|---|
| E1 Foundation & Engineering Baseline | 10 | — | — | — | S8, S9, S10 |
| E2 S4 Extraction Quality & Serving Acceleration | 5 | S1, S2, S3 | — | — | — |
| E6 FHIR/ABDM IG Validation & Correctness | 1 | — | — | — | — |
| E7 Human-in-the-Loop / Counter-Side Review Hardening | 1 | — | — | — | — |
| E9 Scale, Queueing & Autoscaling for 30-Concurrent Load | 8 | — | — | — | S8 |
| E10 Observability, Ops & Governance | 11 | — | — | — | S11, S10 |
| E11 Security, AuthN/AuthZ & DPDP Compliance | 7 | — | — | — | S7 |
| E13 Cost Optimization / Cheapest-Viable-GPU Path | 6 | — | — | — | S6 |
| E14 Shadow-Mode Pilot & Production Cutover | 5 | — | — | — | — |
| **Total** | **54** | 3 | 0 | 0 | 8 |

### 8.2 ARCHITECTURE.md §14 roadmap → blocks in this file

| §14 item | Blocks |
|---|---|
| #1 Harden + accelerate S4 (serving half) | E2-S1, S2, S3, S6, S8 |
| #8 Scale | E9-S1…S8 |
| #9 Ops & governance | E10-S1…S11, E11-S1, S2, S3, S5, S6, S7 |
| #10 Shadow-mode pilot | E14-S1…S5 |
| §12 security posture rows | E1-S5, E7-S1, E10-S7, E11-S1…S7, E10-S10 |
| §13 HA/DR gap | E10-S6, E10-S9, E10-S11, E1-S9 |
| §3.3 ephemeral pod / restore-from-dump | E1-S9, E1-S10, E10-S11 |
| Cost / GPU policy (user mandate) | E13-S1…S6 |
| §4.5 canonical EMR absent from the repo copy | E1-S8 |

---

## 9. Change log

| Date | Block | Change | Tests |
|---|---|---|---|
| 2026-09-19 | — | BACKLOG.md rebuilt only from `CDI-Adapter-Production-Backlog-Enhanced.xlsx` (91 stories / 14 epics / 9 phases); each block checked against `docs/ARCHITECTURE.md`. | not run (no code changed) |
| 2026-09-20 | E1-S8, E1-S9, E2-S10, E7-S10, E9-S8, E10-S10, E10-S11, E11-S7 | 8 blocks added from an end-to-end production-reliability review. | not run (no code changed) |
| 2026-09-20 (rev 2) | all | **Split into feature and infra files** (49 + 54 blocks). Applied decisions: Blackwell baseline, ₹75k/month, reviewer pool not a blocker. Resequenced E11-S1, E11-S4, E10-S7, E6-S8, E4-S6, E13-S5 to clear every phase-dependency conflict. Added E15-S1, E15-S2, E1-S10, E13-S6; retargeted E13-S1, E13-S2; CPU-by-default critic task on E4-S4/E12-S3; drug/dose HITL flag on E7-S5. | not run (no code changed) |
| 2026-09-21 | all | `**Status:**` line added to all 54 blocks; E2-S1/S2/S3 reset from ✅ to Pending (code not provided). Phase 0 (Epic E0, `backlog 1.md`) runs first. | not run (no code changed) |
| 2026-09-21 | E1-S8 | **Blocked (awaiting owner sign-off, DoD-LEGAL).** All five tasks done: live `pg_dump --schema-only` → `docs/schema/cdi_schema.sql` (37 tables, 5 views); ARCHITECTURE.md §3.1/§4.1/§9/§13 reconciled, new §4.6 (canonical layer from the live schema), §15 (accepted mandates + state machine), §16 (scheduled reconciliation pass); ADR-0002…0005 accepted. HOLD re-score: 30 → 5 — lifted ✔ E1-S7, E1-S10, E9-S6, E9-S7, E10-S8, E13-S1…S6, E14-S5; lifted ◐ E9-S8, E14-S4 (this file: 14 → 0). | not run (docs only); re-score script asserted every HOLD block mapped (25 lifted, 5 kept) |
| 2026-09-21 | E1-S1 | **Blocked (needs a git host).** `scripts/ci.py` gate (ruff lint, ruff format, mypy, `pytest -m "not integration"`; every stage runs, exit 0 only if all green), `.github/workflows/ci.yml` (push + PR), `make ci`, `docs/ci.md`, README CI section; `pyyaml` added to the `dev` extra. The gate's first run caught an E1-S8 regression (`patient_registry` row added to ARCHITECTURE §4.1 broke the 20-table schema contract) — fixed by moving it into the §4.1 reconciliation note. | `tests/test_ci_unit.py` 7 passed; full gate GREEN (419 passed, 94 deselected); a deliberately failing test in `tests/` → gate RED (exit 1), removed → GREEN (exit 0) |
| 2026-09-21 | E1-S2 | **Blocked (needs a real MinIO + git host).** `ci.py --suite integration` (stages `integration`, `pipeline-audit`); `scripts/ci_pipeline_audit.py`; `CDI_TEST_REQUIRE_INFRA` in `tests/conftest.py`; `integration-gate` workflow job with native PostgreSQL 16 + MinIO (pinned tag, no containers); `pipeline_smoke.py` rows now carry `document_id`; `docs/ci.md` section. | `test_ci_integration.py` 5 passed (local Postgres); `test_ci_unit.py` 11 passed; unit gate GREEN (423 passed); full suite 511 passed / 11 skipped (MinIO); integration suite locally RED by design: 88 passed, 11 errors = the MinIO tests failing instead of skipping, audit cannot reach :9000. One run also had 1 Postgres `ConnectionTimeout` (11.5 min run); not reproduced in 2 reruns (61 s, 67 s) — logged as a local flake to watch. |
| 2026-09-21 | E1-S5 | **Blocked (owner key rotation + pod run).** `src/cdi_adapter/secretstore.py` (init/check/list/rotate/get/pgpass/redis-conf; stdlib-only), `get_settings()` loads the store and refuses a group/other-readable or missing one; credentials `repr=False`; new setting `CDI_SECRETS_DIR` (ARCHITECTURE §9, §12 updated); `.env.runpod` credential-free; `bootstrap_pod.sh` step 0 (store → `/run/cdi-secrets`, role password via stdin, Redis `requirepass`, MinIO creds from the store), `snapshot.sh` via `PGPASSFILE`; `scripts/secret_scan.py` + `secrets` CI stage; `docs/secrets.md`. Known limit recorded: persistent copy on MooseFS `/workspace` is 0777. | `test_secrets_unit.py` 29 passed / 1 skipped (0600 mode check runs on Linux only); end-to-end on local PostgreSQL: role with a store-generated password connected via `get_settings()`, wrong password rejected; secret scan CLEAN (was 11 findings); `bash -n` on all 4 pod scripts; CI gate GREEN (450 unit); full suite 540 passed / 12 skipped. Git-history scan: n/a (not a git repository). |
| 2026-09-21 | E1-S6 | **Blocked (full run needs MinIO; nightly needs the GPU pod).** `scripts/golden_path.py` (modes `full` S1→S9 / `seeded` S2→S9; `--fresh-db` resets a dedicated DB and refuses the live one; drift → exit 1 + alert log + JSON report), `tests/golden/anjali_das.json` (baselines `stub` 21 facts / 37 resources and `real_vlm` = §8.2 15 / 35), CI integration stage `golden`, `infra/runpod/golden_nightly.sh` + cron, ARCHITECTURE §8.2 note, `docs/ci.md` section. Findings: (1) the stub gives 21 facts, not the 15 in this block — the vitals sheet yields 7 per §5 S4 vs the 7B's 1 (§13); (2) resource counts depend on DB state (shared Patient/Organization attributed to the first document) — hence the fresh-DB rule. | `test_golden_unit.py` 9 passed (incl. the `real_vlm` baseline parsed from §8.2 text); `test_golden_integration.py` 5 passed on PostgreSQL (incl. drift detection on a real run and a repeat-run drift proof); seeded CLI 5/5 OK across random audit samples; 2 fresh-DB runs identical (21/21/37/3/0); `bash -n` on 5 pod scripts; CI gate GREEN (461 unit); full suite 554 passed / 12 skipped. |
| 2026-09-21 | E1-S3 | **Blocked (needs a non-RunPod Ubuntu host).** Owner decision: native fallback, no Docker (backlog said "containerize"; TECH_STACK lists compose only as alt-deploy). `infra/runpod/lib.sh` (`host_kind`, `live_url`), `start_all.sh` prints the host's address + a no-auth port warning on a fallback host, `bootstrap_pod.sh` installs torch 2.8.0 / torchvision 0.23.0 cu128 (App. B) when not inherited and the backend is `hf`; `docs/fallback-host.md` runbook (data copy, start, lock port 8888, verify with the golden path); ARCHITECTURE §3.3 + App. A notes. Off-pod data copies are E10-S11. | `test_fallback_unit.py` 7 passed (lib.sh executed in real bash: RunPod vs fallback URL; torch pins parsed from App. B; no `docker` in any lifecycle script); `test_ops_unit.py` now also syntax-checks `golden_nightly.sh`; `bash -n` on 6 scripts; CI gate GREEN (469 unit). |
| 2026-09-22 | E1-S4 | **Blocked (acceptance run needs the pod).** New `infra/runpod/versions.env` holds all pins: Ubuntu snapshot `20260920T000000Z` + 9 exact apt versions, Python 3.12, Go 1.27.1 + sha256 (mandatory), MinIO `RELEASE.2025-10-15T17-29-55Z` @ `9e49d5e7`, torch/torchvision cu128, transformers 5.16.1. It is sourced after `.env`. New `infra/runpod/requirements.lock`: 97 packages with hashes, from `scripts/lock_pod_deps.py`, which resolves for Linux cp312 with the pod's PEP 508 markers because a plain Windows resolve pulled in `colorama`. Torch family excluded; `make lock`. `bootstrap_pod.sh` changes: apt from the snapshot only; pip `--require-hashes --no-deps` plus `-e --no-deps --no-build-isolation`; no `pip --upgrade`; Go replaced when its version differs; MinIO commit verified; `GOTOOLCHAIN=local -mod=readonly`; step 7 runs `python -m cdi_adapter.podmanifest write` (drift → exit 1, override `CDI_ALLOW_DRIFT=1`). Runbook: `infra/runpod/README.md` (drift, acceptance, upgrading a pin); ARCHITECTURE §10.2 note; `.gitattributes` LF for the pin files. Found, not changed: App. B says MinIO RELEASE.2025-09-07 but the pod has built 2025-10-15 since E0-S16. | `test_bootstrap_pins_unit.py`: 35 passed. It includes bash sourcing `versions.env` the same way Python parses it, and the bootstrap's real `apt_pinned()` run against a fake `dpkg-query`. `test_fallback_unit.py` torch test now reads the pins from `versions.env`. `pip download --require-hashes` for manylinux x86_64 / cp312: 97 of 97 wheels hash-verified (321 MB). `lock_pod_deps.py --check`: lock reproduced. Two local `podmanifest write` runs gave "identical versions" and `compare` exit 0. CI gate GREEN (504 passed / 1 skipped). |
| 2026-09-22 | E1-S9 | **Blocked (needs the ingress VM + domain + pod).** Design chosen from the architecture (owner: "based on the architecture you can select it"): Caddy on our own India-region VM, reached over a reverse SSH tunnel the pod opens. The alternative, Cloudflare Tunnel, was rejected because it decrypts patient data at a third party (§12, E1-S10 residency). New: `infra/ingress/` (`Caddyfile`, `setup_ingress_vm.sh`, `cdi-ingress.env.example`, `versions.env` with Caddy 2.11.4 + sha512), `infra/runpod/start_tunnel.sh` (autossh, host key pinned, idempotent), `scripts/check_ingress.py` (UP / DEGRADED / BACKEND_DOWN / UNREACHABLE), `docs/ingress.md`. `start_all.sh` prints `COUNTER URL`. `autossh=1.4g-1` added to the E1-S4 apt pins. `.env.runpod` gets the ingress keys, empty = off. `test_ops_unit` env-key check allows `CDI_INGRESS_*`, which must be used by `start_tunnel.sh`. Docs: ARCHITECTURE §3.3/§3.5/§12, TECH_STACK, fallback-host, pod README; `.gitattributes` LF for `infra/ingress/*`. The UI is limited to clinic CIDRs until E11-S1. | `test_ingress_unit.py`: 33 passed. It runs `start_tunnel.sh` for real in bash with fake autossh/ssh-keygen (skip when unconfigured, refuse without a host key, pinned known_hosts, idempotent second run). It also calls the VM script's own `check_config` / `authorized_keys_line` (7 unsafe configs refused), runs the checker over real HTTP servers including the RunPod fake-200 page, and checks the real `/healthz` contract. Not run: Caddy itself (VM only; `caddy validate` runs in the setup script before any reload). CI gate GREEN (537 passed / 1 skipped). |
| 2026-09-22 | E1-S7 | **Blocked (a measured run needs real MinIO / the pod).** Locust harness (TECH_STACK names k6/Locust; Locust runs as `python -m locust`, which Smart App Control allows). New: `scripts/load_test.py` (defaults 30 patients × 5–10 docs, SLA 300 s, `--host` any URL, `--enforce-sla`); `scripts/loadtest/locustfile.py` (one user = one patient; upload → poll → FHIR; quits when all done; per-request timeouts); `synthetic.py` (deterministic scanned PNGs, 5 heuristic-classified types); `report.py` (exact p50/p95/p99, histogram, SLA verdict, samples); `docs/load-test.md`. Extra `load = ["locust>=2.40"]`, installed by CI and `make venv`; `make loadtest`; TECH_STACK row. Fixes found while testing: the discharge template now scores on the S2 heuristic (no VLM classify call); a job whose documents all failed is `documents_failed`, not ready; a `mismatch` fetches no bundle; tests never import locust in-process (gevent monkey-patches on import). | `test_loadtest_unit.py` 12 passed: real Locust runs against a job-API contract server (6-patient burst: states, failed docs, p50/p95/p99 within the timed plan, quits by itself; timeout; refused upload; `--enforce-sla` → 1). Local stack: real web app + PostgreSQL, 30 patients / 237 docs, uploads p50 0.14 s, all jobs terminal, all documents failed on MinIO :9000 (expected here). CI gate GREEN (549 passed / 1 skipped). |
| 2026-09-22 | E11-S1 | **Completed.** Local staff accounts + server-side sessions, fail-closed route policy (`webapp/auth.py`), migration 0005 (`web_user`, `web_session`), user CLI (`webapp/users.py`), sign-in page + Sign out, `docs/auth.md`. Settings `CDI_SESSION_IDLE_MINUTES` / `_MAX_HOURS` / `_COOKIE_SECURE`, `CDI_LOGIN_MAX_FAILURES` / `_LOCKOUT_MINUTES` (ARCHITECTURE §9, §12). E1-S9 / E1-S7 / start_all adjusted for sign-in. Dev env: mypy reinstalled as the same version 2.3.1 built from source (pure Python), because Smart App Control began blocking its compiled DLL. | Unit 608 passed / 2 skipped; integration 121 passed (MinIO-only skips); `test_auth_unit` 38 + `test_auth_integration` 28; live native web app, 15 curl checks; CI gate GREEN (lint, format, types, secrets, unit). |
| 2026-09-22 | E6-S5 (+ E6-S1, E6-S4 in `backlog 1.md`) | **E6-S5 Completed; E6-S1 and E6-S4 Blocked.** Pinned ABDM IG (`ndhm.in#6.5.0`; the old `nrces.fhir.r4.ndhm` is not a registry id, and the old code dropped the version), validator jar 6.10.4 + JRE pins, `infra/fhir/packages.lock` (10 packages, digests), `cdi_adapter.fhir.package_cache`, `scripts/fhir_ig.py` (seed / stage / promote / --dry-run), bootstrap step 5b, offline validator call (`-tx n/a -no-http-access` + closed-port proxy), 5 golden bundles through a pure `assemble_document_bundle`, error baseline ratchet (120 real IG errors), CI step, `docs/ig-upgrade.md`. Also fixed: `podmanifest.parse_versions` read `id#version` as `id` (bash keeps a mid-word `#`). | Real validator here: goldens offline from the lock-seeded cache give "no new errors"; `validate()` auto-switch gives `engine hapi`; seed from lock reproduces the registry set by digest; staging 6.5.0 PASSED + dry run identical; staging 6.0.0 FAILED + promote refused. Unit suite GREEN. |
| 2026-09-22 | E11-S4 | **Completed.** `src/cdi_adapter/deid/` (recognizers, ner, engine, document, redteam, CLI), `scripts/deid_redteam.py`, `docs/deid.md`; extra `deid`; settings `CDI_DEID_*` (ARCHITECTURE §9, §12; TECH_STACK). Pod lock regenerated with `deid` (121 packages, numpy 2.4.6) + the spaCy model by URL + sha256 (`lock_pod_deps.direct_wheels`, `podmanifest.parse_lock` reads `name @ url`); `versions.env` `SPACY_MODEL_*`. Dev env (Windows, Smart App Control): `regex 2025.10.22`, `spaCy 3.8.7`, CPU torch 2.8.0 in `.venv`; models in `.tools/`. | Red team 96 docs / 4 seeds, 0 leaks, all controls OK; `test_deid_unit` 24; `test_deid_models` 2 (real models); unit 632 passed / 4 skipped; integration 121 passed / 11 skipped (MinIO); CI gate GREEN. |
| 2026-09-22 | E2-S3 | **Blocked (the engine run needs the GPU pod).** `mlserve/grammar.py` (schema → exact XGrammar EBNF; `unsupported()` refuses features that cannot be locked exactly → HTTP 422), `VLLMBackend` passes the grammar, engine `backend="xgrammar"` with no fallback, setting `CDI_VLLM_GUIDED`. Found: XGrammar 0.1.25 does not enforce a `number` maximum (a 0–1 `confidence` accepted `1.8`), so that rule is hand-written. Also found in testing: sorting the schema for the cache key reordered properties (the grammar forced `confidence` first), so the order is kept. Dev: XGrammar 0.1.25 installed hash-checked into `.tools/xgrammar`; the tokenizer comes from the local Hugging Face cache. Docs: ARCHITECTURE §9 and §14 #3, TECH_STACK, pod README, `.env.*`. | `test_grammar_unit.py` 36 passed (real XGrammar + Qwen tokenizer; 105/105 random outputs valid); `test_vllm_unit.py` 16 passed (5 new); unit 691 passed / 4 skipped; CI gate GREEN. |
| 2026-09-22 | E2-S2 | **Blocked (the A/B needs vLLM on the GPU pod).** `extract_concurrency` setting + `extract_slots()` (hf 1; vllm = setting or `vllm_max_num_seqs`); `JobRunner` runs k jobs and k extractions at once over one shared semaphore, first document alone when the patient is unknown; `mpi.resolve_identity` under `pg_advisory_xact_lock` (race found: parallel jobs could mint a duplicate patient). Docs: ARCHITECTURE §3.3 note, §7.2, §9; `.env.example`, `.env.runpod`; pod README. | `test_jobs_unit.py` 14 passed (7 new, 5 repeat runs stable); new race test passes with the lock and fails without it; unit 650 passed / 4 skipped; integration 122 passed / 11 skipped (MinIO); CI gate GREEN. |
| 2026-09-22 | E2-S1 | **Blocked (real vLLM needs Linux + the GPU pod).** `VLLMBackend` (AsyncLLMEngine, background event loop, continuous batching, 7B→3B OOM fallback), `mlserve_backend` accepts `vllm`, settings `CDI_VLLM_*`. Version decision: vLLM 0.11.0 (last on torch 2.8, App. B) in the gateway's own venv (`.venv-vllm`, transformers 4.57.6) because no vLLM satisfies both App. B pins; `requirements-vllm.lock` (133 packages) via `lock_pod_deps.py --target vllm`; `versions.env` `VLLM_VERSION` / `VLLM_TRANSFORMERS_VERSION` / `TORCHAUDIO_VERSION`; bootstrap step 3b + a second drift check; `start_mlserve.sh` picks the venv; extra `vllm`. Docs: ARCHITECTURE App. B + §9, TECH_STACK, pod README (A/B steps). | `test_vllm_unit.py` 11 passed (fake engine); both locks reproduce (`--check`); unit 643 passed / 4 skipped; CI gate GREEN. |
| 2026-09-24 | E2-S6 | **Blocked (needs the E2-S5 checkpoint and the 32 GB target GPU).** `src/cdi_adapter/dslm/` has three modules: `corpus` builds the real S4 requests without a DB, synthetic/de-identified only; `quantize` runs AWQ `W4A16_ASYM` with decoder-anchored mappings and a `cdi_quantization.json` manifest; `bench` has `run` (production `VLLMBackend`, no 3B fallback, s/doc, docs/min, VRAM, KV tokens, slots) and `compare` (Sizing row, `MEASURED` only on the target GPU, archive). `VLLMBackend.generate_with_usage` + `kv_cache_tokens()`. Version decision: llm-compressor 0.7.1.4 in `.venv-quant` (writes compressed-tensors 0.11.0 = vLLM 0.11.0's; torch 2.8), `requirements-quant.lock` 56 packages via `lock_pod_deps.py --target quant`, `versions.env` `LLMCOMPRESSOR_VERSION` / `QUANT_TRANSFORMERS_VERSION`, extra `quant`. Found: llm-compressor has no Qwen2.5-VL AWQ mapping and its fallback also matches the vision MLP. `infra/runpod/dslm_quantize_bench.sh`, `docs/quantization.md`. The *Sizing & Capacity* workbook is not in the repo, so the row is copied by hand. | `test_dslm_unit.py` 24 passed (incl. real RapidOCR corpus build); `test_vllm_unit.py` + `test_grammar_unit.py` unchanged and passing; the 6-sample corpus builds (5 classified by the heuristic, 1 labelled); unit 716 passed / 4 skipped; CI gate GREEN. |
| 2026-09-24 | E2-S8 | **Blocked (the docs/minute A/B needs the GPU pod; the accuracy check needs the E2-S4 gold set).** Built, off by default. Design (from the architecture): several documents in one batched forward pass as **separate sequences**, not a merged prompt, so output tokens are produced in the same decode steps and evidence cannot cross documents (§0.5). Gateway `POST /vlm/generate_batch` (`hf`: one left-padded `generate`, split on OOM; `vllm`: items submitted together, each grammar-locked); `GenItem`; client `generate_batch` + `vlm_json_batch` (an invalid batched answer continues on the unchanged single-call retry path); `extract_documents` (single and batched share `_start` / `_request` / `_complete` / `_record_failure`; per-document isolation; failed batch → single calls); `JobRunner._groups` (same doc_type, one page, ≤ `CDI_EXTRACT_BATCH_MAX_BLOCKS` OCR blocks, ≤ `CDI_EXTRACT_BATCH_MAX` per group, after the patient is fixed; one slot per batch); `repo.document_shape`. Docs: ARCHITECTURE §7.2 note, §7.4 row 5, §9; `.env.example`, `.env.runpod`; pod README. | `test_batch_unit.py` 18 passed; `test_batch_integration.py` passed on real PostgreSQL (batched = single facts, values, evidence, runs); jobs / vLLM / extract suites unchanged and passing; unit 734 passed / 4 skipped; CI gate GREEN; integration 123 passed with the same 11 MinIO-only cases as before. |
| 2026-09-24 | E7-S1 | **Completed.** `webapp/auth.actor()`; `review.decide(..., reviewer)` with no client `reviewer` field; `/reviewer` claim / approve / reject / worklist and Generate EMR edits take the session user (the `?reviewer=` query and `reviewer` body fields are ignored; `job-editor` is gone). **Gap found:** approve/reject never checked who had claimed the item; `promote._assert_claimed` now requires the claimer (403), with an admin override recorded under the admin's name. Pages: no "Reviewer id" box; `/reviewer` shows the signed-in user and loads by itself. Docs: `docs/auth.md`, ARCHITECTURE auth row. | `test_review_integration.py` 12 and `test_reviewer_integration.py` 14 passed on real PostgreSQL through the signed-in app (7 new misuse tests: spoofed reviewer, other reviewer, admin override, no session, intake role, generate edits); unit 734 passed / 4 skipped; CI gate GREEN; integration 131 passed / 11 skipped (MinIO only); secret scan CLEAN. |
| 2026-09-24 | E10-S1 | **Blocked (DoD-DEPLOY: the deploy check needs the pod or a fallback host).** `src/cdi_adapter/metrics.py`: `cdi_stage_seconds` / `_failures_total` per stage, `cdi_extract_waiting` / `_in_flight`, `cdi_jobs`, `cdi_review_backlog`, `cdi_gate_decisions_total`, `cdi_review_decisions_total`, `cdi_review_items_total`, `cdi_vlm_generate_seconds` / `_requests_total`, `cdi_gpu_*` (from `nvidia-smi`). Web app `/metrics` on its own loopback port (`CDI_METRICS_PORT` 9464, `webapp.app.start_metrics`), gateway `/metrics` on its loopback port 8077; the staff app has no metrics route. Queue depth counted per job (found: with one slot, documents waited in the thread pool, invisible to a slot-only gauge). Lock: only `prometheus-client==0.26.0` added (a fresh resolve would have bumped 6 unrelated packages, left unchanged). Docs: `docs/metrics.md`, ARCHITECTURE observability row + §9, TECH_STACK, `.env.*`. | `test_metrics_unit.py` 15 and `test_metrics_integration.py` 2 passed; jobs / batch suites unchanged and passing; unit gate GREEN; integration 133 passed / 11 skipped (MinIO only). Colab deploy check not run: the updated source could not be moved to Colab from here (see chat, 2026-09-24). |
| 2026-09-24 | E10-S1 | **Completed: deploy check on the Colab L4 fallback host** (owner: "use the colab"). Updated source unpacked over the running copy, `prometheus-client==0.26.0` installed hash-checked, gateway and web app restarted. | Bound to 127.0.0.1 only (`ss`: 9464, 8077; 8888 is the public app); `:8888/metrics` 404; `/healthz` OK. Live job (prescription page): `cdi_stage_seconds_count` +1 for each of the 6 stages (extract 57.8 s, ingest 4.5 s, ocr 4.4 s); `cdi_gpu_utilization_percent` peak 100, GPU memory 18.4 GB; `cdi_extract_in_flight` peak 1; `cdi_jobs{review}` +1; `cdi_gate_decisions_total{in_review}` +3; `cdi_review_backlog` held +1, workbasket +1; `cdi_vlm_requests_total{single,ok}` +2 (first answer failed the schema, one retry). |
| 2026-09-24 | E10-S2 | **Completed; deploy-checked on the Colab L4 fallback host** (owner: "use the colab"; the VM was wiped mid-session and rebuilt from a full source package). `infra/monitoring/`: `prometheus.yml`, `alertmanager.yml`, `rules/cdi-alerts.yml` (4 alerts) + `rules/cdi-alerts.test.yml` (synthetic triggers), Grafana provisioning + dashboard "CDI-Adapter overview" (13 panels), `start_monitoring.sh`; `versions.env` pins Prometheus 3.14.0 / Alertmanager 0.34.1 / Grafana 13.2.2 with sha256; new metrics `cdi_job_seconds`, `cdi_job_oldest_running_seconds`, `cdi_vlm_oom_total{where}`, `cdi_extractions_total{outcome}`; secret `cdi_grafana_admin_password` (E1-S5 store); `docs/alerts.md` (on-call). **Security finding fixed:** Grafana 13.2 ignored the admin-password `__FILE` override and started with admin/admin; the password is now set from the store after start (`reset-admin-password --password-from-stdin`), credentials go to curl on stdin only, and the start fails if admin/admin still works. Alert delivery channel: owner's choice, not configured. | On Colab: every download sha256-verified; `promtool check rules` 4 rules; **`promtool test rules` SUCCESS** (each alert fires on its trigger and stays quiet on its normal case); `test_monitoring_unit.py` **15 passed on Linux** (promtool tests included); 9090/9093/3000 bound to 127.0.0.1 only; Prometheus targets cdi-webapp, cdi-mlserve, prometheus all up; 4 rules loaded `ok`; a synthetic alert through Alertmanager showed `active`; Grafana 13.2.2 healthy, datasource "Successfully queried the Prometheus API", dashboard provisioned in folder "CDI-Adapter", anonymous 401; after the fix admin/admin 401 and the store password 200 (the new curl-on-stdin check: CHECK-PASSES). Locally: `test_monitoring_unit.py` 13 passed / 2 skipped (promtool is Linux-only); metrics integration +1. |
| 2026-09-24 | E10-S3 | **Completed; deploy-checked on the Colab L4 fallback host.** `cdi_adapter.tracing`; job / `stage.*` / `vlm.generate` / `fhir.project` spans; `trace_id` in the job API and on every log line; `traceparent` web app -> gateway; job context handed to pool threads; `Job.finished` (fixes a `wait()` race after a mismatch). Tempo 3.0.3 + Loki 3.7.8 in `infra/monitoring/` (pinned + sha256, loopback, OTLP), Grafana data sources linked both ways; `CDI_OTEL_*` settings; 7 OTel packages in both locks (nothing else changed). Found on Colab and fixed: Tempo 3.0 moved retention to `backend_worker`; a running Grafana did not know new data sources (provisioning now reloaded on every start). Docs: `docs/tracing.md`, ARCHITECTURE observability row + §9, TECH_STACK, `docs/metrics.md`. | Local: `test_tracing_unit.py` 5 passed; `test_monitoring_unit.py` 19 passed / 2 skipped (promtool is Linux-only); unit gate GREEN; integration 134 passed / 11 skipped (MinIO only). **Colab (live job `99610dfd…`, trace `18b85b96e5c7c8c5d0693e2298d97b19`):** the API returned the trace id; Tempo holds **10 spans in that one trace across both services**: `job` -> `stage.ingest`, `stage.ocr`, `stage.classify`, `stage.extract` (-> 2 × `vlm.generate` in cdi-mlserve), `stage.terminology`, `stage.validate`, and `fhir.project` after the bundle request. Loki holds **12 log lines with that trace id** (web app 11, gateway 1: ingested ... projected, job_finished). Grafana: Prometheus, Tempo and Loki data sources all OK; ports 3100 / 3200 / 4318 / 9095 / 9096 bound to 127.0.0.1. |
| 2026-09-24 | E10-S4 | **Completed; deploy-checked on the Colab L4 fallback host.** Metrics `cdi_fact_confidence{fact_type}` (S6 gate) and `cdi_terminology_bindings_total{outcome}` (S5); alert `CdiConfidenceDrift` (KS distance, last 6 h vs the 7 days before, > 0.25, >= 50 facts, 30 min, ticket) with 3 promtool cases; dashboard 17 panels (KS distance, confidence quantiles, human-override rate, unmapped-code rate); `scripts/drift_staging_check.py` (injected shift in a staging Prometheus); `start_monitoring.sh` reloads a running Prometheus / Alertmanager (SIGHUP) so new rules load. Docs: `docs/alerts.md` (on-call section), `docs/metrics.md`. | Local: unit gate 913 passed / 17 skipped; integration 136 passed / 11 skipped (MinIO only). **Colab:** `start_monitoring.sh` "alertmanager / prometheus reloaded its configuration"; `promtool check rules` 5 rules; **`promtool test rules` SUCCESS**; `test_monitoring_unit.py` + `test_metrics_unit.py` **40 passed on Linux** (promtool included); live Prometheus: all 5 rules `ok`; **staging: injected shift -> alert `pending`, KS 0.497; control -> `inactive`, KS 0.0: DRIFT STAGING CHECK PASS**; Grafana dashboard 17 panels (version 2) with the 4 drift panels; web app `/metrics` exposes `cdi_fact_confidence` and `cdi_terminology_bindings_total`. |
| 2026-09-24 | E10-S5 | **Blocked, on E2-S7 only (the gold-set metrics). Everything else is built and drill-checked on the Colab L4.** Migration 0006 (`model_version`, append-only `model_rollout` + audit); `cdi_adapter.modelops` (registry, per-document router with canary fallback, live `compare` + promotion gate, one-command `rollback`, `serve`); the gateway reports `version` on `/healthz` and loads the pinned revision; `extraction.model_version` is filled; 3 metrics + 2 dashboard panels; `.env` `CDI_MODEL_VERSION` / `CDI_VLM_MODEL_REVISION` / `CDI_MODEL_ROUTE_TTL_S`; `docs/model-rollout.md`, ARCHITECTURE, `docs/metrics.md`. | Local: unit 944 passed / 17 skipped; integration 143 passed / 11 skipped (MinIO only), including the 7-test drill. **Colab** (the VM was wiped, so it was rebuilt from the full source): alembic head 0006. **`model_rollout_drill.py`: MODEL ROLLOUT DRILL PASS.** Two gateways ran the Qwen2.5-VL-3B weights at pinned revision cc59489... as `drill-3b-a` / `drill-3b-b` (two 7B do not fit one L4), 16.3 GB on the GPU. Results: canary at 50 % split 8 documents 3 / 5, each answered by the version it was routed to. The canary rollback took 0.00 s, and every later document went to A. After the promotion, every document went to B. The promotion rollback took 0.04 s, back to A. With A's gateway stopped, the rollback started it again and switched in 1.1 s; the next documents went to A. `compare` blocked promotion (5 canary documents < 20). Then the site's 7B was registered as `qwen2.5-vl-7b-cc59489`, restarted with that version, and deployed as primary. The log refuses UPDATE (trigger); 16 audit rows. Found: most drill answers from the 3B were `_partial` (13 of 16 for A), a 3B quality point, not a rollout fault. My own mistake during the check: running `rollback` with no canary correctly returned the site to the previous primary (the drill 3B); it was put back at once (`deploy --force`, log #12-#13) and the drill gateway stopped. `test_gateway_healthz` assumed a stub `.env`; it now reads the host's settings. |
| 2026-09-24 | E10-S6 | **Blocked on a second host, for losing a whole MinIO node (and a standby off the pod). The single-host parts are built and drill-checked on the Colab L4.** Built: `cdi_adapter.ha` (`pg`: standby, status, fenced failover, rejoin, drill; `minio`: erasure set on one host or one process per host, start and stop, migrate, drill); `db.engine_url` + `CDI_DATABASE_HOSTS` (the app connects to the writable server); multi-host `ObjectStore` failover; `bootstrap_pod.sh` fence guard plus opt-in standby and MinIO erasure or cluster; secret `cdi_replication_password`; 5 gauges; alerts `CdiPgReplicationLag`, `CdiPgStandbyDisconnected`, `CdiMinioDegraded`; dashboard 21 panels; `docs/ha.md`, `docs/alerts.md`, ARCHITECTURE §3.3 + §13 HA row, `.env.*`. Found on Colab and changed: (1) MinIO refuses loopback multi-node inside a container (`cmd/endpoint.go`, `/.dockerenv`), and §12 keeps services on loopback (owner: "select based on the architecture"), so on one host MinIO is one process over 4 drives, and node loss needs real hosts; (2) MinIO refuses erasure drives on the root disk: the drill on Colab uses MinIO's `MINIO_ROOTDISK_THRESHOLD_SIZE` (the pod's /workspace is not the root disk); (3) the standby's password file moved out of the root-only secrets folder (the postgres server reads it). | Local: unit 965 passed / 17 skipped (after the MinIO rework); integration 144 passed / 11 skipped (MinIO only), including the real Postgres failover drill (0 of 134 acknowledged commits lost, RTO 2.7 s). **Colab:** HA + ops unit tests 33 passed on Linux; **`pg drill`: PG FAILOVER DRILL PASS**: primary crashed with `stop -m immediate`, 141 acknowledged commits, **0 lost (RPO 0 s)**, **RTO 0.14 s**, lag before the crash 0.0015 s, old primary fenced then rejoined as a standby and received new rows. **MinIO drive-loss drill: PASS** (same steps as `minio_drill.py`, run in a notebook cell because the updated package was not uploaded yet): 4-drive erasure set on 127.0.0.1, 30 objects of 1 KB to 1.5 MB through the app's `ObjectStore`; drive 1 deleted while running, **30/30 read back identical**, still writable, 10 more written; the replacement drive **healed 40/40 in 16 s**; 40/40 intact. Observed: MinIO recreates and heals a deleted drive folder by itself (log: "Healing of drive ... is finished"), so its drive-offline gauge stayed 0. That gauge (and so `CdiMinioDegraded`'s drive trigger) moves on a real disk failure, which one host cannot simulate: it is covered by promtool cases only. |
| 2026-09-24 | E10-S11 | **Built and drill-checked on the Colab L4; Blocked only on E1-S6 (golden-path `full` mode), E14-S5 and an off-pod backup location (the second host).** `cdi_adapter.dr`: the object backup (incremental, sha256-checked, safe paths, manifest), the backup status, the restore drill and the CLI (`backup | drill | status`). `snapshot.sh` now backs up objects too and records failures through `trap`; the weekly drill runs from cron; settings `CDI_BACKUP_DIR` and `CDI_DR_REPORT_DIR`; `DrCollector` (6 gauges); alerts `CdiBackupStale`, `CdiRestoreDrillFailed`, `CdiRestoreDrillOverdue`; dashboard 22 panels; `docs/dr.md` (targets, procedure, what is and is not reproducible); ARCHITECTURE §10.3; E14-S5's failure runs now include data-tier loss. `ha.pg.init_cluster` is shared with the E10-S6 failover drill, which passed again after the refactor. | Local: unit 982 passed / 17 skipped, integration 147 passed / 11 skipped (MinIO only); `test_dr_unit.py` 14 passed; `test_dr_integration.py` 3 passed with real PostgreSQL 16 (restored from `pg_dump` plus objects, 4/4 scans, golden path; a corrupted object and a truncated dump each fail the drill). **Colab, the live system:** `snapshot.sh` dumped 160 KB and backed up **72 objects** (3 MB, sha256-checked), status ok. **`python -m cdi_adapter.dr drill --minio-bin … --live`: RESTORE DRILL PASS**, with a new Postgres cluster and a new MinIO from the backup folder alone: `pg_restore` ok, schema at head, **72/72 objects restored (0 corrupt)**, **24/24 original scans byte-identical**, 24 documents, golden path ok; **RPO 38.6 s** (target 20 min), **RTO 8.5 s** (restore 6.2 s; target 30 min), 0 documents lost since the backup; `/metrics` gauges from the real status files: last backup ok, last drill ok, RPO 38.6, RTO 8.5. Monitoring stack installed on the new VM (pinned and sha256-checked): **`promtool check rules` 11 rules; `promtool test rules` SUCCESS** for all 11 alerts, including the 6 of E10-S6/S11 (their first promtool run); `test_monitoring_unit.py` 25 passed on Linux. Found by promtool and fixed: the `CdiRestoreDrillOverdue` test fed hourly samples, which go stale after 5 min between the 1-min evaluations, so `for: 1h` never held. The test now uses 5-min samples; the rule was right. |
| 2026-09-25 | E10-S9 (+ E9-S1 core) | **Completed; E9-S1 In Progress** (its first two items were built here, because E10-S9 needs them). Migration 0007 `web_job`; `webapp/jobstore.py` (accept durably, then dispatch; read from the database; `PgJobStore` with lease, heartbeat and sweep); `JobRunner` stored jobs (claim, save each step, resume, `LeaseLost`, give-up); Celery task `cdi.web_job` + the worker start-up hook; `/readyz`; ingest advisory lock + re-render; gateway replica failover in `HttpMLClient` and replica lists in `modelops`; `infra/runpod/start_apptier.sh` + `Caddyfile.lb` (Caddy pinned in `versions.env`); `start_all.sh`, `.env.runpod`, Colab notebook Step 5; Prometheus target files, 2 alerts, 3 metrics, panel 23; `scripts/app_ha_drill.py`; `docs/ha.md`, `alerts.md`, `metrics.md`, TECH_STACK, BACKLOG_AS_BUILT. | Local: CI gate GREEN (lint, format, types, secrets; unit 863 passed / 6 skipped); integration 151 passed / 13 skipped (MinIO only) on PostgreSQL 16, then the modelops launcher test updated for replica lists (7 passed). **Colab (VM rebuilt from the full source):** integration on real MinIO 155 passed, 6 failed. 2 were my tests (a leftover orphan from another test; the pod `.env` is in queue mode with no worker during tests), both fixed; the affected files then passed 51/51. The other 4 are the known Colab failures from before this block (HAPI validator, extract counts). Unit tests on Linux: 79 passed. **`app_ha_drill.py --kill-gateway`: DRILL PASSED.** 12/12 uploads acknowledged, 0 lost, 28/28 documents done; `web1` out of rotation 1.9 s after SIGKILL (window 4 s); 6,143 probes through the balancer, 0 failed; 1 job taken over from the killed worker; no document doubled; `web1` back in rotation in 5.4 s. Prometheus found both instances (9464, 9465) and both workers (9470, 9471) up; `cdi_jobs` read from `web_job`; `cdi_job_resumes_total` 2. **promtool: 13 rules; `test rules` SUCCESS** (including the 2 new alerts). `test_monitoring_unit.py`'s pinned rule count updated (11 to 13). Found: files patched by a Python script on Windows were written CRLF and broke bash on Colab (`versions.env`); all 56 affected files converted to LF (no content change). |
| 2026-09-25 | Verification host: sync + test URL (owner direction; E1-S3 fallback-host area) | **Owner:** stop hand-patching Colab per test (keep it synchronized with the verified local code, use it for live/integration tests) and give the website a standalone URL that does not depend on the Colab window and can be handed to other testers. **Added:** `scripts/dev/colab_bundle.py` (the working tree + `MANIFEST.sha256` -> `Desktop/cdi-src.tar.gz`; CRLF text refused); `infra/colab/sync.sh` (checks every file against the manifest, removes files dropped since the previous bundle, applies, re-checks, reinstalls on a `requirements.lock` change, `alembic upgrade head`, restarts web + workers and the gateway if its code changed, records `.bundle_id`; `.env`/`.venv`/data never touched); `infra/colab/start_test_tunnel.sh` (Cloudflare quick tunnel `*.trycloudflare.com` to the balancer, `cloudflared` 2026.9.3 pinned + sha256 in `versions.env`, `--no-autoupdate`; adds its host to `CDI_PUBLIC_ORIGINS`, restarts the web instances; `status`/`stop`). Notebook Steps 9 (test URL) and 10 (sync). `docs/fallback-host.md` section. **Test hosts only:** the quick tunnel decrypts at Cloudflare, so synthetic/de-identified data only; production stays on the E1-S9 ingress (Cloudflare Tunnel was rejected there for residency). | Local: lint/format/types/secrets clean; unit 890 passed (then the new test file re-run after a format fix, 8 passed). `tests/test_colab_sync_unit.py` (8): bash syntax, pin + checksum + test-only guard, bundle manifest round trip, CRLF refusal, **`sync.sh` run for real** (apply, remove dropped file, keep `.env`/data/venv, damaged bundle refused with nothing changed, host without setup refused). |
