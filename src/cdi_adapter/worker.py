"""Celery pipeline for the folder-watch / ingest-API path (ARCHITECTURE §3.1, TECH_STACK §5).

Broker + result backend: Redis (``CDI_REDIS_URL``). Each stage is a retryable task; the chain is
the five tasks TECH_STACK §5 names: ``ingest_file -> classify_document -> ocr_document ->
extract_document -> bind_document``. This path classifies **before** OCR (the classifier falls back
to a one-off RapidOCR hint, §5 S2), then OCR runs with the engine the classification picks.

It also runs the web app's jobs (backlog E10-S9, the core of E9-S1): task ``cdi.web_job``, started
as a *threads* pool so all of a worker's jobs share one runner and its GPU extraction slots
(E2-S2).

**Separate CPU and GPU pools (backlog E9-S2).** §7.3 measured extraction as 86% of wall time and
the only GPU-bound stage, so it has a queue - and therefore a worker pool - of its own:

=============  ==================================================  ==========================
queue          work                                                pool
=============  ==================================================  ==========================
``cdi``        folder-watch CPU stages: ingest, classify, OCR,      CPU (``role=cpu``)
               terminology binding
``cdi-web``    web-app jobs: Phase 1 of each document (§7.2)        CPU (``role=cpu``)
``cdi-gpu``    ``cdi.extract_document``, and the web job's Phase 2  GPU (``role=gpu``)
=============  ==================================================  ==========================

Each pool is started and sized on its own, so buying Phase-1 throughput never changes GPU spend
(:func:`queues_for_role`, :func:`concurrency_for_role`, ``CDI_WORKER_CPU_CONCURRENCY`` /
``CDI_WORKER_GPU_CONCURRENCY``)::

    celery -A cdi_adapter.worker.celery_app worker -Q cdi,cdi-web --pool threads --concurrency 6
    celery -A cdi_adapter.worker.celery_app worker -Q cdi-gpu     --pool threads --concurrency 1

A worker consuming every queue (``role=all``, ``make worker``, the compose stack) is the
single-worker deployment: the same code, both phases in the one process, nothing handed over.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from celery import Celery, chain, signals

from cdi_adapter.config import Settings, get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

# E9-S2: the queue of each pool. ``cdi`` keeps its name and all of its CPU stages, so a worker
# started the way it was before this story still does that work; only extraction has moved.
CPU_QUEUE = "cdi"
WEB_QUEUE = "cdi-web"
GPU_QUEUE = "cdi-gpu"
QUEUES_BY_ROLE = {
    "cpu": (CPU_QUEUE, WEB_QUEUE),
    "gpu": (GPU_QUEUE,),
    "all": (CPU_QUEUE, WEB_QUEUE, GPU_QUEUE),
}
# E9-S3: the priority lanes every sender and every worker must agree on (0 lowest … 9 highest)
PRIORITY_STEPS = 10
NORMAL_PRIORITY = 5


def broker_url(settings: Settings | None = None) -> str:
    """The broker this process talks to (E9-S8): ``CDI_BROKER_URL`` when it is set, else the
    Redis URL - so every deployment that says nothing keeps the broker it has."""
    s = settings or get_settings()
    return s.broker_url or s.redis_url


def broker_kind(url: str) -> str:
    """``redis`` or ``amqp`` (RabbitMQ). The two differ only in how priority and publishing are
    configured; the queues, routing, lanes and task code are the same."""
    scheme = url.split("://", 1)[0].lower()
    if scheme in ("amqp", "amqps", "pyamqp"):
        return "amqp"
    if scheme in ("redis", "rediss", "redis+socket", "socket"):
        return "redis"
    raise ValueError(f"unsupported broker {scheme!r}: this project runs on Redis or RabbitMQ")


def broker_conf(url: str) -> dict[str, Any]:
    """The Celery settings that depend on which broker is in use (E9-S8).

    * **Redis** has no priority of its own, so Celery emulates it with one list per step
      (``cdi-web``, ``cdi-web:1`` …) and a worker drains the higher list first - which is why the
      steps and the separator have to be identical on every sender and worker.
    * **RabbitMQ** has real priority queues (``x-max-priority``) and publisher confirms, so the
      lanes are declared on the queue and a publish is only reported as sent once the broker has
      taken it. That is exactly the durability difference the kill-test measures.
    """
    if broker_kind(url) == "amqp":
        return {"task_queue_max_priority": PRIORITY_STEPS - 1,
                "broker_transport_options": {"confirm_publish": True}}  # fmt: skip
    return {"broker_transport_options": {"priority_steps": list(range(PRIORITY_STEPS)),
                                         "sep": ":",
                                         "queue_order_strategy": "priority"}}  # fmt: skip


_s = get_settings()
_broker = broker_url(_s)
# the result backend stays on Redis whichever broker is used: the comparison in E9-S8 is about the
# broker, and an AMQP result backend is deprecated in Celery anyway
celery_app = Celery("cdi", broker=_broker, backend=_s.redis_url)
celery_app.conf.update(
    task_default_queue=CPU_QUEUE,
    task_acks_late=True,  # a crashed worker's task is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # every stage but extraction is CPU work and stays on the default queue (E9-S2)
    task_routes={
        "cdi.extract_document": {"queue": GPU_QUEUE},
        "cdi.web_job": {"queue": WEB_QUEUE},
        # advisory, outside the job's critical path, so it is not worth a GPU-pool slot of its own
        "cdi.medication_agent": {"queue": WEB_QUEUE},
    },
    # E9-S3 priority lanes; lane 5 is normal, so with nothing set the behaviour is what it was
    task_default_priority=NORMAL_PRIORITY,
    task_inherit_parent_priority=True,  # a chained stage keeps its document's lane
    **broker_conf(_broker),
)


def queues_for_role(role: str) -> str:
    """The ``-Q`` list for a worker of this pool role (E9-S2); the start scripts read it, so the
    queue names live here only."""
    if role not in QUEUES_BY_ROLE:
        raise ValueError(f"unknown worker role {role!r} (one of {sorted(QUEUES_BY_ROLE)})")
    return ",".join(QUEUES_BY_ROLE[role])


def role_for_queues(consumed: set[str]) -> str:
    """The pool role a worker consuming these queues serves. Derived, not configured, so a
    worker's role can never contradict the work it actually takes (``CDI_WORKER_ROLE`` overrides
    it when it is set)."""
    if GPU_QUEUE in consumed:
        return "all" if consumed & {CPU_QUEUE, WEB_QUEUE} else "gpu"
    return "cpu"


def concurrency_for_role(role: str, settings: Settings | None = None) -> int:
    """Celery ``--concurrency`` for a worker of this pool role (E9-S2). 0 in the settings means
    auto: the CPU pool takes ``job_max_workers`` (Phase 1 is I/O- and OCR-bound), the GPU pool
    takes ``extract_slots()`` - what the model gateway itself allows (E2-S2). A worker that serves
    both is bounded by the GPU, as the single worker was before this story."""
    from cdi_adapter.webapp.jobs import extract_slots

    s = settings or get_settings()
    if role == "cpu":
        return s.worker_cpu_concurrency or s.job_max_workers
    return s.worker_gpu_concurrency or extract_slots(s)


MAX_RETRIES = 3
RETRY_BACKOFF = 10  # seconds, doubled per attempt


def dead_letter(kind: str, ref: str, *, task: str, attempts: int, error: str,
                queue: str | None = None,
                detail: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Put work that has failed every attempt in the dead-letter queue (backlog E9-S3,
    migration 0022). Recording it must never be what fails: a poison document has to leave the
    queue either way, and the log carries it if the write cannot."""
    from cdi_adapter import metrics, repo
    from cdi_adapter.db import session_scope

    try:
        with session_scope() as c:
            repo.write_dead_letter(c, kind=kind, ref=str(ref), attempts=attempts, error=error,
                                   queue=queue, task=task, detail=detail)  # fmt: skip
        metrics.DEAD_LETTERS.labels(kind).inc()
        log.error("dead_lettered", kind=kind, ref=str(ref), task=task, attempts=attempts,
                  error=error)  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        log.error("dead_letter_failed", kind=kind, ref=str(ref), task=task,
                  error=f"{type(exc).__name__}: {exc}")  # fmt: skip


def _retry(task, exc: Exception, *, kind: str = "document", ref: str | None = None) -> Any:
    """Retry with backoff; on the attempt that exhausts ``MAX_RETRIES``, dead-letter it first.

    A poison document used to end its run as a bare ``MaxRetriesExceededError`` with nothing to
    look at. Now the last attempt records a ``dead_letter`` row (E9-S3) and then raises the
    document's own error: the task fails, so its chain stops and the queue moves on, and what was
    given up is on a list that can be looked at and replayed. Never returns.
    """
    attempts = task.request.retries + 1
    if attempts > MAX_RETRIES:
        args = getattr(task.request, "args", None) or []
        dead_letter(kind, str(ref if ref is not None else (args[0] if args else "unknown")),
                    task=task.name, attempts=attempts, error=f"{type(exc).__name__}: {exc}",
                    queue=(getattr(task.request, "delivery_info", None) or {}).get(
                        "routing_key"))  # fmt: skip
        raise exc
    countdown = RETRY_BACKOFF * (2**task.request.retries)
    raise task.retry(exc=exc, countdown=countdown, max_retries=MAX_RETRIES)


@celery_app.task(bind=True, name="cdi.ingest_file", max_retries=MAX_RETRIES)
def ingest_file(self, path: str, source_channel: str = "folder") -> str:
    from cdi_adapter.ingest.service import ingest_bytes

    p = Path(path)
    try:
        res = ingest_bytes(p.read_bytes(), p.name, source_channel=source_channel)
    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc, kind="task", ref=path)  # no document id yet: the file is the ref
    log.info("folder_ingested", path=path, document_id=str(res.document_id),
             deduplicated=res.deduplicated)  # fmt: skip
    return str(res.document_id)


@celery_app.task(bind=True, name="cdi.classify_document", max_retries=MAX_RETRIES)
def classify_document(self, document_id: str) -> str:
    from cdi_adapter.classify.service import classify_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.ocr_document", max_retries=MAX_RETRIES)
def ocr_document(self, document_id: str) -> str:
    from cdi_adapter.ocr.service import ocr_document as run

    try:
        run(document_id)  # engine chosen from the classification
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.extract_document", max_retries=MAX_RETRIES)
def extract_document(self, document_id: str) -> str:
    from cdi_adapter.extract.service import extract_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


@celery_app.task(bind=True, name="cdi.bind_document", max_retries=MAX_RETRIES)
def bind_document(self, document_id: str) -> str:
    from cdi_adapter.terminology.service import bind_document as run

    try:
        run(document_id)
    except Exception as exc:  # noqa: BLE001
        _retry(self, exc)
    return document_id


def pipeline_after_ingest():
    """The stages that follow ingest, as a Celery chain signature."""
    return chain(classify_document.s(), ocr_document.s(), extract_document.s(),
                 bind_document.s())  # fmt: skip


def submit_file(path: str | Path, source_channel: str = "folder"):
    """Queue one file through the whole pipeline."""
    return chain(ingest_file.s(str(path), source_channel), pipeline_after_ingest()).apply_async()


def submit_document(document_id: str):
    """Queue an already-ingested document (the ingest API) through the rest of the pipeline."""
    return chain(classify_document.s(document_id), ocr_document.s(), extract_document.s(),
                 bind_document.s()).apply_async()  # fmt: skip


# ------------------------------------------------------------------ web-app jobs (E10-S9)


@celery_app.task(name="cdi.web_job", acks_late=True, ignore_result=True)
def web_job(job_id: str, handoff: bool = False) -> str:
    """Run (or resume) one stored web-app job to its end. The message only says "run this job":
    the job itself is the ``web_job`` row plus its staged uploads, and the claim decides who runs
    it, so a duplicated or redelivered message is a no-op and a lost one is covered by the sweep.

    The same task serves both pools (E9-S2): on ``cdi-web`` it runs Phase 1 and, when this worker
    is CPU-only, puts the job on ``cdi-gpu`` and returns; the message that arrives there carries
    ``handoff=True`` and the run continues at extraction."""
    from cdi_adapter.webapp.jobs import get_runner

    return get_runner().run_stored(job_id, handoff=handoff)


@celery_app.task(name="cdi.medication_agent", acks_late=True, ignore_result=True)
def medication_agent(document_id: str, actor: str = "agent") -> dict[str, Any]:
    """The medication agent on one validated document (backlog E12-S4): queued after the web job
    files the document for review, and by the Reviewer's "Run agent again". Advisory only; a
    failure is recorded on its ``agent`` run and never touches the document or its review."""
    from cdi_adapter.agent.medication import run_medication_agent

    try:
        return run_medication_agent(document_id, actor=actor)
    except Exception as exc:  # noqa: BLE001 - recorded on the run; nothing to retry into
        return {"failed": f"{type(exc).__name__}: {exc}"}


@signals.worker_init.connect
def _start_web_runner(sender: Any = None, **_: Any) -> None:
    """A worker that runs web-app jobs - either half of them (E9-S2: ``cdi-web`` is Phase 1,
    ``cdi-gpu`` is Phase 2) - starts its runner with the lease heartbeat and the orphan sweep at
    once (not on its first task), plus its tracing and ``/metrics`` port. The runner is given the
    role its own queues imply, so a CPU-only worker hands extraction on and a worker that consumes
    ``cdi-gpu`` does it."""
    queues = getattr(getattr(getattr(sender, "app", None), "amqp", None), "queues", None)
    if queues is None:
        return
    consumed = set(queues.consume_from or {})
    if not consumed & {WEB_QUEUE, GPU_QUEUE}:
        return
    from cdi_adapter import tracing
    from cdi_adapter.logging import configure
    from cdi_adapter.webapp.jobs import get_runner

    configure()
    tracing.setup_from_settings(tracing.WORKER)
    port = get_settings().worker_metrics_port
    if port:
        from prometheus_client import start_http_server

        start_http_server(port, addr="127.0.0.1")
    runner = get_runner(role=get_settings().worker_role or role_for_queues(consumed))
    log.info("web_runner_started", runner=runner.owner, slots=runner.extract_slots,
             role=runner.role, queues=sorted(consumed))  # fmt: skip
