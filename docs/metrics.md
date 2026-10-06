# Prometheus metrics (backlog E10-S1)

ARCHITECTURE §13 and §14 #9. The web app and the model gateway expose Prometheus metrics. The
Prometheus server, dashboards and alert rules are E10-S2.

## Where the metrics are

| Process | URL | Bound to |
|---|---|---|
| Web app (where jobs run) | `http://127.0.0.1:9464/metrics` | 127.0.0.1 only, its own port `CDI_METRICS_PORT` |
| Model gateway (the GPU) | `http://127.0.0.1:8077/metrics` | 127.0.0.1 only, the gateway's own port |

Neither is public. The web app does **not** serve `/metrics` on the staff-facing port (8888): that
port is what the ingress (E1-S9) and the RunPod proxy expose. A separate loopback port can never be
reached through them, so no route, sign-in rule or ingress rule is needed to protect it.
`CDI_METRICS_PORT=0` turns the web app's metrics server off.

Prometheus runs on the same host and scrapes both:

```yaml
scrape_configs:
  - job_name: cdi-webapp
    static_configs: [{targets: ["127.0.0.1:9464"]}]
  - job_name: cdi-mlserve
    static_configs: [{targets: ["127.0.0.1:8077"]}]
```

## The metrics

| Metric | Type | Meaning |
|---|---|---|
| `cdi_stage_seconds{stage}` | histogram | Wall time of one pipeline stage for one document: `ingest`, `ocr`, `classify`, `extract`, `terminology`, `validate`. Buckets 0.25 s to 10 min. `extract` covers the GPU work only; the wait for a slot is `cdi_extract_waiting` |
| `cdi_stage_failures_total{stage}` | counter | Stage runs that raised |
| `cdi_extract_waiting` | gauge | **Queue depth:** documents that finished Phase 1 and are waiting to start extraction |
| `cdi_extract_in_flight` | gauge | Extraction calls holding a GPU slot (a batched call, E2-S8, counts once) |
| `cdi_jobs{state}` | gauge | Web-app jobs by state: `running`, `review`, `mismatch`, `error`. Since E10-S9 read from `web_job` (unfinished jobs and the last day's), so every web app instance reports the same numbers: take `max`, never `sum` |
| `cdi_review_backlog{queue}` | gauge | Open human work, read from PostgreSQL at scrape time: `held_fact_tasks`, `audit_tasks`, `workbasket`, `claimed` |
| `cdi_gate_decisions_total{decision}` | counter | S6 gate per fact: `auto_accepted` vs `in_review` |
| `cdi_review_decisions_total{action}` | counter | **Reviewer throughput** on held facts: `accept`, `correct`, `reject` |
| `cdi_review_items_total{action}` | counter | Review items: `claim`, `approve`, `reject` |
| `cdi_vlm_generate_seconds{kind}` | histogram | Model gateway call time, `single` or `batch` |
| `cdi_vlm_requests_total{kind,outcome}` | counter | Gateway calls: `ok`, `refused` (a schema the grammar cannot lock, HTTP 422), `error` |
| `cdi_gpu_utilization_percent{gpu}` | gauge | GPU busy %, from `nvidia-smi` at scrape time |
| `cdi_gpu_memory_used_bytes{gpu}`, `cdi_gpu_memory_total_bytes{gpu}` | gauge | GPU memory |
| `cdi_job_seconds` | histogram | E10-S2: a job's time from upload to review, hold or stop. Buckets 30 s to 30 min, with 300 s (the ADR-0002 SLA) exact |
| `cdi_job_oldest_running_seconds` | gauge | E10-S2: age of the oldest job still running, so an SLA breach shows while it happens |
| `cdi_vlm_oom_total{where}` | counter | E10-S2: CUDA out-of-memory in the gateway: `load` (7B -> 3B fallback), `batch` (split into singles), `generate` (a call failed) |
| `cdi_extractions_total{outcome}` | counter | E10-S2: S4 extractions, `complete` or `partial` (schema-invalid, held for review) |
| `cdi_fact_confidence{fact_type}` | histogram | E10-S4: calibrated confidence per fact at the S6 gate. Buckets 0.5 to 1.0, with the gate lines exact (0.95 audit, 0.985 auto-accept); `fact_type` is from the fixed `metrics.FACT_TYPES` list or `other`. Feeds the drift alert CdiConfidenceDrift |
| `cdi_model_extractions_total{role,outcome}` | counter | E10-S5: S4 extractions per serving side (`primary` / `canary`): `complete`, `partial`, `failed` |
| `cdi_model_canary_fallbacks_total` | counter | E10-S5: canary calls that failed (unreachable / 5xx) and were answered by the primary |
| `cdi_model_canary_percent` | gauge | E10-S5: the live canary share of documents (%) |
| `cdi_pg_standbys_streaming`, `cdi_pg_replication_lag_seconds`, `cdi_pg_replication_slots_inactive` | gauge | E10-S6: replication as the primary sees it, read at scrape time (nothing on a standby) |
| `cdi_minio_nodes`, `cdi_minio_nodes_online` | gauge | E10-S6: MinIO nodes listed in `CDI_S3_ENDPOINT_URL` and how many answer (only with a cluster) |
| `cdi_minio_drives`, `cdi_minio_drives_offline` | gauge | E10-S6: MinIO's own drive gauges (its loopback `/minio/v2/metrics/cluster`), with an erasure set |
| `cdi_jobs_orphaned` | gauge | E10-S9: accepted jobs whose runner's lease ran out and that no runner has taken over yet (alert `CdiJobsOrphaned`) |
| `cdi_job_resumes_total` | counter | E10-S9: stored jobs taken over and resumed after their runner died |
| `cdi_mlserve_failovers_total` | counter | E10-S9: model gateway calls moved to another replica (one refused the connection, dropped it, or answered 502/503/504) |
| `cdi_terminology_bindings_total{outcome}` | counter | E10-S4: S5 terminology per fact, `bound` (a standard code) or `local_only` (unmapped). The unmapped-code rate on the dashboard |

Plus the client library's own `process_*` and `python_*` metrics.

Useful queries:

```promql
histogram_quantile(0.95, sum by (le, stage) (rate(cdi_stage_seconds_bucket[15m])))   # p95 per stage
sum(rate(cdi_gate_decisions_total{decision="auto_accepted"}[1h]))
  / sum(rate(cdi_gate_decisions_total[1h]))                                         # auto-accept share
sum(rate(cdi_review_decisions_total[1h])) * 3600                                     # decisions per hour
```

## Rules the metrics follow

- **No PHI.** Every label comes from a fixed list (stage, state, queue, decision, action, kind,
  outcome, where, GPU index). A patient, document, file or user name is never a label, and a test fails
  if any other label appears.
- **A scrape never fails because of a dependency.** If PostgreSQL does not answer,
  `cdi_review_backlog` is left out of that scrape. If there is no GPU or `nvidia-smi` fails, the GPU
  metrics are left out. The metric is absent rather than a wrong 0.
- **GPU readings need no extra library.** `nvidia-smi` is installed with every NVIDIA driver.
  (`nvidia-ml-py` would fall under the lock's `nvidia-*` torch-family rule, which assumes the
  torch image supplies it; it does not.)
- **Counters restart at 0** when a process restarts; Prometheus `rate()` handles that. The gate and
  review counters count in the process that did the work, which is the web app on the counter path.

## Checks on the pod (DoD-DEPLOY)

After `start_all.sh`:

```bash
curl -s http://127.0.0.1:8888/healthz
curl -s http://127.0.0.1:9464/metrics | grep -E '^cdi_(jobs|extract_waiting|stage_seconds_count)'
curl -s http://127.0.0.1:8077/metrics | grep -E '^cdi_gpu_utilization_percent'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8888/metrics   # not 200: staff app has no metrics
```

Then run one job and check that `cdi_stage_seconds_count` rose by one for each stage, and that
`cdi_gpu_utilization_percent` moved during extraction.

## Prometheus, Alertmanager and Grafana (backlog E10-S2)

`bash infra/monitoring/start_monitoring.sh` installs the pinned, sha256-checked releases
(`versions.env`) and starts all three on 127.0.0.1: Prometheus `:9090` (scrapes both endpoints,
30-day retention), Alertmanager `:9093`, Grafana `:3000` with the "CDI-Adapter overview" dashboard.
The four alert rules and their on-call responses: `docs/alerts.md`. Traces and central logs
(Tempo, Loki; backlog E10-S3): `docs/tracing.md`.

## Not in this block

- Since E10-S9 the web app's jobs run on Celery workers (queue `cdi-web`). Each such worker serves
  its own `/metrics` on `127.0.0.1:CDI_WORKER_METRICS_PORT + i - 1` (the pipeline's stage, queue
  and gate metrics now come from there), and each web app instance on `CDI_METRICS_PORT + i - 1`.
  `start_apptier.sh` writes both lists to `$WS/monitoring/targets/`, which Prometheus reads
  (`file_sd_configs`). A folder-watch worker (queue `cdi`) still has no metrics endpoint.
