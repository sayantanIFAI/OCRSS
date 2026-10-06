"""Web-app job model (ARCHITECTURE §7.2): parallel, then serial, then human review.

* **Phase 1 (parallel)** — ``ThreadPoolExecutor(max(2, CDI_JOB_MAX_WORKERS))``: every document runs
  ``ingest -> OCR(rapidocr) -> classify -> [OCR(vlm) if handwritten]`` concurrently.
* **Phase 2 (serial)** — ``extract -> terminology -> validate`` one document at a time, because
  the ``hf`` backend does one forward pass at a time. The first document fixes the patient when
  none was chosen; later documents attach to it.
* The job then stops at ``state = review`` (it does **not** auto-project). A registry patient
  chosen on the form arms the mismatch guard: a document for someone else stops the job with
  ``state = mismatch`` and its facts purged.

Job state is in memory (``_jobs``); moving it onto the durable queue is backlog E9-S1.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

STEPS = ("ingest", "ocr", "classify", "extract", "terminology", "validate")


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

    @classmethod
    def real(cls) -> Stages:
        from sqlalchemy import text

        from cdi_adapter.classify.service import classify_document
        from cdi_adapter.db import session_scope
        from cdi_adapter.extract.service import extract_document
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

        return cls(ingest_bytes, ocr_document, classify_document, extract_document,
                   bind_document, validate_document, counts, merge, enqueue)  # fmt: skip


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

    def as_json(self) -> dict[str, Any]:
        return {"job_id": self.id, "state": self.state, "patient_id": self.patient_id,
                "mismatch": self.mismatch,
                "documents": [d.__dict__ for d in self.docs]}  # fmt: skip


class JobRunner:
    def __init__(self, stages: Stages | None = None, max_workers: int | None = None) -> None:
        self.stages = stages or Stages.real()
        n = max(2, max_workers or get_settings().job_max_workers)
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="job-p1")
        self._stage2_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job-p2")
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def create(self, files: list[tuple[str, bytes, str | None]], *,
               registry_patient: dict[str, Any] | None = None, patient_id: str | None = None,
               abha: str | None = None) -> Job:  # fmt: skip
        job = Job(id=uuid.uuid4().hex, docs=[DocState(filename=f[0]) for f in files],
                  patient_id=patient_id, registry_patient=registry_patient, abha=abha)  # fmt: skip
        self._jobs[job.id] = job
        self._stage2_pool.submit(self._run, job, files)
        return job

    def wait(self, job_id: str, timeout: float = 600) -> Job:
        import time

        t0 = time.monotonic()
        while self._jobs[job_id].state == "running":
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(job_id)
            time.sleep(0.05)
        return self._jobs[job_id]

    # ---------------------------------------------------------------- pipeline

    def _phase1(self, job: Job, i: int, raw: bytes, filename: str, mime: str | None) -> None:
        d, s = job.docs[i], self.stages
        d.status = "running"
        try:
            d.step = "ingest"
            res = s.ingest(raw, filename, mime, patient_id=job.patient_id)
            d.document_id = str(res.document_id)
            d.steps_done.append("ingest")
            d.step = "ocr"
            s.ocr(d.document_id, force_engine="rapidocr")
            d.steps_done.append("ocr")
            d.step = "classify"
            cls = s.classify(d.document_id)
            d.doc_type = cls.get("doc_type")
            if cls.get("is_handwritten") and get_settings().handwritten_uses_vlm:
                d.step = "ocr"
                s.ocr(d.document_id, force_engine="vlm")
            d.steps_done.append("classify")
        except Exception as exc:  # one bad document never sinks the job
            d.status, d.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("job_doc_failed", job=job.id, filename=filename, error=d.error)

    def _phase2(self, job: Job, d: DocState) -> None:
        from cdi_adapter.extract.service import IdentityMismatch

        s = self.stages
        try:
            d.step = "extract"
            out = s.extract(d.document_id, patient_id=job.patient_id,
                            expected_patient=job.registry_patient)  # fmt: skip
            if out.get("patient_id") and job.patient_id is None:
                job.patient_id = str(out["patient_id"])
            d.steps_done.append("extract")
            if out.get("skipped"):
                d.status, d.step = "skipped", None
                return
            d.step = "terminology"
            s.terminology(d.document_id)
            d.steps_done.append("terminology")
            d.step = "validate"
            s.validate(d.document_id)
            d.steps_done.append("validate")
            s.enqueue(str(d.document_id), job.id)
            d.__dict__.update(s.counts(str(d.document_id)))
            d.status, d.step = "done", None
        except IdentityMismatch as exc:
            d.status, d.step = "mismatch", None
            job.state = "mismatch"
            job.mismatch = {"filename": d.filename,
                            "selected": (job.registry_patient or {}).get("name"),
                            "document_name": exc.found.name}  # fmt: skip
        except Exception as exc:
            d.status, d.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("job_doc_failed", job=job.id, filename=d.filename, error=d.error)

    def _run(self, job: Job, files: list[tuple[str, bytes, str | None]]) -> None:
        try:
            futures = [self._pool.submit(self._phase1, job, i, raw, name, mime)
                       for i, (name, raw, mime) in enumerate(files)]  # fmt: skip
            for f in futures:
                f.result()
            for d in job.docs:  # serial: one GPU forward pass at a time
                if job.state == "mismatch":
                    if d.status == "running":
                        d.status = "skipped"
                    continue
                if d.status == "running":
                    self._phase2(job, d)
            if job.patient_id and job.state != "mismatch":
                self.stages.merge_identity(job.patient_id)
            if job.state == "running":
                job.state = "review"
        except Exception as exc:  # pragma: no cover - defensive
            job.state = "error"
            log.error("job_failed", job=job.id, error=str(exc))
        log.info("job_finished", job=job.id, state=job.state)


_runner: JobRunner | None = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner()
    return _runner
