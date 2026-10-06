"""Model registry, canary routing and rollback (backlog E10-S5; runbook ``docs/model-rollout.md``).

- ``registry``: registered model versions and the append-only rollout log (migration 0006)
- ``router``: which gateway, and so which model version, extracts a given document
- ``compare``: the canary against the incumbent on live documents, and the promotion gate
- ``python -m cdi_adapter.modelops``: the operator commands (register, deploy, canary, promote,
  rollback, ...)
"""
