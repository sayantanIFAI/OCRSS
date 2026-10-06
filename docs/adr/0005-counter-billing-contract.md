# ADR-0005 — Accept the counter status board and the billing handoff contract

- **Status:** Accepted (2026-09-21) · decided under E1-S8 by the engineering owner (delegated) ·
  **owner sign-off pending**
- **Backlog:** E1-S8 · lifts E7-S6, E7-S7

## Context

ARCHITECTURE.md has no billing contract and no counter-side board, so E7-S6 and E7-S7 were on
⛔ HOLD. The brief requires "human in the loop … at the counter" and "JSON before billing". §4.5
already carries the `ops_*` / `fin_*` "counter-side and billing-adjacent state" groups.

## Decision

1. **Counter status board** (E7-S6): in-flight patients with per-document status (governed / held /
   count) and an ETA-to-ready, updated live from S8 decisions (reusing the live re-projection).
2. **Billing handoff contract** (E7-S7): a status API exposing `uploaded_docs`, `processed_docs`,
   `governed_fact_count`, `held_fact_count`, `identity_status`, `FHIR_validation_status`,
   `estimated_ready_at`.
3. `billing_ready = true` is **impossible** while identity fields, the configured billing-critical
   facts or FHIR validation are unresolved. Unresolved non-critical facts are serialised
   separately and are never labelled confirmed.

## Constraints

- The UI and the API use the same state machine and audit trail (ADR-0003 §1).
- The billing-critical field set is approved by the billing and clinical owners (🔒, unchanged).

## Consequences

- E7-S6 and E7-S7 are architecture-backed. E6-S8's InvoiceRecord half is now backed by this ADR;
  its v7 vital-sign-profile and ImmunizationRecord half is not (see the E1-S8 re-score).
