# Alerts and on-call responses (backlog E10-S2, E10-S4, E10-S6, E10-S9, E10-S11)

Eleven alert rules in `infra/monitoring/rules/cdi-alerts.yml`, evaluated by the local Prometheus
every 15 s and routed by Alertmanager (`infra/monitoring/alertmanager.yml`). The Grafana dashboard
"CDI-Adapter overview" shows them at the top. Metrics are described in `docs/metrics.md`.

| Alert | Severity | Fires when |
|---|---|---|
| [CdiSlaWindowBreach](#cdislawindowbreach) | page | a job is still running after 300 s, or one finished after 300 s in the last 10 min |
| [CdiGpuOom](#cdigpuoom) | page | any CUDA out-of-memory in the model gateway in the last 15 min |
| [CdiQueueBacklog](#cdiqueuebacklog) | page | more than 30 documents have waited for a GPU slot for 5 min |
| [CdiPartialRateAboveBaseline](#cdipartialrateabovebaseline) | ticket | the last hour's `_partial` share is over 1.5 × its 7-day rate + 5 points (at least 10 extractions) |
| [CdiPgReplicationLag](#cdipgreplicationlag) | page | the Postgres standby is more than 30 s behind, for 2 min |
| [CdiPgStandbyDisconnected](#cdipgstandbydisconnected) | ticket | a replication slot has had no standby on it for 5 min |
| [CdiMinioDegraded](#cdiminiodegraded) | ticket | a MinIO drive is offline, or a host of a multi-host set does not answer, for 2 min |
| [CdiBackupStale](#cdibackupstale) | page | no good backup (pg_dump + objects) for 45 min |
| [CdiRestoreDrillFailed](#cdirestoredrillfailed) | page | the last restore drill failed |
| [CdiRestoreDrillOverdue](#cdirestoredrilloverdue) | ticket | no restore drill for 8 days |
| [CdiConfidenceDrift](#cdiconfidencedrift) | ticket | the fact-confidence distribution of the last 6 h differs from the 7 days before it (Kolmogorov-Smirnov distance > 0.25, at least 50 facts), for 30 min |

**Never, under any alert:** lower a gate threshold, auto-accept facts, or skip review to get the
queue moving (§0.5: nothing is promoted because of SLA pressure). The SLA is met with governed
results **or an explicit hold with an ETA** (ADR-0002); a hold is always an acceptable answer.

## Start, stop, reach the UIs

```bash
bash infra/monitoring/start_monitoring.sh           # after start_all.sh; idempotent
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 <pod>   # from a workstation
python3 src/cdi_adapter/secretstore.py get cdi_grafana_admin_password           # Grafana `admin`
```

Everything listens on 127.0.0.1 only; nothing is published through the ingress.

## CdiSlaWindowBreach

A job took, or is taking, longer than the 5-minute SLA.

1. **Grafana → "Job time p95 vs the 5-minute SLA"** and **"Stage time p95"**: which stage grew?
   - `extract` grew: go to [CdiQueueBacklog](#cdiqueuebacklog) (the GPU is the bottleneck).
   - `ingest` / `ocr` grew: CPU contention. `top` on the host; check `CDI_JOB_MAX_WORKERS` and
     the ORT thread caps (§7.4 #4).
2. **"Extraction queue"**: `waiting` high with `in flight` at its limit means capacity. That is
   expected during the design burst (30 patients × 10 documents). Tell the counter the expected wait:
   the hold with an ETA is the SLA answer.
3. `curl -s 127.0.0.1:8077/healthz`: `loaded: false` after a restart means the next job pays the
   model load (~1 min on Colab, cached weights). Not a fault if it clears after one job.
4. If one job is stuck (the oldest running job keeps growing, nothing else moves), check
   `$WS/logs/webapp.log` for that job's last step. Restarting the web app loses in-memory job state
   (E9-S1 makes it durable): only after the counter has been told.
5. Record the cause in the incident log. Repeated breaches feed E9-S4 (autoscaling) and E13.

## CdiGpuOom

The gateway ran out of GPU memory. It never drops the work: a model load falls back 7B → 3B, a
batch is split into single calls (E2-S8), a single call fails and is retried by the client.

1. See where it happened, on the gateway's metrics:
   `curl -s 127.0.0.1:8077/metrics | grep cdi_vlm_oom_total` (the `where` label):
   - `load`: the 7B did not fit, the **3B is now serving**, and quality is lower. Check
     `curl -s 127.0.0.1:8077/healthz` (`model`). Find what else holds GPU memory (`nvidia-smi`) and
     restart the gateway (`bash infra/runpod/start_mlserve.sh` after stopping it) so the 7B loads.
   - `batch`: batches are too large for the free memory. Lower `CDI_EXTRACT_BATCH_MAX` (E2-S8)
     and restart the web app.
   - `generate`: a single page was too large. Lower `CDI_VLM_MAX_PIXELS_OCR` only after checking
     accuracy (E2-S7); the page image stays as evidence either way.
2. **Grafana → "GPU memory"**: memory near the total with no traffic means a leak or another
   process; `nvidia-smi` names it.
3. Documents extracted by the 3B while it served are marked with that model in their provenance
   (`extraction.model_name`); nothing extra is needed, the gate treats them the same.

## CdiQueueBacklog

More documents are waiting for extraction than can finish inside the SLA.

1. **Grafana → "Extraction queue"** and **"Jobs by state"**: a real burst, or is the queue stuck?
   - `in flight` at 0 while `waiting` is high: extraction is stuck. Check the gateway
     (`curl -s 127.0.0.1:8077/healthz`, `$WS/logs/mlserve.log`); restart it if it does not answer.
   - `in flight` at its limit: capacity. Tell the counter the expected wait (holds with an ETA).
2. With the `vllm` backend, check `CDI_EXTRACT_CONCURRENCY` / `CDI_VLLM_MAX_NUM_SEQS` (E2-S2).
   With `hf`, one extraction runs at a time by design; batching short documents
   (`CDI_EXTRACT_BATCH_MAX`, E2-S8) is the lever, once its A/B has shown a gain.
3. Do not start a second gateway on the same GPU: it only adds OOMs ([CdiGpuOom](#cdigpuoom)).
   Adding GPUs is E9-S4 / E13-S6.

## CdiPartialRateAboveBaseline

More model outputs than usual fail the schema (`_partial`). Safety is not at risk (the S6 gate
holds every `_partial` fact for review), but reviewers get more work and quality has regressed.

1. **What changed?** A model (`CDI_VLM_MODEL_ID`, the 3B fallback after an OOM), a prompt, a
   schema, or the documents themselves (a new clinic, a new form, poor scans).
   - `curl -s 127.0.0.1:8077/healthz`: is the 3B serving? Then see [CdiGpuOom](#cdigpuoom).
   - Recent deployments: roll back a model or prompt change (E10-S5 when it exists).
2. Look at the latest `_partial` extractions: `extraction` rows with `is_partial = true`, the
   `_errors` in their payload, `model_name`, and the document types. One type failing points at
   its schema or prompt.
3. Open a ticket with the doc types and error samples. No PHI in the ticket: use document ids.
4. The review backlog will grow: check **"Human review backlog"** and warn the reviewers.

## CdiConfidenceDrift

The shape of the calibrated confidence distribution changed within a day (backlog E10-S4). It is
drift, not an outage, and not a safety failure: the S6 gate still holds anything under its lines.
It is the earliest sign of a model, prompt, OCR or input change.

1. **Grafana → "Confidence drift: KS distance"** and **"Fact confidence quantiles"**: which way did
   it move?
   - **Down** (p10 / p50 lower): more facts will be held; the review backlog grows. Look for the
     cause below and warn the reviewers ("Human review backlog").
   - **Up** (p90 near 1.0 where it was not): more facts pass the auto-accept line. This is the case
     to be careful with. Check the audit sample (`audit_tasks` in the backlog) and the
     **human-override rate**: rising overrides on accepted facts mean the confidences are no longer
     calibrated. Tell the owner; recalibration is E4-S3.
2. **What changed?** The same checks as [CdiPartialRateAboveBaseline](#cdipartialrateabovebaseline):
   the model serving (`/healthz` model, a 3B fallback after an OOM), a prompt or schema change, the
   OCR engine, or the documents (a new clinic or form, scan quality). `_partial` is part of the
   calibration, so a `_partial` spike also moves confidence down.
3. **"Unmapped-code rate"** rising at the same time points at new vocabulary (a new specialty,
   brand names): the terminology seed map, E3-S2 / E3-S3.
4. Open a ticket with the direction, the fact types (`cdi_fact_confidence{fact_type}`) and the
   window. No PHI in the ticket.

The other two drift signals are trend lines on the same dashboard, with no alert: the
**human-override rate** (corrected or rejected ÷ all reviewer decisions, per day) and the
**unmapped-code rate** (`local_only` ÷ all terminology bindings, per day).

## CdiPgReplicationLag

The standby is more than 30 s behind the primary (backlog E10-S6). If the primary died now, the
failover would lose those commits: the RPO promise (under 1 minute) is at risk.

1. `python -m cdi_adapter.ha pg status`: is the standby `receiving`, and how far behind
   (`replay_lag_s`, `bytes_behind`)?
2. Receiving but slow: the standby cannot keep up (disk or CPU on its host, or a long query on it
   holding replay back). Check its host and `$WS/logs/postgres-standby.log`.
3. Not receiving: go to [CdiPgStandbyDisconnected](#cdipgstandbydisconnected).
4. Until it catches up, avoid planned primary restarts. Do **not** fail over to a lagging standby
   unless the primary is gone: that is exactly the data loss this alert warns about.

## CdiPgStandbyDisconnected

A replication slot has had no standby connected for 5 minutes. There is no failover target, and
the primary keeps every WAL segment for the missing standby, so its disk grows.

1. `python -m cdi_adapter.ha pg status`: is the standby up at all?
2. Down: start it (`pg_ctl -D <standby data> start` as `postgres`, or rerun the bootstrap). Its
   data folder gone (the overlay was wiped): `python -m cdi_adapter.ha pg setup-standby` builds it
   again from the primary.
3. Up but not streaming: `$WS/logs/postgres-standby.log` (replication password rotated? the
   standby reads it from `.pgpass-replication` next to the data folders; after a rotation rerun
   `pg setup-standby`: with the standby built, it only re-applies the password).
4. Standby retired on purpose: drop its slot (`SELECT pg_drop_replication_slot('cdi_standby')` on
   the primary), or the WAL keeps piling up.

## CdiMinioDegraded

MinIO is running with a drive offline, or (several hosts) a host not answering (backlog E10-S6). No
data is lost and uploads continue (erasure coding, write quorum 3 of 4), but **one more loss stops
writes** to the scan store.

1. `python -m cdi_adapter.ha minio status`: which process, and is the set still writable?
   `$WS/logs/minio-*.log` names the drive (`Unable to use the drive ...`).
2. A drive lost or corrupted: give MinIO an empty replacement at the same path (a new disk
   mounted there, or an empty folder). MinIO formats and heals it by itself; nothing to copy.
3. A host down (several hosts): start its process again (`minio start` on that host, with its
   `CDI_MINIO_NODE`). MinIO heals the shards it missed.
4. Watch `cdi_minio_drives_offline` return to 0 on the dashboard ("MinIO").

## CdiBackupStale

No good backup for 45 minutes (backlog E10-S11). `snapshot.sh` runs every 15 minutes; three have
failed or not run. A restore now would lose more than the RPO target (20 minutes).

1. `python -m cdi_adapter.dr status`: the `error` of the last run, and `last_success_at`.
2. `tail -50 $WS/logs/snapshot.log`: the failing step (`pg_dump`, the dump check, or the object
   copy).
3. Not running at all: `crontab -l` (the bootstrap installs it), and whether `cron` runs.
4. Disk full on `/workspace` (`df -h`): the dumps keep 10; old `restore-*.json` reports can go.
5. Run it by hand once fixed: `bash $REPO/infra/runpod/snapshot.sh`, then check the alert clears.

## CdiRestoreDrillFailed

The last restore drill could not rebuild the data tier from the backups, or missed its RPO or RTO
target (backlog E10-S11). **Treat the backups as untrusted until a drill passes.**

1. `python -m cdi_adapter.dr status`: the drill report's `checks` and `error` say which step failed
   (the full report: `$WS/logs/dr/last.json`).
2. By step:
   - `pg_restore` failed: the dump is damaged or does not match the tools; take a fresh
     snapshot now, and keep the previous dumps (`cdi.<time>.dump`).
   - Schema not at head: a migration does not apply to restored data. Fix it before the next
     release.
   - Scans missing or mismatched: the object backup lacks files that rows point to. Run a
     snapshot now; if it persists, compare `objects.json` with the store.
   - Golden path failed: read `golden_path_tail` in the report.
   - RPO over target: backups were late (see [CdiBackupStale](#cdibackupstale)).
   - RTO over target: the rebuild is too slow for the target; tell the owner.
3. Rerun the drill once fixed: `python -m cdi_adapter.dr drill` (a clean throwaway environment;
   the live data is never touched).

## CdiRestoreDrillOverdue

The weekly restore drill (Sunday 03:30, cron) has not finished for 8 days: the backups are
untested (backlog E10-S11).

1. `crontab -l`: the `cdi_adapter.dr drill` line (the bootstrap installs it).
2. `tail -50 $WS/logs/dr.log`: did it start, and where did it stop?
3. Run it now: `python -m cdi_adapter.dr drill`.

## CdiWebappInstanceDown

A web app instance has not answered its `/metrics` for 2 minutes (backlog E10-S9). Staff are not
affected yet: the balancer stopped sending it traffic within one health check (`/readyz`, every
2 s), and every job and session is in PostgreSQL, so the other instances serve everything. But
there is one spare less.

1. `bash $REPO/infra/runpod/start_apptier.sh status`: which instance (`webN`) is not ready.
2. `tail -50 $WS/logs/webapp-N.log`: why it stopped (a crash trace, `Killed` = out of memory).
3. Start it again: `bash $REPO/infra/runpod/start_apptier.sh` (starts only what is down; the
   balancer puts it back after two passing checks).
4. Every instance down at once, or it keeps dying: `/readyz` names the failing dependency
   (`db`, `s3`, `queue`); follow that dependency's alert.

## CdiJobsOrphaned

Accepted uploads are not being processed (backlog E10-S9). Their runner died and no other runner
took them over for 2 minutes. The uploads are safe (the files in MinIO under `uploads/`, the job in
`web_job`); nothing is lost, but the SLA clock is running.

1. `bash $REPO/infra/runpod/start_apptier.sh status`: are the job workers (`workerN`) up?
2. All down: `bash $REPO/infra/runpod/start_apptier.sh` starts them. Each new worker's sweep takes
   the orphaned jobs over at once (`job_orphan_claiming` in `$WS/logs/worker-N.log`) and resumes
   them: finished documents are kept, the rest run again from the staged files.
3. Workers are up but do not take the jobs: `grep job_heartbeat_failed $WS/logs/worker-*.log`
   (the database is refusing them: see `CdiPgReplicationLag` / the database's own state).
4. A job that keeps killing its worker is stopped after `CDI_JOB_MAX_ATTEMPTS` (3) runs: its
   documents show `gave up after 3 attempts`. Send the file to the EMR team; do not re-upload
   it in a loop.

## Tests (synthetic triggers)

`infra/monitoring/rules/cdi-alerts.test.yml` feeds synthetic series to each rule: every alert has
a case that must fire and one that must stay quiet (a short burst, jobs inside the SLA, a steady
baseline, too few extractions to judge). Run with the pinned promtool:

```bash
$WS/tools/prometheus-<version>/promtool test rules infra/monitoring/rules/cdi-alerts.test.yml
```

The data-tier drills (backlog E10-S6, `docs/ha.md`) run the real failovers: `python -m cdi_adapter.ha pg
drill` (crash the primary, measured RPO and RTO) and `python -m cdi_adapter.ha minio drill` (lose a drive, heal its
replacement).

The drift alert is also checked against an **injected shift in a staging Prometheus**
(`scripts/drift_staging_check.py`): 7 days of normal history plus a same-day shift are backfilled
with `promtool tsdb create-blocks-from openmetrics` into a throwaway Prometheus on 127.0.0.1:9091
that loads the real rule file; the alert must become active, and a control without the shift must
stay inactive. The live Prometheus is never touched.

`tests/test_monitoring_unit.py` runs the same when `promtool` is available (`CDI_PROMTOOL` or on
`PATH`), and also checks every metric a rule or dashboard panel uses exists in
`cdi_adapter.metrics`, every alert has a section here, and every service binds to 127.0.0.1.
