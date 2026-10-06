"""Backups and the restore / DR drill (backlog E10-S11; runbook ``docs/dr.md``).

- ``backup``: every 15 minutes (``infra/runpod/snapshot.sh``) the ``pg_dump`` is joined by an
  incremental, sha256-checked copy of every MinIO object (the scans and pages), and a status file
  the monitoring reads
- ``drill``: rebuild Postgres and MinIO in a clean environment from the backups alone, prove every
  original scan byte-identical, run the golden path on it, and measure RPO and RTO against the
  documented targets (weekly, from cron)
- ``python -m cdi_adapter.dr backup | drill | status``
"""
