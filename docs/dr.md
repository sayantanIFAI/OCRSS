# Backups, restore and data-tier loss (backlog E10-S11)

This is the worst case: Postgres **and** its standby (E10-S6) are gone, or the MinIO data is lost.
Only the backups are left. This page covers what is backed up, how a restore is rehearsed every
week, the targets it is measured against, and what to do when it happens.

## Targets

| | Target | Why |
|---|---|---|
| **RPO** (data lost) | **20 minutes** | backups every 15 minutes, plus the time to dump and copy |
| **RTO** (time to a proven-working data tier) | **30 minutes** | a clean rebuild from the backups: empty servers, restore, integrity check, golden path |

The weekly drill measures both, and they are proposed targets for the owner to confirm. With the
hot standby (E10-S6), a failed primary costs far less: RPO under 1 second, and seconds to fail
over. These targets are for when the standby is lost too.

## What is backed up

Every 15 minutes, `infra/runpod/snapshot.sh` (cron) does this in the backup folder
(`CDI_BACKUP_DIR`, default `/workspace/backup`):

1. `pg_dump -Fc` writes `cdi.<time>.dump`. It is checked with `pg_restore --list` before it counts,
   and the last 10 are kept.
2. `python -m cdi_adapter.dr backup` copies every MinIO object not backed up yet (the original
   scans, rendered pages and thumbnails) to `objects/<key>`. Each copy is read back and its sha256
   entered in `objects.json`. Objects never change (keys are content addresses), so each run copies
   only the new ones.
3. `last_backup.json` records ok or failed, the dump's snapshot time, and the newest good backup.
   A failure is recorded too (`trap`), so the `CdiBackupStale` alert fires.

**Where the backup folder must live:** the default is on `/workspace`, the same volume as the live
data. That protects against a damaged database, a deleted object or a bad migration, but **not
against losing the volume**. For real disaster recovery, `CDI_BACKUP_DIR` must be off the pod,
on a mounted volume of the second host (E1-S3 / E10-S6), in the India region (§12, DPDP).

## The weekly restore drill

`python -m cdi_adapter.dr drill` (cron, Sundays at 03:30; `$WS/logs/dr.log`) works in a clean
throwaway environment and never touches the live data:

1. It starts a new, empty PostgreSQL cluster and restores the newest good dump into it
   (`pg_restore --exit-on-error`).
2. It brings the schema to Alembic head, so a dump from an older release must still come up.
3. It starts a new, empty MinIO and puts back every object, each checked against its sha256. A
   backup file that no longer matches is counted as corrupt and not restored.
4. It reads **every document's original scan** back from the restored MinIO and checks it
   against `source_document.sha256`, recorded at ingest. That proves rows and bytes still belong
   together.
5. It runs the golden path (E1-S6, `--mode seeded`) against the restored server, in a database of
   its own there.
6. It measures **RPO** (the dump's snapshot time to the moment of the simulated loss) and **RTO**
   (from the empty servers to all the above passing), and compares them with the targets.

The report goes to `$WS/logs/dr/restore-<time>.json` and `last.json`. The web app's `/metrics`
publishes it (`cdi_dr_restore_last_ok`, `cdi_dr_rpo_seconds`, `cdi_dr_rto_seconds`), and so does
the backup status (`cdi_backup_last_success_timestamp_seconds`). Alerts: `CdiBackupStale`,
`CdiRestoreDrillFailed` ("a backup that fails to restore raises an alert") and
`CdiRestoreDrillOverdue`. Grafana has a "Backups" panel.

`python -m cdi_adapter.dr status` shows the last backup and the last drill.

## When it happens: data-tier loss

1. **Stop intake.** Put the counter on paper for new documents, and do not let the web app write
   to a half-restored database.
2. **Is there a standby?** If the standby is alive, this is a failover, not a restore:
   `python -m cdi_adapter.ha pg failover` (`docs/ha.md`).
3. **Restore Postgres** on the rebuilt host:
   1. The bootstrap initialises an empty cluster and restores `/workspace/backup/cdi.dump`, the
      newest good dump, by itself (§3.3).
   2. If that dump is damaged, restore an older one:
      `pg_restore --no-owner -d <url> /workspace/backup/cdi.<time>.dump`.
   3. Then `alembic upgrade head`.
4. **Restore MinIO** (only if its data is lost):
   1. Start an empty MinIO.
   2. Put the objects back from the backup folder:
      `python -c "from pathlib import Path; from cdi_adapter.dr.drill import restore_objects; from cdi_adapter.storage import get_store; print(restore_objects(Path('/workspace/backup'), get_store()))"`.
   3. `corrupt_in_backup` must be 0.
5. **Check before opening:** run `python -m cdi_adapter.dr drill` (it proves the backups, and the
   restored live system matches), then the golden path.
6. **Find what was lost:** documents ingested after the dump's snapshot time are gone from the
   database. Their scans may still be in MinIO if MinIO survived, or on paper at the counter.
   Re-ingest them. **Human decisions made after that time are lost and must be redone** (below).
7. Record the incident with the measured RPO and RTO.

## What comes back and what does not

A restore returns the database as it was at the dump's snapshot time. What was done after it:

| Data | After a loss | How it comes back |
|---|---|---|
| Original scans (MinIO `original.*`) | from the object backup | re-ingest the paper copy if it was never backed up |
| Rendered pages and thumbnails (MinIO `pages/`) | reproducible | re-ingest renders them from the scan |
| `source_document`, `document_page` | lost after the snapshot | re-ingest the scan (the same sha256 is the same document) |
| `pipeline_run`, `ocr_block`, `doc_classification`, `extraction` | **reproducible** | re-run the pipeline on the scan (stages S2–S4) |
| `clinical_fact`, `medication_detail`, `fact_provenance`, `fact_conflict`, `encounter` (machine-made) | **reproducible** | re-run S4–S6 (the gate decides again) |
| `fhir_resource`, `fhir_bundle`, `cn_fhir_bundle` | **reproducible** | re-project (S9) |
| `ai_event`, `ai_suggestion`, `ai_clinical_verification` | **reproducible** | re-run the agentic steps |
| Reviewer decisions: `review_task` outcomes, facts `clinician_confirmed` / `corrected` / `rejected`, `rv_item`, `rv_item_element`, `rv_action` | **not reproducible** | the reviewers must redo them; the audit trail says who decided what up to the snapshot |
| `audit_log` | **not reproducible** | entries after the snapshot are gone; record the incident itself in the new log |
| Canonical EMR edits (`cn_condition`, `cn_medication`, `cn_lab_result`, `cn_vital_sign`, `cn_allergy`, `cn_clinical_note`) where a person edited them | **not reproducible** | redo from the paper record or the clinician |
| `patient_registry`, `patient_identity`, `patient_identity_alias` (manual links) | **not reproducible** | re-enter new patients; repeat manual identity links |
| `ops_patient_visit`, `fin_billing_item` | **not reproducible** | re-enter from the counter's records |
| `abdm_care_context`, `abdm_consent`, `abdm_transfer` | **not reproducible** | reconcile with the ABDM gateway's own records |
| `web_user`, `web_session` | users: not reproducible; sessions: dropped | recreate new accounts (`cdi_adapter.webapp.users`); everyone signs in again |
| `model_version`, `model_rollout` | **not reproducible** | re-register versions and redeploy (`docs/model-rollout.md`) |

The rule: **anything a machine derived from a scan is reproducible; anything a person decided,
entered or consented to is not**, and that part is what the RPO actually costs.

## Tests

- `tests/test_dr_integration.py` (real PostgreSQL 16):
  - the drill rebuilds a database from `pg_dump` and object backups alone, finds every scan
    intact, and passes the golden path;
  - a corrupted object backup fails the drill;
  - a truncated dump fails the drill.
- `tests/test_dr_unit.py`: safe backup paths, incremental and verified copies, the failure status,
  the corrupt-file refusal, the scan check, RPO input, the collector, and the snapshot and cron
  wiring.
- The three alerts have promtool cases (`infra/monitoring/rules/cdi-alerts.test.yml`).
- With a real MinIO the drill runs on Colab / the pod: `python -m cdi_adapter.dr drill --minio-bin …`.
