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
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: — · Arch ◐ §3.3, §10.1/§10.2 · DoD-DEPLOY`
Harden `start_all.sh` / `bootstrap_pod.sh` with pinned package versions and drift detection.
- [ ] Pin package versions in the bootstrap
- [ ] Add drift detection
- [ ] A new pod reaches "LIVE URL printed" unattended from a clean checkout, **twice in a row, with identical versions**
- **Tests:** two consecutive fresh-pod boots; version manifests compared

#### Block E1-S9 — Stable ingress / DNS for the counter workflow ➕ ⬜
**Status:** Pending
`Must · BLOCKER · 1 d · 2 pts · Deps: E1-S3 · Arch ✔ §3.3 ("RunPod reassigns IP/port on every restart"); Risk R7 · DoD-DEPLOY`
R7 names stable ingress as a mitigation, but no block delivers it. A billing counter cannot have
its URL change every time the pod restarts.
- [ ] A stable hostname fronts the web app; replacing the pod/host does not change the URL staff use
- [ ] Health-checked ingress, so a dead backend returns a real error instead of RunPod's HTTP-200 fake-502 page (§10.4)
- [ ] TLS terminated at the stable hostname; the runbook (§10.2) no longer treats the per-pod LIVE URL as the address of record
- **Tests:** restart/replace the backend — the same URL keeps serving; a stopped backend returns a distinguishable health failure

#### Block E1-S7 — Load-test harness (30 × 5–10 concurrent documents)
**Status:** Pending
`Must · BLOCKER · 2.5 d · 5 pts · Deps: E1-S6 · Arch ✔ [ADR-0002](adr/0002-load-and-sla-requirement.md) §15.1 (E1-S8 re-score; was ✖ (cites "Brief's own load figure"; ARCHITECTURE.md has no 30-patient load figure; §7.3 is single-job timing)) · DoD-CODE`
> **E1-S8 re-score (2026-09-21):** ⛔ HOLD lifted → Arch ✔. External 🔒 dependencies, if any, are unchanged.
- [ ] k6/Locust script uploads N synthetic patients × 5–10 docs concurrently (N configurable, default 30)
- [ ] Reports a p50/p95/p99 upload→bundle-ready latency histogram against the Sizing SLA
- [ ] Runnable against any environment URL
- **Tests:** a harness dry run against a local stack

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
**Status:** Pending — reset from ✅: code at `fdd4aeb` not provided; not verifiable
`Must · BLOCKER · 4 d · 8 pts · Deps: E1-S3 · Arch ✔ §3.4, §14 #1a · DoD-CODE`
- [x] `CDI_MLSERVE_BACKEND=vllm` serves the same `/vlm/generate` contract; `hf` selectable with one env flip
- **Evidence (Excel):** built and A/B tested on-pod; **no throughput gain** (72–74 s, unchanged). Reverted to `hf`; the code is kept, dormant. Confirmed in code: `VLLMBackend` in `mlserve/backends.py`.

#### Block E2-S2 — Remove the Phase-2 serialisation lock in `webapp/jobs.py` ✅
**Status:** Pending — reset from ✅: code at `fdd4aeb` not provided; not verifiable
`Must · BLOCKER · 1 d · 2 pts · Deps: E2-S1 · Arch ✔ §7.2, §14 #1b · DoD-CODE`
- [x] With vLLM, documents in a job extract in parallel; with `hf`, the lock stays active
- **Evidence (Excel):** same A/B as E2-S1. Confirmed in code: `extract_concurrency` in `config.py`.

#### Block E2-S3 — Grammar-locked decoding (guided_json / XGrammar) ✅
**Status:** Pending — reset from ✅: code at `fdd4aeb` not provided; not verifiable
`Should · HARDEN · 3 d · 5 pts · Deps: E2-S1 · Arch ✔ §3.4, §14 #1c · DoD-CODE`
- [x] Schema-invalid JSON impossible by construction (grammar rejects the token)
- **Evidence (Excel):** same A/B as E2-S1. Confirmed in code: `vllm_guided` / XGrammar.
- **Note:** the acceptance criterion "`_partial` rate on the E2-S4 gold set drops to 0" can't be measured until E2-S4 exists.

#### Block E2-S6 — Quantize + benchmark the fine-tuned DSLM ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E2-S5 · Arch ✔ §14 #1d (AWQ/INT4) · DoD-EVAL`
- [ ] AWQ/INT4 the E2-S5 checkpoint
- [ ] Measure latency/VRAM vs the bf16 7B baseline: extract-seconds/doc and concurrent slots at 32 GB
- [ ] Write the results into *Sizing & Capacity* (DSLM row, currently row 32), tagged MEASURED
- **Tests:** benchmark script output archived

#### Block E2-S8 — Batch small documents/pages into fewer VLM calls ⬜
**Status:** Pending
`Could · OPTIMIZE · 2 d · 3 pts · Deps: E2-S1, E1-S7 · Arch ◐ §7.4 Performance plan · DoD-CODE`
- [ ] Group short documents (e.g. single-page vitals sheets) into one VLM call where the schema allows
- [ ] Measurably higher docs/minute/GPU on the E1-S7 harness vs one call per document
- [ ] No drop in per-field accuracy on E2-S4
- **Tests:** E1-S7 A/B + E2-S7 accuracy comparison

---

# EPIC E6 — FHIR/ABDM IG Validation & Correctness

### Phase 1

#### Block E6-S5 — IG package version-pin + upgrade process ⬜
**Status:** Pending
`Should · HARDEN · 1 d · 2 pts · Deps: E6-S4 · Arch ◐ §9 Configuration · DoD-DEPLOY`
- [ ] Runbook: upgrade the IG package in staging → re-run E6-S4 → only then flip production `CDI_IG_PACKAGE` (currently `nrces.fhir.r4.ndhm#6.5.0`)
- **Tests:** a staging dry run of the procedure

---

# EPIC E7 — Human-in-the-Loop / Counter-Side Review Hardening

### Phase 3

#### Block E7-S1 — AuthN for the /review console ⬜ 🔒
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E11-S1 (resolved by resequencing, §3.1) · Arch ✔ §12, §4.3, §14 #6 · DoD-SEC`
- [ ] `/review`, `/api/review/*` and `/api/facts/*/review` require an authenticated staff session
- [ ] The reviewer comes from the session and can no longer be typed by the client (`fact_provenance.agent`)
- **Tests:** unauthenticated → rejected; a spoofed reviewer parameter is ignored
- 🔒 Identity provider / user directory

---

# EPIC E9 — Scale, Queueing & Autoscaling for 30-Concurrent Load

### Phase 4

#### Block E9-S1 — Move web-app ingestion onto the durable queue ⬜
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E1-S7 · Arch ✔ §13, §14 #8 · DoD-CODE`
- [ ] Route `/api/jobs` through the existing Celery/Redis chain instead of the in-process pool (`_jobs` dict in `webapp/jobs.py`)
- [ ] A webapp restart mid-job loses no in-flight uploads; queued jobs resume
- [ ] ➕ A forward-incompatible Alembic migration can be applied with workers in flight, and rolled back, without corrupting or double-processing queued jobs
- **Tests:** kill the webapp during the E1-S7 load test → zero lost documents; a migrate-under-load test

#### Block E9-S2 — Separate the CPU pool (ingest/OCR/classify) from the GPU pool (extract) ⬜
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: E9-S1 · Arch ✔ §7.3, §7.4, §14 #8 · DoD-CODE`
- [ ] CPU-stage and GPU-stage worker counts independently configurable and scalable
- [ ] Scaling only the CPU pool measurably raises Phase-1 throughput without touching GPU spend
- **Tests:** load-test comparison

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
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §13, §14 #9 · DoD-DEPLOY`
- [ ] `/metrics` exposes per-stage latency histograms, queue depth, GPU utilization %, and S6 gate `auto_accepted` vs `in_review` counts (plus reviewer throughput)
- **Tests:** scrape test for each metric

#### Block E10-S2 — Grafana dashboards + alerting ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E10-S1 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [ ] 4 alert rules: SLA-window breach, GPU OOM, queue backlog above threshold, `_partial` rate above rolling baseline
- [ ] A documented on-call response for each
- **Tests:** a synthetic trigger for each alert

#### Block E10-S3 — Centralized logs + OTel tracing ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E10-S1 · Arch ✔ §10.4, §14 #9 · DoD-DEPLOY`
- [ ] One trace id follows a document from `POST /api/jobs` through every stage to the FHIR bundle
- [ ] Viewable in one place (Loki/Tempo or equivalent)
- **Tests:** end-to-end trace-continuity test

#### Block E10-S4 — Drift monitors ⬜
**Status:** Pending
`Should · HARDEN · 3 d · 5 pts · Deps: E10-S1, E3-S3 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [ ] Confidence-distribution shift alert (same day)
- [ ] Human-override rate and unmapped-code rate as trend lines on the E10-S2 dashboard
- **Tests:** an injected distribution shift in staging triggers the alert

#### Block E10-S5 — Model registry + canary/rollback ⬜
**Status:** Pending
`Should · HARDEN · 2 d · 3 pts · Deps: E2-S7 · Arch ✔ §14 #9 · DoD-DEPLOY`
- [ ] Deploy a new model version to a small traffic percentage first; compare E2-S7 metrics live against the incumbent
- [ ] One command rolls back to the previous registered version
- **Tests:** canary + rollback drill

#### Block E10-S6 — Postgres primary + standby, MinIO multi-node ⬜
**Status:** Pending
`Should · HARDEN · 4 d · 8 pts · Deps: E1-S3 · Arch ✔ §3.3, §10.3, §13, §14 #9 · DoD-DEPLOY`
- [ ] Postgres failover to a standby with RPO < 1 minute (vs today's 15-minute dump)
- [ ] MinIO survives one node loss without data loss
- **Tests:** failover drill; node-loss drill

#### Block E10-S11 — Restore / DR drill with a measured RPO and RTO ➕ ⬜
**Status:** Pending
`Must · BLOCKER · 2 d · 3 pts · Deps: E10-S6 · Arch ✔ §3.3 / §10.3 (the pg_dump + auto-restore persistence model), §13 "HA/DR: single pod" · DoD-DEPLOY`
E10-S6 *promises* RPO < 1 min. Nothing in this backlog ever restores anything, and the whole
documented persistence model is a 15-minute dump replayed at boot.
- [ ] Rehearsed restore of Postgres **and** the MinIO scan bytes into a clean environment, from backups alone
- [ ] Measured RPO and RTO published against the documented targets; the golden-path suite (E1-S6) passes against the restored environment
- [ ] Runbook entry for data-tier loss, stating which derived rows are reproducible by re-running the pipeline and which are not
- [ ] Add data-tier loss to E14-S5's failure-injection set (it currently covers web restart, worker restart, GPU loss, terminology outage and a poison document — not this)
- **Tests:** the restore drill itself, re-run on a schedule; a backup that fails to restore raises an alert

#### Block E10-S9 — Application-tier HA: multi-instance webapp/mlserve ⬜
**Status:** Pending
`Should · HARDEN · 4 d · 3 pts · Deps: E9-S1 · Arch ◐ §12/§13 "HA/DR: single pod" · DoD-CODE`
- [ ] webapp runs as 2+ stateless instances behind health-aware traffic distribution
- [ ] A killed instance leaves rotation within the health-check window with zero dropped acknowledged uploads
- **Tests:** kill-instance drill under load

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
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §12 (top row), §13, §14 #9 · DoD-SEC`
- [ ] Roles: intake staff / clinician reviewer / admin
- [ ] Every route in the §7.1 endpoint table requires an authenticated session with a role check
- **Tests:** an unauthenticated request to any `/api/*` route is rejected (negative test); role-check tests

#### Block E11-S4 — De-identification pipeline for training/eval data ⬜
**Status:** Pending
`Must · BLOCKER · 3 d · 5 pts · Deps: — · Arch ✔ §12 De-identification · DoD-SEC`
- [ ] Presidio + clinical NER strips direct identifiers before any document is used in E2-S4/E2-S5
- **Tests:** a red-team re-identification attempt on a sample fails

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
- [ ] Failure runs: web restart, worker restart, one GPU loss, terminology outage, one poison document
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
