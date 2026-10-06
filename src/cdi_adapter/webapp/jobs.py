"""Web-app job model (ARCHITECTURE §7.2): parallel, then serial, then human review.

* **Phase 1 (parallel)** — ``ThreadPoolExecutor(max(2, CDI_JOB_MAX_WORKERS))``: every document runs
  ``ingest -> OCR(rapidocr) -> classify -> [OCR(vlm) if handwritten]`` concurrently.
* **Phase 2** — ``extract -> terminology -> validate``. How many extractions run at once is
  :func:`extract_slots` (backlog E2-S2): **one with ``hf``**, which does one forward pass at a
  time, and up to ``vllm_max_num_seqs`` with ``vllm``, which batches concurrent requests. The
  slots are shared by every job on the GPU. When no patient was chosen, the first document runs
  alone and fixes the patient; the other documents then attach to it in parallel.
* The job then stops at ``state = review`` (it does **not** auto-project). A registry patient
  chosen on the form arms the mismatch guard: a document for someone else stops the job with
  ``state = mismatch`` and its facts purged.

**Durable jobs (backlog E10-S9, with the core of E9-S1).** A runner built with a
:class:`JobStore` (``webapp.jobstore.PgJobStore``: the ``web_job`` table + the staged uploads) runs
*stored* jobs: :meth:`JobRunner.run_stored` claims a job with a lease, saves the job at every step
boundary, and renews the lease from a background thread while it runs. A runner that dies stops
renewing; once the lease runs out, any other runner's sweep claims the job and resumes it:
documents that finished stay as they are, a document past extraction continues from there, and
any other document runs again from its staged upload (ingest is idempotent by SHA-256; OCR and
extraction replace their earlier output). A runner that finds its lease taken over stops at its
next step boundary (:class:`LeaseLost`), so two runners never both finish a job. Without a
store (unit tests) jobs live only in ``_jobs``, as before.

**Separate CPU and GPU pools (backlog E9-S2).** Phase 1 is CPU work; Phase 2 holds the one
GPU-bound stage. In queue mode they run on different Celery pools, so each is sized and paid for
on its own: a runner whose ``role`` is ``cpu`` runs Phase 1, saves it, gives its lease up and puts
the job on the GPU queue (``cdi-gpu``); a runner whose ``role`` is ``gpu`` claims it and continues
at Phase 2 - nothing is ingested or OCR'd twice, because :func:`phase1_done` says that work is
already in the database and no staged upload is needed for it. A runner whose role is ``all`` (the
default, and every in-process runner) does both phases itself, exactly as before. The same is true
of the folder-watch chain: ``cdi.extract_document`` is routed to ``cdi-gpu`` and its other stages
stay on ``cdi`` (:mod:`cdi_adapter.worker`).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from cdi_adapter import metrics, tracing
from cdi_adapter.config import Settings, get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

STEPS = ("ingest", "ocr", "classify", "extract", "terminology", "validate")
UNFINISHED = ("pending", "running")
# E9-S2: Phase 1, the CPU stages of one document (the handwriting VLM pass is part of "classify":
# it runs before that step is recorded). PHASE2 is the GPU stage and what follows it.
PHASE1 = ("ingest", "ocr", "classify")
# which side of the split a runner serves; "all" = both, as every runner did before E9-S2
ROLES = ("all", "cpu", "gpu")


class LeaseLost(BaseException):  # noqa: N818 - a signal, not an error
    """This runner's lease on a job ran out and another runner claimed it (E10-S9). A
    BaseException on purpose: the per-document ``except Exception`` handlers must not turn it
    into a document error; it unwinds the whole run, which then writes nothing more."""


def extract_slots(settings: Settings | None = None) -> int:
    """Phase-2 extractions allowed in flight at once, across all jobs (backlog E2-S2, §7.2).

    ``hf`` is always 1: it does one forward pass at a time, so more would only queue on the GPU
    (or OOM). ``vllm`` batches concurrent requests: ``CDI_EXTRACT_CONCURRENCY``, or when that is 0
    (auto) the engine's own ``vllm_max_num_seqs``. ``stub`` is 1 unless set explicitly.
    """
    s = settings or get_settings()
    if s.mlserve_backend == "hf":
        return 1
    if s.extract_concurrency:
        return (
            min(s.extract_concurrency, s.vllm_max_num_seqs)
            if s.mlserve_backend == "vllm"
            else s.extract_concurrency
        )
    return s.vllm_max_num_seqs if s.mlserve_backend == "vllm" else 1


def phase1_done(d: DocState) -> bool:
    """True when this document is through Phase 1 and that work is durable (backlog E9-S2): it
    has been ingested, and its OCR text and classification are in the database. Extraction can
    then run in another process - the GPU pool, or a later resume - without the staged upload.
    """
    return d.document_id is not None and all(step in d.steps_done for step in PHASE1)


@dataclass
class Stages:
    """The pipeline stage functions (the real ones by default)."""

    ingest: Callable[..., Any]
    ocr: Callable[..., Any]
    classify: Callable[..., Any]
    extract: Callable[..., Any]
    terminology: Callable[..., Any]
    validate: Callable[..., Any]
    counts: Callable[[str], dict[str, int]]
    merge_identity: Callable[[Any], Any]
    # a validated document lands in the shared /reviewer workbasket (§4.5)
    enqueue: Callable[[str, str], Any] = lambda document_id, job_id: None
    # E2-S8: several documents' extraction in one batched model call, and the page / OCR-block
    # counts that decide whether a document is short enough to join a batch
    extract_batch: Callable[..., list[Any]] | None = None
    shape: Callable[[str], dict[str, int]] | None = None
    # the medication agent (E12-S4), queued once a document is filed for review; it runs beside
    # the review, never in the job's way (advisory only, ADR-0003)
    agent: Callable[[str], Any] = lambda document_id: None

    @classmethod
    def real(cls) -> Stages:
        from sqlalchemy import text

        from cdi_adapter import repo
        from cdi_adapter.classify.service import classify_document
        from cdi_adapter.db import session_scope
        from cdi_adapter.extract.service import extract_document, extract_documents
        from cdi_adapter.ingest.service import ingest_bytes
        from cdi_adapter.mpi.service import merge_identity_evidence
        from cdi_adapter.ocr.service import ocr_document
        from cdi_adapter.terminology.service import bind_document
        from cdi_adapter.validate.service import validate_document

        def counts(doc_id: str) -> dict[str, int]:
            with session_scope() as c:
                rows = c.execute(
                    text("SELECT review_state, count(*) FROM clinical_fact "
                         "WHERE :d = ANY(source_doc_ids) AND is_current GROUP BY review_state"),
                    {"d": doc_id},
                ).all()  # fmt: skip
            by = {r[0]: r[1] for r in rows}
            return {"facts": sum(by.values()),
                    "accepted": by.get("auto_accepted", 0) + by.get("clinician_confirmed", 0)
                    + by.get("corrected", 0),
                    "in_review": by.get("in_review", 0) + by.get("pending", 0)}  # fmt: skip

        def merge(pid):
            with session_scope() as c:
                return merge_identity_evidence(c, pid)

        def enqueue(document_id: str, job_id: str):
            from cdi_adapter.canon.repo import create_rv_item_for_document

            with session_scope() as c:
                return create_rv_item_for_document(c, document_id, job_id=job_id)

        def shape(doc_id: str) -> dict[str, int]:
            with session_scope() as c:
                return repo.document_shape(c, doc_id)

        def agent(document_id: str) -> None:
            from cdi_adapter.worker import medication_agent

            medication_agent.delay(document_id)

        return cls(ingest_bytes, ocr_document, classify_document, extract_document,
                   bind_document, validate_document, counts, merge, enqueue,
                   extract_batch=extract_documents, shape=shape, agent=agent)  # fmt: skip


@dataclass
class DocState:
    filename: str
    status: str = "pending"  # pending | running | done | error | mismatch | skipped
    step: str | None = None
    steps_done: list[str] = field(default_factory=list)
    document_id: str | None = None
    doc_type: str | None = None
    facts: int = 0
    accepted: int = 0
    in_review: int = 0
    error: str | None = None


@dataclass
class Job:
    id: str
    docs: list[DocState]
    state: str = "running"  # running | review | mismatch | error
    patient_id: str | None = None
    registry_patient: dict[str, Any] | None = None
    abha: str | None = None
    mismatch: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # documents past Phase 1 that have not started extraction yet (cdi_extract_waiting, E10-S1)
    extract_queued: int = field(default=0, repr=False)
    # upload time: the start of the 5-minute SLA window (ADR-0002, E10-S2 alert)
    started_at: float = field(default_factory=time.time, repr=False)
    # the job's trace (E10-S3): every stage of every document, the model calls and the FHIR
    # projection are spans of this one trace
    span: Any = field(default=None, repr=False)
    trace_id: str | None = None
    # the upload's span (E10-S9): a stored job's run, in another process, continues its trace
    span_id: str | None = field(default=None, repr=False)
    attempts: int = field(default=0, repr=False)  # runs started so far (stored jobs)
    # set when the job's run has fully ended: a mismatch changes `state` while other documents
    # may still be extracting, so `state` alone does not mean "finished"
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    # a save is a snapshot of the whole job: one at a time, so a later snapshot is never
    # overwritten by an earlier one (E10-S9)
    save_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def trace_parent(self) -> Any:
        """The span a later request continues (the FHIR projection, E10-S3): the run's own span
        in this process, else the stored upload span."""
        if self.span is not None:
            return self.span.get_span_context()
        return tracing.context_from_ids(self.trace_id, self.span_id)

    def as_json(self) -> dict[str, Any]:
        return {"job_id": self.id, "state": self.state, "patient_id": self.patient_id,
                "mismatch": self.mismatch, "trace_id": self.trace_id,
                "documents": [d.__dict__ for d in self.docs]}  # fmt: skip


class JobStore(Protocol):
    """Where stored jobs live (E10-S9); ``webapp.jobstore.PgJobStore`` in production."""

    def claim(self, job_id: str, owner: str, lease_s: float, *,
              attempt: bool = True) -> Job | None:  # fmt: skip
        """Take the job if it is unfinished and nobody holds a live lease; attempts + 1.

        ``attempt=False`` (E9-S2) leaves the count alone: the GPU pool taking a job over at the
        phase boundary is the one run continuing, not a retry of a run that failed."""

    def load(self, job_id: str) -> Job | None:
        """The job as stored, claiming nothing (E9-S2 uses it to see what work is left)."""

    def save(self, job: Job, owner: str) -> bool:
        """Write the job; False when ``owner`` no longer holds its lease."""

    def finish(self, job: Job, owner: str) -> None:
        """Write the final state, release the lease and drop the staged uploads."""

    def files(self, job: Job, indices: list[int]) -> dict[int, tuple[str, bytes, str | None]]:
        """The staged uploads of those documents; a missing one is left out."""

    def release(self, job_id: str, owner: str) -> None:
        """Give the lease up at once (the run could not start), so a sweep retries the job."""

    def heartbeat(self, owner: str, lease_s: float) -> int:
        """Renew every lease ``owner`` holds on an unfinished job."""

    def orphans(self, unclaimed_s: float, limit: int) -> list[str]:
        """Unfinished jobs whose lease ran out, or never claimed ``unclaimed_s`` after upload."""

    def dead_letter(self, job: Job, *, reason: str, actor: str = "runner") -> None:
        """Take a job that kept failing out of the queue (E9-S3). Optional: a store without it
        (the in-memory ones of the unit tests) simply gives the job up as before."""


class JobRunner:
    def __init__(self, stages: Stages | None = None, max_workers: int | None = None,
                 extract_concurrency: int | None = None, batch_max: int | None = None,
                 batch_max_blocks: int | None = None, *, store: JobStore | None = None,
                 owner: str | None = None, role: str | None = None,
                 dispatch_extract: Callable[[str], None] | None = None) -> None:  # fmt: skip
        self.stages = stages or Stages.real()
        # E10-S9: stored jobs, claimed under this owner's lease
        self.store = store
        self.owner = owner or f"runner-{uuid.uuid4().hex[:12]}"
        # E9-S2: which side of the pool split this runner serves. "cpu" stops at the phase
        # boundary and puts the job on the GPU queue; "gpu" and "all" run Phase 2 themselves.
        self.role = role or get_settings().worker_role or "all"
        assert self.role in ROLES, f"unknown runner role {self.role!r}"
        self._dispatch_extract = dispatch_extract
        self._starting: set[str] = set()  # stored jobs handed to the pool, not claimed yet
        self._bg: threading.Thread | None = None
        self._stop = threading.Event()
        # E2-S8: up to batch_max short documents of one doc_type share one model call (1 = off)
        self.batch_max = batch_max or get_settings().extract_batch_max
        self.batch_max_blocks = batch_max_blocks or get_settings().extract_batch_max_blocks
        n = max(2, max_workers or get_settings().job_max_workers)
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="job-p1")
        # E2-S2: with one slot this is exactly the old behaviour (one job at a time, serial
        # Phase 2); with k slots, k jobs run and k extractions share the GPU at once
        settings = get_settings()
        if extract_concurrency is not None:
            if extract_concurrency < 1:
                raise ValueError("extract_concurrency must be positive")
            settings = settings.model_copy(update={"extract_concurrency": extract_concurrency})
        self.extract_slots = extract_slots(settings)
        k = self.extract_slots
        self._job_pool = ThreadPoolExecutor(max_workers=k, thread_name_prefix="job")
        self._p2_pool = ThreadPoolExecutor(max_workers=k, thread_name_prefix="job-p2")
        self._slots = threading.BoundedSemaphore(k)
        self._waited: dict[int, float] = {}  # slot wait per thread (E10-S1 timing)
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def create(self, files: list[tuple[str, bytes, str | None]], *,
               registry_patient: dict[str, Any] | None = None, patient_id: str | None = None,
               abha: str | None = None) -> Job:  # fmt: skip
        job = Job(id=uuid.uuid4().hex, docs=[DocState(filename=f[0]) for f in files],
                  patient_id=patient_id, registry_patient=registry_patient, abha=abha)  # fmt: skip
        job.span = tracing.start_job(job.id)
        job.trace_id = tracing.ids(job.span).get("trace_id")
        self._jobs[job.id] = job
        self._job_pool.submit(self._run, job, list(files))
        return job

    # ---------------------------------------------------------------- stored jobs (E10-S9)

    def run_stored(self, job_id: str, *, handoff: bool = False) -> str:
        """Claim the stored job and run (or resume) it to its end, in this thread. Returns the
        final state, or ``not_claimed`` when it is finished or another runner holds it.

        ``handoff=True`` says this job arrived at the phase boundary from the CPU pool (E9-S2),
        so the claim is the same run continuing and does not count as another attempt. A runner
        whose role is ``cpu`` returns ``running``: Phase 1 is saved and the job is on the GPU
        queue, and the runner that claims it there finishes it."""
        assert self.store is not None, "run_stored needs a JobStore"
        s = get_settings()
        try:
            # E9-S2: a GPU-pool runner picking the job up at the phase boundary continues the run
            # it was handed; only a run that has to start over again is a new attempt
            job = self.store.claim(job_id, self.owner, s.job_lease_s, attempt=not handoff)
        finally:
            self._starting.discard(job_id)
        if job is None:
            return "not_claimed"
        self._jobs[job.id] = job
        if job.attempts > 1:
            metrics.JOB_RESUMES.inc()
            log.warning("job_resumed", job=job.id, attempt=job.attempts, runner=self.owner)
        if job.attempts > s.job_max_attempts:  # it kept killing its runner: stop retrying it
            reason = f"gave up after {s.job_max_attempts} attempts"
            for d in job.docs:
                if d.status in UNFINISHED:
                    d.status, d.step = "error", None
                    d.error = reason
            job.state = "error"
            log.error("job_gave_up", job=job.id, attempts=job.attempts)
            # E9-S3: it leaves the queue for the dead-letter table instead of being swept for
            # ever, and stays there to be looked at and replayed.
            dead = getattr(self.store, "dead_letter", None)
            if dead is not None:
                dead(job, reason=reason, actor=self.owner)
            self._finish(job)
            job.finished.set()
            return job.state
        # the staged uploads this run needs: only documents that must go through Phase 1 again.
        # One already past it (E9-S2, or a resume) is continued from the database instead.
        todo = [i for i, d in enumerate(job.docs)
                if d.status in UNFINISHED and not phase1_done(d)]  # fmt: skip
        try:
            staged = self.store.files(job, todo)
        except Exception:  # the object store is down: let a later sweep retry the job
            self.store.release(job.id, self.owner)
            self._jobs.pop(job.id, None)
            raise
        files = [staged.get(i) for i in range(len(job.docs))]
        job.span = tracing.start_job(
            job.id, parent=tracing.context_from_ids(job.trace_id, job.span_id)
        )
        self._run(job, files)
        return job.state

    def submit_stored(self, job_id: str) -> None:
        """Run the stored job on this runner's pool (in-process execution; the sweep)."""
        if job_id in self._starting:
            return
        # E9-S2: a job the sweep found with nothing but extraction left belongs to the GPU pool,
        # not to this CPU-pool worker; send it there instead of running it here.
        if self._hands_off() and self._phase2_only(job_id):
            try:
                self._dispatch(job_id)
            except Exception as exc:  # noqa: BLE001 - it stays an orphan; a later sweep retries
                log.error("job_handoff_failed", job=job_id, error=f"{type(exc).__name__}: {exc}")
            return
        self._starting.add(job_id)
        self._job_pool.submit(self._run_stored_logged, job_id)

    # ---------------------------------------------------------------- the pool split (E9-S2)

    def _hands_off(self) -> bool:
        """True when this runner serves the CPU pool only, so Phase 2 goes on the GPU queue.
        Stored jobs in queue mode alone: with no queue (inprocess, unit tests) there is nobody to
        hand a job to, so the runner does both phases itself however its role is set."""
        return (self.role == "cpu" and self.store is not None
                and get_settings().job_execution == "queue")  # fmt: skip

    def _phase2_only(self, job_id: str) -> bool:
        """True when every unfinished document of the stored job is through Phase 1, so all that
        is left of it is extraction and the steps after it."""
        try:
            job = self.store.load(job_id) if self.store is not None else None
        except Exception as exc:  # noqa: BLE001 - unsure: let this runner take the job
            log.warning("job_phase_check_failed", job=job_id, error=str(exc))
            return False
        if job is None:
            return False
        left = [d for d in job.docs if d.status in UNFINISHED]
        return bool(left) and all(phase1_done(d) for d in left)

    def _hand_over(self, job: Job) -> None:
        """Give the lease up and put the job on the GPU queue. The lease goes first, so whichever
        runner takes the message can claim it at once. A dispatch that is lost leaves an orphan
        whose Phase 1 is saved, and the next sweep sends it on (:meth:`submit_stored`)."""
        assert self.store is not None
        try:
            self.store.release(job.id, self.owner)
            self._dispatch(job.id)
            log.info("job_handed_to_gpu_pool", job=job.id, runner=self.owner,
                     documents=sum(1 for d in job.docs if d.status == "running"))  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - the sweep picks the job up either way
            log.error("job_handoff_failed", job=job.id, error=f"{type(exc).__name__}: {exc}")

    def _dispatch(self, job_id: str) -> None:
        """Put the job on the GPU queue (``cdi-gpu``); injected in tests."""
        if self._dispatch_extract is not None:
            self._dispatch_extract(job_id)
            return
        from cdi_adapter.webapp.jobstore import dispatch_extract

        dispatch_extract(job_id)

    def _run_stored_logged(self, job_id: str) -> None:
        try:
            self.run_stored(job_id)
        except Exception as exc:  # a pool thread: nobody reads the future
            log.error("job_run_failed", job=job_id, error=f"{type(exc).__name__}: {exc}")

    def sweep(self) -> list[str]:
        """Claim-and-run every orphaned stored job (its runner died, or its queue message was
        lost). Any runner may do this: the claim decides who gets each job."""
        assert self.store is not None
        s = get_settings()
        ids = [j for j in self.store.orphans(s.job_unclaimed_s, limit=20)
               if j not in self._starting]  # fmt: skip
        for job_id in ids:
            log.warning("job_orphan_claiming", job=job_id, runner=self.owner)
            self.submit_stored(job_id)
        return ids

    def start_background(self) -> None:
        """The lease heartbeat + the orphan sweep, every third of a lease, in a daemon thread."""
        if self.store is None or self._bg is not None:
            return

        def loop() -> None:
            store = self.store
            assert store is not None
            while not self._stop.wait(get_settings().job_lease_s / 3):
                try:
                    store.heartbeat(self.owner, get_settings().job_lease_s)
                    self.sweep()
                except Exception as exc:  # noqa: BLE001 - the database may be failing over
                    log.warning("job_heartbeat_failed", runner=self.owner, error=str(exc))

        self._bg = threading.Thread(target=loop, name="job-lease", daemon=True)
        self._bg.start()

    def stop_background(self) -> None:
        self._stop.set()

    def _save(self, job: Job) -> None:
        """Write the job to the store (no-op without one). Raises :class:`LeaseLost` when another
        runner has taken the job over."""
        if self.store is None:
            return
        with job.save_lock:
            if not self.store.save(job, self.owner):
                raise LeaseLost(job.id)

    def wait(self, job_id: str, timeout: float = 600) -> Job:
        """Block until the job's run has fully ended (every document done, skipped or failed)."""
        job = self._jobs[job_id]
        if not job.finished.wait(timeout):
            raise TimeoutError(job_id)
        return job

    # ---------------------------------------------------------------- pipeline

    def _phase1(self, job: Job, i: int, raw: bytes | None, filename: str,
                mime: str | None) -> None:  # fmt: skip
        d, s = job.docs[i], self.stages
        d.status = "running"
        try:
            if raw is None:  # a stored job whose staged upload is gone
                raise FileNotFoundError("the staged upload is missing")
            d.step = "ingest"
            self._save(job)
            with metrics.stage("ingest"):
                res = s.ingest(raw, filename, mime, patient_id=job.patient_id)
            d.document_id = str(res.document_id)
            d.steps_done.append("ingest")
            d.step = "ocr"
            self._save(job)
            with metrics.stage("ocr", d.document_id):
                s.ocr(d.document_id, force_engine="rapidocr")
            d.steps_done.append("ocr")
            d.step = "classify"
            with metrics.stage("classify", d.document_id):
                cls = s.classify(d.document_id)
            d.doc_type = cls.get("doc_type")
            if cls.get("is_handwritten") and get_settings().handwritten_uses_vlm:
                d.step = "ocr"
                with metrics.stage("ocr", d.document_id):  # the handwriting pass on the VLM
                    s.ocr(d.document_id, force_engine="vlm")
            d.steps_done.append("classify")
        except Exception as exc:  # one bad document never sinks the job
            d.status, d.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("job_doc_failed", job=job.id, filename=filename, error=d.error)
        self._save(job)

    def _groups(self, docs: list[DocState]) -> list[list[DocState]]:
        """E2-S8: short documents (one page, at most ``batch_max_blocks`` OCR blocks) of the same
        doc_type go together, up to ``batch_max`` per group; every other document is alone.
        Same doc_type = same schema and a similar output length, so no sequence in the batch
        waits long on another. Order is kept within each doc_type."""
        s = self.stages
        if self.batch_max <= 1 or s.extract_batch is None or s.shape is None:
            return [[d] for d in docs]
        groups: list[list[DocState]] = []
        open_by_type: dict[str, list[DocState]] = {}
        for d in docs:
            try:
                sh = s.shape(str(d.document_id))
                fits = sh["pages"] == 1 and sh["blocks"] <= self.batch_max_blocks
                short = bool(d.doc_type) and fits
            except Exception as exc:  # unknown shape: extract it alone
                log.warning("job_shape_failed", document_id=d.document_id, error=str(exc))
                short = False
            if not short:
                groups.append([d])
                continue
            group = open_by_type.get(str(d.doc_type))
            if group is None or len(group) >= self.batch_max:
                group = open_by_type[str(d.doc_type)] = []
                groups.append(group)
            group.append(d)
        return groups

    def _phase2_group(self, job: Job, group: list[DocState]) -> None:
        if len(group) == 1:
            self._phase2(job, group[0])
            return
        with job.lock:
            stopped = job.state == "mismatch"
        if stopped:  # _dequeue takes job.lock itself, so it runs outside it
            for d in group:
                d.status, d.step = "skipped", None
            self._dequeue(job, len(group))
            self._save(job)
            return
        for d in group:
            d.step = "extract"
        self._save(job)
        s = self.stages
        extract_batch = s.extract_batch
        assert extract_batch is not None
        outs: list[Any]
        t0 = time.perf_counter()
        docs = [str(d.document_id) for d in group]
        try:  # one batched model call holds one GPU slot
            with tracing.span("stage.extract", {"cdi.stage": "extract",
                                                "cdi.document_ids": docs}):  # fmt: skip
                outs = self._with_slot(job, len(group), lambda: extract_batch(
                    [d.document_id for d in group], patient_id=job.patient_id,
                    expected_patient=job.registry_patient))  # fmt: skip
            if len(outs) != len(group):
                raise RuntimeError("batch extraction returned the wrong number of documents")
        except Exception as exc:
            outs = [exc] * len(group)
        took = time.perf_counter() - t0 - self._waited.get(threading.get_ident(), 0.0)
        for out in outs:  # every document of the batch waited for the whole call
            metrics.STAGE_SECONDS.labels("extract").observe(took)
            if isinstance(out, Exception):
                metrics.STAGE_FAILURES.labels("extract").inc()
        log.info("job_extract_batch", job=job.id, documents=len(group), doc_type=group[0].doc_type)
        for d, out in zip(group, outs, strict=True):
            self._after_extract(job, d, out)

    def _phase2(self, job: Job, d: DocState) -> None:
        with job.lock:
            stopped = job.state == "mismatch"  # another document of this job already stopped it
        if stopped:
            d.status, d.step = "skipped", None
            self._dequeue(job, 1)
            self._save(job)
            return
        d.step = "extract"
        self._save(job)
        out: Any
        try:  # the only GPU-bound step
            out = self._with_slot(job, 1, lambda: self._timed_extract(job, d))
        except Exception as exc:
            out = exc
        self._after_extract(job, d, out)

    def _timed_extract(self, job: Job, d: DocState) -> Any:
        with metrics.stage("extract", d.document_id):
            return self.stages.extract(d.document_id, patient_id=job.patient_id,
                                       expected_patient=job.registry_patient)  # fmt: skip

    def _with_slot(self, job: Job, docs: int, fn: Callable[[], Any]) -> Any:
        """Runs ``fn`` holding one GPU extraction slot. The ``docs`` documents leave
        ``cdi_extract_waiting`` (queued since Phase 1 ended) once the slot is held, and the call
        counts in ``cdi_extract_in_flight`` (E10-S1). The slot wait is remembered per thread so a
        batch's timing can leave it out."""
        t0 = time.perf_counter()
        self._slots.acquire()
        self._dequeue(job, docs)
        self._waited[threading.get_ident()] = time.perf_counter() - t0
        metrics.EXTRACT_IN_FLIGHT.inc()
        try:
            return fn()
        finally:
            metrics.EXTRACT_IN_FLIGHT.dec()
            self._slots.release()

    def _after_extract(self, job: Job, d: DocState, out: Any) -> None:
        """The rest of Phase 2 for one document, given its extraction result or exception."""
        from cdi_adapter.extract.service import IdentityMismatch

        try:
            if isinstance(out, Exception):
                raise out
            with job.lock:
                if out.get("patient_id") and job.patient_id is None:
                    job.patient_id = str(out["patient_id"])
            d.steps_done.append("extract")
            if out.get("skipped"):
                d.status, d.step = "skipped", None
                self._save(job)
                return
        except IdentityMismatch as exc:
            d.status, d.step = "mismatch", None
            with job.lock:
                job.state = "mismatch"
                job.mismatch = {"filename": d.filename,
                               "selected": (job.registry_patient or {}).get("name"),
                               "document_name": exc.found.name}  # fmt: skip
            self._save(job)
            return
        except Exception as exc:
            d.status, d.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("job_doc_failed", job=job.id, filename=d.filename, error=d.error)
            self._save(job)
            return
        self._save(job)  # extracted, and the job's patient fixed: a resumed run starts after this
        self._post_extract(job, d)

    def _post_extract(self, job: Job, d: DocState) -> None:
        """terminology -> validate -> the workbasket. A step already in ``steps_done`` (a resumed
        run, E10-S9) is not repeated; filing for review is idempotent per document."""
        s = self.stages
        try:
            for step, fn in (("terminology", s.terminology), ("validate", s.validate)):
                if step in d.steps_done:
                    continue
                d.step = step
                with metrics.stage(step, d.document_id):
                    fn(d.document_id)
                d.steps_done.append(step)
            self._save(job)
            s.enqueue(str(d.document_id), job.id)
            d.__dict__.update(s.counts(str(d.document_id)))
            d.status, d.step = "done", None
        except Exception as exc:
            d.status, d.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("job_doc_failed", job=job.id, filename=d.filename, error=d.error)
        self._save(job)
        if d.status == "done":
            try:  # advisory: a queueing failure never fails the document
                s.agent(str(d.document_id))
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_not_queued", document_id=str(d.document_id), error=str(exc))

    def _resume(self, job: Job) -> list[int]:
        """The documents to run from the start: all of them for a new job. A resumed stored job
        (E10-S9) keeps its finished documents, takes a document past extraction on from there,
        takes one through Phase 1 but not extraction (a GPU-pool handoff, E9-S2, or a runner that
        died between the phases) on at extraction, and starts every other unfinished document
        again from its staged upload."""
        redo = []
        for i, d in enumerate(job.docs):
            if d.status not in UNFINISHED:
                continue
            if "extract" in d.steps_done:
                d.status = "running"
                self._post_extract(job, d)
            elif phase1_done(d):  # its ingest, OCR and classification are in the database
                d.status, d.step = "running", None
            else:
                job.docs[i] = DocState(filename=d.filename)
                redo.append(i)
        return redo

    def _in_job(self, job: Job, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` in a pool thread inside the job's trace (threads do not inherit it)."""
        with tracing.attached(job.span):
            return fn(*args)

    def _run(self, job: Job, files: list[tuple[str, bytes, str | None] | None]) -> None:
        with tracing.attached(job.span):
            self._run_traced(job, files)

    def _phase1_file(self, job: Job, i: int, f: tuple[str, bytes, str | None] | None) -> None:
        name, raw, mime = f if f is not None else (job.docs[i].filename, None, None)
        self._phase1(job, i, raw, name, mime)

    def _run_traced(self, job: Job, files: list[tuple[str, bytes, str | None] | None]) -> None:
        lost = handed = False
        try:
            redo = self._resume(job)
            futures = [self._pool.submit(self._in_job, job, self._phase1_file, job, i, files[i])
                       for i in redo]  # fmt: skip
            for f in futures:
                f.result()
            ready = [d for d in job.docs if d.status == "running"]
            self._queue(job, len(ready))  # E10-S1 queue depth: waiting for extraction
            if ready and self._hands_off():  # E9-S2: Phase 2 is the GPU pool's half of the job
                for d in ready:  # no stage of theirs is running: they wait for the GPU pool
                    d.step = None
                self._save(job)  # Phase 1 is durable before the lease goes
                handed = True
                return
            # no patient yet: documents go one by one until one of them fixes it, so every
            # document of the job lands on the same patient (§7.2)
            while ready and job.patient_id is None and job.state != "mismatch":
                self._phase2(job, ready.pop(0))
            # then the rest in parallel, bounded by the shared extraction slots (E2-S2); short
            # documents of one type may share a batched model call (E2-S8)
            groups = self._groups(ready)
            for f in [self._p2_pool.submit(self._in_job, job, self._phase2_group, job, g)
                      for g in groups]:  # fmt: skip
                f.result()
            if job.patient_id and job.state != "mismatch":
                self.stages.merge_identity(job.patient_id)
            if job.state == "running":
                job.state = "review"
        except LeaseLost:  # another runner took the job over: it finishes the job, not this one
            lost = True
            log.warning("job_lease_lost", job=job.id, runner=self.owner)
        except Exception as exc:  # pragma: no cover - defensive
            job.state = "error"
            log.error("job_failed", job=job.id, error=str(exc))
        finally:
            self._dequeue(job, job.extract_queued)  # nothing of this job is left waiting
            if job.span is not None:
                job.span.set_attribute("cdi.job_state", job.state)
                job.span.end()
            if handed:  # E9-S2: the GPU pool has the rest of the job, and times the whole of it
                self._hand_over(job)
                self._jobs.pop(job.id, None)
            elif not lost:
                metrics.JOB_SECONDS.observe(time.time() - job.started_at)
                self._finish(job)
            if not handed:  # a handed-over job is not finished: another pool finishes it
                job.finished.set()
        if not handed:
            log.info("job_finished", job=job.id, state=job.state, lease_lost=lost)

    def _finish(self, job: Job) -> None:
        if self.store is None:
            return
        try:
            with job.save_lock:
                self.store.finish(job, self.owner)
        except Exception as exc:  # noqa: BLE001 - the lease runs out; a sweep finishes it again
            log.error("job_finish_failed", job=job.id, error=str(exc))

    @staticmethod
    def _queue(job: Job, n: int) -> None:
        with job.lock:
            job.extract_queued += n
        metrics.EXTRACT_WAITING.inc(n)

    @staticmethod
    def _dequeue(job: Job, n: int) -> None:
        with job.lock:
            n = min(n, job.extract_queued)
            job.extract_queued -= n
        metrics.EXTRACT_WAITING.dec(n)


_runner: JobRunner | None = None


def get_runner(role: str | None = None) -> JobRunner:
    """This process's runner of stored jobs (E10-S9): the ``web_job`` table + staged uploads,
    with its lease heartbeat and orphan sweep running. ``role`` (E9-S2) is the side of the pool
    split it serves - a Celery worker passes the role its own queues imply - and it only applies
    to the runner this call builds."""
    global _runner
    if _runner is None:
        from cdi_adapter.webapp.jobstore import OWNER, PgJobStore

        _runner = JobRunner(store=PgJobStore(), owner=OWNER, role=role)
        _runner.start_background()
    return _runner
