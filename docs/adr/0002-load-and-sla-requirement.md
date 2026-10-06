# ADR-0002 — Accept the 30 × 10 load requirement and the 5-minute SLA

- **Status:** Accepted (2026-09-21) · decided under E1-S8 by the engineering owner, to whom the
  product owner delegated the E1-S8 mandate decisions · **owner sign-off pending**
- **Backlog:** E1-S8 · lifts E1-S7, E9-S6, E9-S7, E14-S5 (and, with ADR-0004, E10-S8)

## Context

ARCHITECTURE.md §7.3 records single-job timing only (a 3-document job in 74 s). It has no
concurrent-load figure, so every block built on the brief's load requirement sat on ⛔ HOLD —
including E14-S5, the final go-live gate. The planning backlogs (§2.1 of the infra file) state the
requirement: 30 concurrent patients × 5–10 documents (design burst 10 → 300 documents), and an SLA
of 5 minutes from the last upload to governed JSON/FHIR **or an explicit hold with an ETA**.

## Decision

The architecture carries a **non-functional requirement**:

| Requirement | Value | Status of the number |
|---|---|---|
| Concurrent patients | 30 | ASSUMPTION — measured by E1-S7 / E14-S5 |
| Documents per patient | 5–10 (design burst 10 → 300 documents) | REQUIREMENT |
| SLA | last upload → governed JSON/FHIR, **or an explicit hold with an ETA**, within 5 minutes | ASSUMPTION — measured by E10-S8 / E14-S5 |
| Max planned utilisation | 0.7 | ASSUMPTION |

## Constraints (non-negotiable)

- The SLA is met by **machine-ready results plus explicit holds** — never by auto-accepting to save
  time. The no-guess rule (§0 governing principle) outranks the SLA.
- Planning estimates do not prove readiness: only the E14-S5 soak does.

## Consequences

- The load-test harness (E1-S7), the autoscaled load test (E9-S6), deadline queueing (E9-S7) and the
  production-readiness soak (E14-S5) are architecture-backed.
- The honest limit recorded in the infra backlog §2.1 stands: one CDI GPU at ~21 s/doc serial cannot
  clear 300 documents in 5 minutes; it takes CPU-first routing (E2-S9), batching, and/or the
  "hold with ETA" reading of the SLA.
