"""Prometheus metrics (backlog E10-S1, ARCHITECTURE §13, §14 #9).

Two processes expose metrics, each only on 127.0.0.1 (§12: internal services are never public):

* the **web app** (where jobs run): ``/metrics`` on its own loopback port ``CDI_METRICS_PORT``
  (9464), never on the staff-facing app, so the ingress can never serve it
* the **model gateway** (the GPU): ``/metrics`` on its existing loopback port (8077)

Labels come from fixed lists only (stage names, gate decisions, review actions, GPU index):
never a patient, document, file name or user, so no PHI can reach the metrics store.

| Metric | Type | What |
|---|---|---|
| ``cdi_stage_seconds{stage}`` | histogram | wall time of each pipeline stage per document |
| ``cdi_stage_failures_total{stage}`` | counter | stage runs that raised |
| ``cdi_extract_waiting`` | gauge | documents waiting for a GPU extraction slot (queue depth) |
| ``cdi_extract_in_flight`` | gauge | extraction calls holding a slot |
| ``cdi_jobs{state}`` | gauge | web-app jobs by state (in-memory until E9-S1) |
| ``cdi_review_backlog{queue}`` | gauge | open human work: held-fact tasks, workbasket, claimed |
| ``cdi_gate_decisions_total{decision}`` | counter | S6 gate: ``auto_accepted`` vs ``in_review`` |
| ``cdi_review_decisions_total{action}`` | counter | fact decisions: accept / correct / reject |
| ``cdi_review_items_total{action}`` | counter | review items: claim / approve / reject |
| ``cdi_vlm_generate_seconds{kind}`` | histogram | gateway call time, ``single`` or ``batch`` |
| ``cdi_vlm_requests_total{kind,outcome}`` | counter | gateway calls: ok / refused / error |
| ``cdi_gpu_utilization_percent{gpu}`` | gauge | GPU busy %, from ``nvidia-smi`` at scrape time |
| ``cdi_gpu_memory_used_bytes{gpu}``, ``_total_bytes`` | gauge | GPU memory |
| ``cdi_job_seconds`` | histogram | E10-S2: a job's upload -> review/hold time (the 5 min SLA) |
| ``cdi_job_oldest_running_seconds`` | gauge | E10-S2: age of the oldest job still running |
| ``cdi_vlm_oom_total{where}`` | counter | E10-S2: CUDA out-of-memory in the gateway |
| ``cdi_extractions_total{outcome}`` | counter | E10-S2: S4 extractions ``complete`` / ``partial`` |
| ``cdi_fact_confidence{fact_type}`` | histogram | E10-S4: calibrated confidence per fact (S6) |
| ``cdi_terminology_bindings_total{outcome}`` | counter | E10-S4: S5 ``bound`` / ``local_only`` |

Alert rules and dashboards on these are in ``infra/monitoring/`` (backlog E10-S2).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from cdi_adapter import tracing

STAGES = ("ingest", "ocr", "classify", "extract", "terminology", "validate")
# a page is seconds on the CPU path and up to minutes on the GPU (§7.4): 0.25 s .. 10 min
STAGE_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)

STAGE_SECONDS = Histogram("cdi_stage_seconds", "Wall time of one pipeline stage for one document",
                          ["stage"], buckets=STAGE_BUCKETS)  # fmt: skip
STAGE_FAILURES = Counter("cdi_stage_failures_total", "Pipeline stage runs that raised", ["stage"])
EXTRACT_WAITING = Gauge("cdi_extract_waiting", "Documents waiting for a GPU extraction slot")
EXTRACT_IN_FLIGHT = Gauge("cdi_extract_in_flight", "Extraction calls holding a GPU slot")
GATE_DECISIONS = Counter("cdi_gate_decisions_total", "S6 governance gate decisions per fact",
                         ["decision"])  # fmt: skip
REVIEW_DECISIONS = Counter("cdi_review_decisions_total", "Reviewer decisions on held facts",
                           ["action"])  # fmt: skip
REVIEW_ITEMS = Counter("cdi_review_items_total", "Reviewer actions on review items", ["action"])
# E5-S3: how a reviewer settled a multi-fact conflict (keep_a / keep_b / merge)
CONFLICTS_RESOLVED = Counter("cdi_conflicts_resolved_total",
                             "Multi-fact conflicts a reviewer settled", ["resolution"])  # fmt: skip
VLM_SECONDS = Histogram("cdi_vlm_generate_seconds", "Model gateway call time", ["kind"],
                        buckets=STAGE_BUCKETS)  # fmt: skip
VLM_REQUESTS = Counter("cdi_vlm_requests_total", "Model gateway calls", ["kind", "outcome"])
# E10-S2 alert inputs
SLA_SECONDS = 300  # ADR-0002: last upload -> governed JSON/FHIR or an explicit hold, within 5 min
JOB_BUCKETS = (30, 60, 120, 180, 240, SLA_SECONDS, 420, 600, 900, 1800)
JOB_SECONDS = Histogram("cdi_job_seconds", "Job time from upload to review, hold or stop",
                        buckets=JOB_BUCKETS)  # fmt: skip
VLM_OOM = Counter("cdi_vlm_oom_total", "CUDA out-of-memory events in the model gateway",
                  ["where"])  # fmt: skip
EXTRACTIONS = Counter("cdi_extractions_total", "S4 extractions by outcome", ["outcome"])
# E10-S4 drift inputs. The confidence buckets include the gate's own thresholds exactly
# (review floor 0.85, audit 0.95, auto-accept 0.985), so a shift across a gate line is visible.
FACT_TYPES = ("condition", "symptom", "finding", "lab_result", "vital_sign", "procedure",
              "medication", "allergy", "immunization", "diagnostic_report", "advice",
              "care_plan", "referral", "document")  # fmt: skip  # = the clinical_fact CHECK list
CONFIDENCE_BUCKETS = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.985, 1.0)
FACT_CONFIDENCE = Histogram("cdi_fact_confidence", "Calibrated fact confidence at the S6 gate",
                            ["fact_type"], buckets=CONFIDENCE_BUCKETS)  # fmt: skip
TERMINOLOGY = Counter("cdi_terminology_bindings_total", "S5 terminology binding per fact",
                      ["outcome"])  # fmt: skip

# E10-S5 canary: the same document outcomes per serving side, so the canary is compared live
# against the primary (the incumbent); role is "primary" or "canary", never a version name
MODEL_EXTRACTIONS = Counter("cdi_model_extractions_total", "S4 extractions per serving side",
                            ["role", "outcome"])  # fmt: skip
CANARY_FALLBACKS = Counter("cdi_model_canary_fallbacks_total",
                           "Failed canary calls sent to the primary instead")  # fmt: skip
CANARY_PERCENT = Gauge("cdi_model_canary_percent", "Share of documents routed to the canary (%)")
# E10-S9: a job taken over after its runner died (lease ran out), and gateway replica failover
JOB_RESUMES = Counter("cdi_job_resumes_total", "Stored jobs resumed by another run")
MLSERVE_FAILOVERS = Counter("cdi_mlserve_failovers_total",
                            "Model gateway calls moved to another replica")  # fmt: skip
# E9-S3: an upload answered with the job it already created (an idempotency-key hit), and work
# that stopped being retried and left the queue for the dead-letter table
JOB_DUPLICATES = Counter("cdi_job_duplicate_uploads_total",
                         "Uploads answered with an existing job (idempotency key)")  # fmt: skip
DEAD_LETTERS = Counter("cdi_dead_letters_total", "Work moved to the dead-letter queue", ["kind"])

# every label value is known up front, so each series exists (at 0) from the first scrape
for _s in STAGES:
    STAGE_SECONDS.labels(_s)
    STAGE_FAILURES.labels(_s)
for _d in ("auto_accepted", "in_review"):
    GATE_DECISIONS.labels(_d)
for _a in ("accept", "correct", "reject"):
    REVIEW_DECISIONS.labels(_a)
for _a in ("claim", "approve", "reject"):
    REVIEW_ITEMS.labels(_a)
for _c in ("keep_a", "keep_b", "merge"):
    CONFLICTS_RESOLVED.labels(_c)
for _w in ("load", "generate", "batch"):
    VLM_OOM.labels(_w)
for _o in ("complete", "partial"):
    EXTRACTIONS.labels(_o)
for _k in ("web_job", "document", "task"):
    DEAD_LETTERS.labels(_k)
for _o in ("bound", "local_only"):
    TERMINOLOGY.labels(_o)
for _f in (*FACT_TYPES, "other"):
    FACT_CONFIDENCE.labels(_f)
for _r in ("primary", "canary"):
    for _o in ("complete", "partial", "failed"):
        MODEL_EXTRACTIONS.labels(_r, _o)
for _k in ("single", "batch"):
    VLM_SECONDS.labels(_k)
    for _o in ("ok", "refused", "error"):
        VLM_REQUESTS.labels(_k, _o)


@contextmanager
def stage(name: str, document_id: str | None = None) -> Iterator[None]:
    """Times one stage run into ``cdi_stage_seconds`` (a raise also counts a failure) and records
    it as the span ``stage.<name>`` in the current trace (E10-S3), so metrics and traces cannot
    disagree about what ran."""
    t0 = time.perf_counter()
    attrs = {"cdi.stage": name, **({"cdi.document_id": document_id} if document_id else {})}
    with tracing.span(f"stage.{name}", attrs):
        try:
            yield
        except BaseException:
            STAGE_FAILURES.labels(name).inc()
            raise
        finally:
            STAGE_SECONDS.labels(name).observe(time.perf_counter() - t0)


def fact_type_label(fact_type: str | None) -> str:
    """The label for a fact type: one of ``FACT_TYPES``, else ``other`` (a fixed list, §12)."""
    return fact_type if fact_type in FACT_TYPES else "other"


# ------------------------------------------------------------------ collectors read at scrape time


class JobsCollector(Collector):
    """``cdi_jobs{state}`` and ``cdi_job_oldest_running_seconds``: a job breaching the SLA shows
    while it is still running, not only once it ends (E10-S2). Since E10-S9 read from ``web_job``
    (unfinished jobs + the last day's), so every web-app instance reports the same numbers, plus
    ``cdi_jobs_orphaned``: unfinished jobs whose runner's lease ran out (none alive to take them
    over when it stays above zero). A failed read yields nothing rather than wrong numbers."""

    STATES = ("running", "review", "mismatch", "error")

    def __init__(self, jobs: Callable[[], Any], clock: Callable[[], float] = time.time) -> None:
        self._jobs, self._clock = jobs, clock

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            jobs = list(self._jobs())
        except Exception:  # noqa: BLE001 - never break the scrape; absent, not zero
            return
        g = GaugeMetricFamily("cdi_jobs", "Web-app jobs by state", labels=["state"])
        counts = dict.fromkeys(self.STATES, 0)
        oldest = 0.0
        orphaned = 0
        now = self._clock()
        for job in jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
            if job.state == "running":
                oldest = max(oldest, now - job.started_at)
            orphaned += int(bool(getattr(job, "orphaned", False)))
        for state, n in counts.items():
            g.add_metric([state], n)
        yield g
        yield GaugeMetricFamily("cdi_job_oldest_running_seconds",
                                "Age of the oldest job still running", value=oldest)  # fmt: skip
        yield GaugeMetricFamily(
            "cdi_jobs_orphaned", "Unfinished jobs whose runner's lease ran out", value=orphaned
        )


BACKLOG_SQL = (
    "SELECT 'held_fact_tasks' AS q, count(*) FROM review_task "
    "WHERE status = 'open' AND kind <> 'audit_sample' "
    "UNION ALL SELECT 'audit_tasks', count(*) FROM review_task "
    "WHERE status = 'open' AND kind = 'audit_sample' "
    "UNION ALL SELECT 'workbasket', count(*) FROM rv_item WHERE status = 'workbasket' "
    "UNION ALL SELECT 'claimed', count(*) FROM rv_item WHERE status = 'claimed'"
)


class ReviewBacklogCollector(Collector):
    """``cdi_review_backlog{queue}``: the human queue, read from PostgreSQL at scrape time. A
    failed read yields nothing rather than a wrong number (and never breaks the scrape)."""

    def __init__(self, query: Callable[[], list[tuple[str, int]]] | None = None) -> None:
        self._query = query or _backlog_from_db

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            rows = self._query()
        except Exception:  # noqa: BLE001 - a scrape must not fail on a database hiccup
            return
        g = GaugeMetricFamily("cdi_review_backlog", "Open human review work", labels=["queue"])
        for q, n in rows:
            g.add_metric([q], n)
        yield g


def _backlog_from_db() -> list[tuple[str, int]]:  # pragma: no cover - needs PostgreSQL
    from sqlalchemy import text

    from cdi_adapter.db import session_scope

    with session_scope() as c:
        return [(r[0], int(r[1])) for r in c.execute(text(BACKLOG_SQL)).all()]


# E10-S6: replication as the primary sees it. A physical slot whose standby is not connected
# means no failover target and WAL piling up; the lag is the RPO at risk if the primary dies now.
REPLICATION_SQL = (
    "SELECT pg_is_in_recovery(), "
    "(SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming'), "
    "(SELECT coalesce(max(extract(epoch FROM replay_lag)), 0) FROM pg_stat_replication), "
    "(SELECT count(*) FROM pg_replication_slots WHERE slot_type = 'physical' AND NOT active)"
)


class PgReplicationCollector(Collector):
    """``cdi_pg_standbys_streaming``, ``cdi_pg_replication_lag_seconds``,
    ``cdi_pg_replication_slots_inactive`` (backlog E10-S6), read from the primary at scrape
    time. Nothing on a standby or when the read fails."""

    def __init__(self, query: Callable[[], tuple[bool, int, float, int]] | None = None) -> None:
        self._query = query or _replication_from_db

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            standby, streaming, lag, inactive = self._query()
        except Exception:  # noqa: BLE001 - a scrape must not fail on a database hiccup
            return
        if standby:
            return
        yield GaugeMetricFamily("cdi_pg_standbys_streaming", "Postgres standbys streaming WAL",
                                value=streaming)  # fmt: skip
        yield GaugeMetricFamily("cdi_pg_replication_lag_seconds",
                                "Replay lag of the slowest standby (s)", value=lag)  # fmt: skip
        yield GaugeMetricFamily("cdi_pg_replication_slots_inactive",
                                "Physical replication slots with no standby connected",
                                value=inactive)  # fmt: skip


def _replication_from_db() -> tuple[bool, int, float, int]:  # pragma: no cover - needs PostgreSQL
    from sqlalchemy import text

    from cdi_adapter.db import session_scope

    with session_scope() as c:
        r = c.execute(text(REPLICATION_SQL)).one()
    return bool(r[0]), int(r[1]), float(r[2]), int(r[3])


class MinioDrivesCollector(Collector):
    """``cdi_minio_drives`` / ``cdi_minio_drives_offline`` (backlog E10-S6): MinIO's own drive
    gauges from its loopback metrics endpoint (``MINIO_PROMETHEUS_AUTH_TYPE=public``, set by
    ``cdi_adapter.ha``). Nothing when MinIO does not publish them (the single-drive store)."""

    def __init__(self, fetch: Callable[[], str] | None = None) -> None:
        self._fetch = fetch or _minio_metrics_text

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            text = self._fetch()
        except Exception:  # noqa: BLE001 - a scrape must not fail on a storage hiccup
            return
        got: dict[str, float] = {}
        for line in text.splitlines():
            if line.startswith(("minio_cluster_drive_total", "minio_cluster_drive_offline_total")):
                name, _, value = line.rpartition(" ")
                got[name.split("{", 1)[0]] = float(value)
        if "minio_cluster_drive_total" not in got:
            return
        yield GaugeMetricFamily("cdi_minio_drives", "MinIO drives in the erasure set",
                                value=got["minio_cluster_drive_total"])  # fmt: skip
        offline = got.get("minio_cluster_drive_offline_total", 0.0)
        yield GaugeMetricFamily("cdi_minio_drives_offline", "MinIO drives offline", value=offline)


def _minio_metrics_text() -> str:  # pragma: no cover - needs MinIO
    import httpx

    from cdi_adapter.config import get_settings

    url = get_settings().s3_endpoint_url.split(",")[0].strip()
    r = httpx.get(f"{url}/minio/v2/metrics/cluster", timeout=3.0)
    r.raise_for_status()
    return r.text


def _ts(value: Any) -> float | None:
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


class DrCollector(Collector):
    """Backups and the restore drill (backlog E10-S11), from their status files at scrape time:
    ``cdi_backup_last_success_timestamp_seconds``, ``cdi_backup_last_ok``,
    ``cdi_dr_restore_last_timestamp_seconds``, ``cdi_dr_restore_last_ok``, ``cdi_dr_rpo_seconds``,
    ``cdi_dr_rto_seconds``. Nothing for a file that does not exist yet."""

    def __init__(
        self, read: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]] | None = None
    ) -> None:
        self._read = read or _dr_files

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            backup, drill = self._read()
        except Exception:  # noqa: BLE001 - a scrape must not fail on a status file
            return
        if backup:
            yield GaugeMetricFamily(
                "cdi_backup_last_ok",
                "The last backup run succeeded (1/0)",
                value=1 if backup.get("ok") else 0,
            )
            t = _ts(backup.get("last_success_at"))
            if t is not None:
                yield GaugeMetricFamily(
                    "cdi_backup_last_success_timestamp_seconds",
                    "Snapshot time of the newest good backup",
                    value=t,
                )
        if drill:
            yield GaugeMetricFamily(
                "cdi_dr_restore_last_ok",
                "The last restore drill passed (1/0)",
                value=1 if drill.get("ok") else 0,
            )
            t = _ts(drill.get("finished_at"))
            if t is not None:
                yield GaugeMetricFamily(
                    "cdi_dr_restore_last_timestamp_seconds",
                    "When the last restore drill finished",
                    value=t,
                )
            for key, name, doc in (
                ("rpo_s", "cdi_dr_rpo_seconds", "RPO measured by the drill"),
                ("rto_s", "cdi_dr_rto_seconds", "RTO measured by the drill"),
            ):
                if isinstance(drill.get(key), int | float):
                    yield GaugeMetricFamily(name, doc, value=float(drill[key]))


def _dr_files() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from pathlib import Path

    from cdi_adapter.config import get_settings
    from cdi_adapter.dr import backup, drill

    s = get_settings()
    return backup.read_status(Path(s.backup_dir)), drill.read_last(Path(s.dr_report_dir))


class MinioNodesCollector(Collector):
    """``cdi_minio_nodes`` / ``cdi_minio_nodes_online`` (backlog E10-S6): the MinIO nodes listed
    in ``CDI_S3_ENDPOINT_URL`` and how many answer their liveness check. Only with a cluster."""

    def __init__(self, urls: list[str] | None = None,
                 probe: Callable[[list[str]], int] | None = None) -> None:  # fmt: skip
        self._urls = urls
        self._probe = probe

    def collect(self) -> Iterator[GaugeMetricFamily]:
        from cdi_adapter.config import get_settings
        from cdi_adapter.ha import minio

        urls = self._urls
        if urls is None:
            urls = [u.strip() for u in get_settings().s3_endpoint_url.split(",") if u.strip()]
        if len(urls) < 2:
            return
        up = (self._probe or minio.online)(urls)
        yield GaugeMetricFamily("cdi_minio_nodes", "MinIO nodes configured", value=len(urls))
        yield GaugeMetricFamily("cdi_minio_nodes_online", "MinIO nodes answering", value=up)


NVIDIA_SMI = ("nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total",
              "--format=csv,noheader,nounits")  # fmt: skip
MIB = 1024 * 1024


def parse_nvidia_smi(out: str) -> list[tuple[str, float, float, float]]:
    """``index, util %, used MiB, total MiB`` lines -> (gpu, util %, used B, total B)."""
    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            rows.append((parts[0], float(parts[1]), float(parts[2]) * MIB, float(parts[3]) * MIB))
        except ValueError:  # "[N/A]" on some virtualised GPUs: skip that GPU, not the scrape
            continue
    return rows


def _nvidia_smi() -> str:  # pragma: no cover - needs an NVIDIA driver
    return subprocess.run(NVIDIA_SMI, capture_output=True, text=True, timeout=5,
                          check=True).stdout  # fmt: skip


class GpuCollector(Collector):
    """GPU utilization and memory from ``nvidia-smi`` (installed with every NVIDIA driver, so no
    extra library). No GPU or no driver: the metrics are simply absent."""

    def __init__(self, read: Callable[[], str] = _nvidia_smi) -> None:
        self._read = read

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            rows = parse_nvidia_smi(self._read())
        except Exception:  # noqa: BLE001 - no GPU here, or the driver is unhappy
            return
        util = GaugeMetricFamily("cdi_gpu_utilization_percent", "GPU busy %", labels=["gpu"])
        used = GaugeMetricFamily("cdi_gpu_memory_used_bytes", "GPU memory in use", labels=["gpu"])
        total = GaugeMetricFamily("cdi_gpu_memory_total_bytes", "GPU memory", labels=["gpu"])
        for gpu, u, m_used, m_total in rows:
            util.add_metric([gpu], u)
            used.add_metric([gpu], m_used)
            total.add_metric([gpu], m_total)
        yield from (util, used, total)


_registered: set[str] = set()


def register_once(name: str, collector: Collector, registry: Any = REGISTRY) -> None:
    """Registers a scrape-time collector once per process (app factories may run twice)."""
    if name in _registered:
        return
    registry.register(collector)
    _registered.add(name)
