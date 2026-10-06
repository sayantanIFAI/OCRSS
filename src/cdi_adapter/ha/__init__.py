"""Data-tier high availability (backlog E10-S6; runbook ``docs/ha.md``).

- ``pg``: a Postgres hot standby by streaming replication (physical slot, so the primary keeps
  every WAL segment the standby has not received), status and lag, fenced failover, rejoin
- ``minio``: a distributed, erasure-coded MinIO cluster (4 nodes by default: any one can be lost
  without losing data or writes), node control and a verified copy from the single-node store
- ``python -m cdi_adapter.ha``: the operator commands

The app side needs no HA code of its own: ``CDI_DATABASE_URL`` lists both Postgres servers with
``target_session_attrs=read-write`` (libpq connects to whichever is writable), and
``CDI_S3_ENDPOINT_URL`` lists the MinIO nodes (``cdi_adapter.storage`` moves to the next one when a
node stops answering).
"""
