# ADR-0004 — Accept the GPU-class and cost policy

- **Status:** Accepted (2026-09-21) · decided under E1-S8 by the engineering owner (delegated);
  the GPU and budget numbers were already decided by the product owner on 2026-09-20 ·
  **owner sign-off pending**
- **Backlog:** E1-S8 · lifts E13-S1…S6, E1-S10, and (with ADR-0002) E10-S8

## Context

ARCHITECTURE.md has no cost, GPU-class, OPD-hours or interruptible-capacity content, so epic E13 was
on ⛔ HOLD. The owner decisions of 2026-09-20 (recorded in both backlogs and TECH_STACK §17) fix the
parameters.

## Decision

| Policy | Value |
|---|---|
| Production GPU baseline | **Blackwell 32 GB** running the current Qwen2.5-VL-7B in bf16 (no quantization needed to start) |
| Monthly envelope | **₹75,000** for Voice + CDI combined (alert at 80 %) |
| Topology | Two GPUs: a protected Voice GPU and an escalation-only CDI GPU; the Voice GPU never runs CDI work |
| Scheduling | CDI GPU on during upload hours, scaled to zero off-hours, pre-warmed before opening |
| Cost levers, in order | CPU-first routing (E2-S9) → schedule GPUs to clinic hours → batching where measured → quantization / smaller GPU only if headroom is needed → interruptible capacity only for overflow and only after failure tests |
| Production host | India-resident cloud with a Blackwell-class GPU (DPDP residency); RunPod remains dev / pilot |

## Constraints

- Provider rates are inputs; the cheapest configuration is named only after real quotes, and only
  among configurations that pass the E14-S5 production gate.
- Cost pressure never changes the no-guess rule.

## Consequences

- The benchmark (E13-S1), scheduling (E13-S3), interruptible-burst evaluation (E13-S4), cost
  dashboard (E13-S5), two-GPU topology (E13-S6) and India-resident hosting (E1-S10) are
  architecture-backed. E13-S6 still needs the Voice-project owner's sign-off; E1-S10 still needs a
  provider quote and legal residency sign-off (🔒, unchanged).
