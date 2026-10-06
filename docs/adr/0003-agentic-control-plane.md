# ADR-0003 — Accept a bounded agentic control plane under a deterministic workflow

- **Status:** Accepted (2026-09-21) · decided under E1-S8 by the engineering owner (delegated) ·
  **owner sign-off pending**
- **Backlog:** E1-S8 · lifts E12-S1…S6, E4-S4, E7-S4

## Context

ARCHITECTURE.md describes a deterministic 9-stage pipeline (§3.2) and names no agent layer, critic,
copilot or `agent_trace` table, so epic E12 and the blocks built on it were on ⛔ HOLD. The brief
asks for "agentic + no-guess" processing, and the web app's own front page is titled "Intelligent
OCR driven Agentic EMR generation" (§7.2).

## Decision

Agents are accepted **only** inside these bounds (backlog §0.5 "Agents" rule, now architecture):

1. A **persisted, deterministic workflow / state machine owns every document transition**:
   `UPLOADED → DURABLY_STORED → TRIAGED → {CPU_PATH | GPU_ESCALATED} → CANDIDATES →
   EVIDENCE_BOUND → VALIDATED → {GOVERNED | REVIEW_REQUIRED} → FHIR_VALIDATED → BILLING_READY`.
   Transitions are service methods / DB transactions with allow-listed previous states and audit.
2. Agents may choose tools, re-check evidence, query terminology, recommend corrections or request
   escalation. Every tool call is allow-listed, schema-validated and time-bounded.
3. Agents **can never set** `auto_accepted`, `clinician_confirmed`, `billing_ready` or
   `ready_to_share`. The S6 gate stays the only machine authority for fact acceptance; humans
   decide the rest.
4. A grounding / critic step (E4-S4 / E12-S3) may only **veto or escalate**, and runs on CPU by
   default (RapidOCR on the cropped evidence box).
5. Every agent decision is traceable (`agent_trace`, E12-S5) and joined to `fact_provenance`.

## Consequences

- The deterministic S6 gate (§5 S6) and the "FHIR asserts only governed facts" rule (§6) are
  unchanged and remain authoritative.
- The reviewer copilot (E7-S4) is advisory only: the human still clicks accept / edit / reject.
