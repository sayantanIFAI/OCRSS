# Traces and central logs (backlog E10-S3)

ARCHITECTURE §10.4 and §14 #9. **One trace id follows a document** from `POST /api/jobs`
through every pipeline stage and the model gateway to its FHIR bundle, and every log line of that
path carries the same id. Traces go to **Tempo**, logs to **Loki**, and both are viewed in the same
Grafana as the metrics and alerts (E10-S1, E10-S2).

## What one trace contains

| Span | Process | When | Attributes |
|---|---|---|---|
| `job` (root) | web app | from `POST /api/jobs` until the job leaves `running` | `cdi.job_id`, `cdi.job_state` |
| `stage.ingest` … `stage.validate` | web app | each stage of each document | `cdi.stage`, `cdi.document_id` (`stage.extract` of a batched call: `cdi.document_ids`) |
| `vlm.generate` / `vlm.generate_batch` | model gateway | each model call, a child of that document's `stage.extract` | `cdi.backend`, `cdi.max_tokens` / `cdi.items` |
| `fhir.project` | web app | the job's FHIR bundle (`GET /api/jobs/{id}/fhir`, *Generate EMR*): a later request, attached to the job's trace | `cdi.job_id` |

How the id travels:

- **Across threads:** the job runner hands the job's context to every pool thread explicitly (worker
  threads do not inherit it).
- **Across processes:** the web app sends the W3C `traceparent` header to the gateway, which continues
  the trace.
- **Across requests:** the FHIR projection uses the job's stored span context as its parent.

The stage spans come from the same timer as the `cdi_stage_seconds` metric (`metrics.stage`), so
traces and metrics always describe the same runs.

**No PHI in traces:** attributes are ids and fixed names only. A test runs a job whose file is
named after a patient and checks the name appears in no span.

## Find a document's trace

1. The job's `trace_id` is in the `POST /api/jobs` answer and in `GET /api/jobs/{id}`.
2. Grafana → Explore → **Tempo** → paste the trace id. You see the job, every document's stages,
   the model calls and the FHIR projection on one timeline.
3. On any span, **Logs for this span** opens Loki filtered to the same trace (Tempo → Loki link).
4. From a log line in Loki, **Open the trace** follows its `trace_id` to Tempo (Loki → Tempo link).

Useful Loki queries (LogQL):

```logql
{service_name="cdi-webapp"} | trace_id = "<trace id>"          # one job's web-app lines
{service_name=~"cdi-webapp|cdi-mlserve"} |= "extract_failed"      # failures across services
{service_name="cdi-mlserve"} |= "vlm_oom_fallback"                # the 7B -> 3B fallbacks
```

Or directly, on the host:

```bash
curl -s 127.0.0.1:3200/api/traces/<trace id> | python3 -m json.tool | less
curl -sG 127.0.0.1:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="cdi-webapp"}'
```

## Configuration

| Setting | Pod value | Meaning |
|---|---|---|
| `CDI_OTEL_TRACES_ENDPOINT` | `http://127.0.0.1:4318/v1/traces` | Tempo's OTLP/HTTP receiver; empty = no trace export |
| `CDI_OTEL_LOGS_ENDPOINT` | `http://127.0.0.1:3100/otlp/v1/logs` | Loki's OTLP ingestion; empty = no log export |

With both empty (tests, dev) spans are still created, so log lines still carry `trace_id` /
`span_id`; nothing is sent anywhere. Stdout logging is unchanged either way.

`bash infra/monitoring/start_monitoring.sh` installs and starts Tempo 3.0.3 and Loki 3.7.8
(pinned, sha256-checked, `versions.env`) next to Prometheus, Alertmanager and Grafana, all on
127.0.0.1. Both keep 30 days.

## Tests

`tests/test_tracing_unit.py` is the end-to-end trace-continuity test. A two-document job runs
through the real job runner, with the extract stage calling the real gateway app over HTTP, then
the web app's FHIR projection runs. It checks:

- every span is in **one** trace, the job's;
- each document has every stage under the job span;
- each document's gateway span is a child of its extract span (the `traceparent` hop);
- the FHIR span is under the job span;
- every log line of the job carries the job's trace id and its stage's span id.

It also checks that two jobs are two traces, that shipped log records carry their trace context,
and that no patient name appears in any span. `tests/test_monitoring_unit.py` checks the Tempo and
Loki configuration: loopback only, the OTLP ports the services use, pins, and the Grafana links.
