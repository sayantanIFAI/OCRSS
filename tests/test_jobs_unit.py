"""E0-S13 unit: the job orchestrator (§7.2) with pluggable stages — ordering and failure rules.

The stage functions here are recorders so the *orchestration* can be checked on its own; the
real stages are covered by their own blocks and by the end-to-end job test (needs MinIO).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from cdi_adapter.config import Settings
from cdi_adapter.extract.service import IdentityMismatch
from cdi_adapter.mpi.service import Candidate
from cdi_adapter.webapp.jobs import JobRunner, Stages, extract_slots


class Recorder:
    def __init__(self, *, fail_ingest=(), handwritten=(), mismatch=(), skip=(), delay=0.02):
        self.calls, self.lock, self.delay = [], threading.Lock(), delay
        self.active_p2 = 0
        self.max_p2 = 0
        self.fail_ingest, self.handwritten = set(fail_ingest), set(handwritten)
        self.mismatch, self.skip = set(mismatch), set(skip)

    def log(self, *a):
        with self.lock:
            self.calls.append(a)

    def stages(self) -> Stages:
        def ingest(raw, name, mime, patient_id=None):
            time.sleep(0.02)
            if name in self.fail_ingest:
                raise ValueError("unreadable scan")
            self.log("ingest", name)
            return SimpleNamespace(document_id=f"doc-{name}")

        def ocr(doc, force_engine=None):
            self.log("ocr", doc, force_engine)

        def classify(doc):
            self.log("classify", doc)
            return {"doc_type": "prescription", "is_handwritten": doc[4:] in self.handwritten}

        def extract(doc, patient_id=None, expected_patient=None):
            with self.lock:
                self.active_p2 += 1
                self.max_p2 = max(self.max_p2, self.active_p2)
            time.sleep(self.delay)
            with self.lock:
                self.active_p2 -= 1
            self.log("extract", doc, patient_id)
            if doc[4:] in self.mismatch:
                raise IdentityMismatch(doc, expected_patient or {}, Candidate(name="Someone Else"))
            if doc[4:] in self.skip:
                return {"skipped": True}
            return {"patient_id": patient_id or "patient-from-first-doc", "facts": 3}

        return Stages(
            ingest=ingest, ocr=ocr, classify=classify, extract=extract,
            terminology=lambda d: self.log("terminology", d),
            validate=lambda d: self.log("validate", d),
            counts=lambda d: {"facts": 3, "accepted": 2, "in_review": 1},
            merge_identity=lambda p: self.log("merge", p),
        )  # fmt: skip


def _files(*names):
    return [(n, b"x", None) for n in names]


def test_phase1_parallel_phase2_serial_then_review():  # stub/hf default: one slot
    rec = Recorder()
    runner = JobRunner(rec.stages(), max_workers=4)
    job = runner.wait(runner.create(_files("a", "b", "c")).id)
    assert job.state == "review"
    assert rec.max_p2 == 1  # extract never runs two documents at once
    order = [c[0] for c in rec.calls]
    assert order.index("extract") > max(i for i, c in enumerate(order) if c == "classify")
    # the first document fixes the patient; the rest attach to it
    extracts = [c for c in rec.calls if c[0] == "extract"]
    assert extracts[0][2] is None and all(c[2] == "patient-from-first-doc" for c in extracts[1:])
    assert ("merge", "patient-from-first-doc") in rec.calls
    assert [d.status for d in job.docs] == ["done"] * 3
    assert job.docs[0].steps_done == ["ingest", "ocr", "classify", "extract", "terminology",
                                      "validate"]  # fmt: skip
    assert (job.docs[0].facts, job.docs[0].accepted, job.docs[0].in_review) == (3, 2, 1)


def test_handwritten_page_gets_a_second_vlm_ocr_pass():
    rec = Recorder(handwritten={"b"})
    runner = JobRunner(rec.stages())
    runner.wait(runner.create(_files("a", "b")).id)
    assert ("ocr", "doc-b", "vlm") in rec.calls and ("ocr", "doc-a", "vlm") not in rec.calls


def test_one_bad_document_does_not_sink_the_job():
    rec = Recorder(fail_ingest={"bad"})
    runner = JobRunner(rec.stages())
    job = runner.wait(runner.create(_files("good", "bad")).id)
    assert job.state == "review"
    assert [d.status for d in job.docs] == ["done", "error"]
    assert "unreadable scan" in job.docs[1].error
    assert not any(c[0] == "extract" and c[1] == "doc-bad" for c in rec.calls)


def test_mismatch_stops_the_job():
    rec = Recorder(mismatch={"b"})
    runner = JobRunner(rec.stages())
    job = runner.create(_files("a", "b", "c"), registry_patient={"name": "Ramesh Kumar"},
                        patient_id="registry-patient")  # fmt: skip
    job = runner.wait(job.id)
    assert job.state == "mismatch"
    assert [d.status for d in job.docs] == ["done", "mismatch", "skipped"]
    assert job.mismatch == {"filename": "b", "selected": "Ramesh Kumar",
                            "document_name": "Someone Else"}  # fmt: skip
    assert not any(c[0] == "merge" for c in rec.calls)


def test_unknown_doc_type_is_skipped_not_failed():
    rec = Recorder(skip={"a"})
    runner = JobRunner(rec.stages())
    job = runner.wait(runner.create(_files("a")).id)
    assert job.state == "review" and job.docs[0].status == "skipped"
    assert not any(c[0] == "terminology" for c in rec.calls)


def test_validated_documents_are_enqueued_for_the_reviewer():
    rec = Recorder(skip={"b"})
    stages = rec.stages()
    stages.enqueue = lambda doc, job: rec.log("enqueue", doc, job)
    runner = JobRunner(stages)
    job = runner.wait(runner.create(_files("a", "b")).id)
    assert ("enqueue", "doc-a", job.id) in rec.calls
    assert not any(c[0] == "enqueue" and c[1] == "doc-b" for c in rec.calls)  # skipped type


def test_as_json_shape():
    rec = Recorder()
    runner = JobRunner(rec.stages())
    j = runner.wait(runner.create(_files("a")).id).as_json()
    # trace_id: E10-S3, the job's trace (None until a tracer provider is set up in the process)
    assert set(j) == {"job_id", "state", "patient_id", "mismatch", "documents", "trace_id"}
    assert j["documents"][0]["filename"] == "a"


# ------------------------------------------------ E2-S2: Phase 2 parallel with vLLM, serial with hf


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_extract_slots_follow_the_backend():
    assert extract_slots(_s(mlserve_backend="hf")) == 1
    assert extract_slots(_s(mlserve_backend="hf", extract_concurrency=4)) == 1  # the lock stays
    assert extract_slots(_s(mlserve_backend="vllm")) == 4  # auto = vllm_max_num_seqs
    assert extract_slots(_s(mlserve_backend="vllm", vllm_max_num_seqs=3)) == 3
    assert extract_slots(_s(mlserve_backend="vllm", extract_concurrency=2)) == 2
    assert extract_slots(_s(mlserve_backend="stub")) == 1
    assert extract_slots(_s(mlserve_backend="stub", extract_concurrency=3)) == 3


def test_with_vllm_the_documents_of_a_job_extract_in_parallel():
    rec = Recorder(delay=0.2)
    runner = JobRunner(rec.stages(), max_workers=4, extract_concurrency=4)
    t0 = time.perf_counter()
    job = runner.wait(runner.create(_files("a", "b", "c", "d"), patient_id="p-1").id)
    wall = time.perf_counter() - t0
    assert job.state == "review" and [d.status for d in job.docs] == ["done"] * 4
    assert rec.max_p2 == 4 and wall < 4 * 0.2  # not four extractions one after another
    assert all(c[2] == "p-1" for c in rec.calls if c[0] == "extract")


def test_with_one_slot_phase2_stays_serial():
    rec = Recorder(delay=0.05)
    runner = JobRunner(rec.stages(), max_workers=4, extract_concurrency=1)
    runner.wait(runner.create(_files("a", "b", "c", "d"), patient_id="p-1").id)
    assert rec.max_p2 == 1


def test_first_document_alone_fixes_the_patient_then_the_rest_run_in_parallel():
    rec = Recorder(delay=0.1)
    runner = JobRunner(rec.stages(), max_workers=4, extract_concurrency=4)
    job = runner.wait(runner.create(_files("a", "b", "c", "d")).id)
    extracts = [c for c in rec.calls if c[0] == "extract"]
    assert extracts[0][2] is None  # only the first one resolves the patient ...
    assert all(c[2] == "patient-from-first-doc" for c in extracts[1:])  # ... the rest attach
    assert rec.max_p2 == 3  # b, c, d together, after a finished
    assert job.patient_id == "patient-from-first-doc"
    assert ("merge", "patient-from-first-doc") in rec.calls


def test_a_skipped_first_document_hands_patient_resolution_to_the_next_one():
    rec = Recorder(skip={"a"}, delay=0.05)
    runner = JobRunner(rec.stages(), max_workers=4, extract_concurrency=4)
    job = runner.wait(runner.create(_files("a", "b", "c", "d")).id)
    extracts = {c[1]: c[2] for c in rec.calls if c[0] == "extract"}
    assert extracts["doc-a"] is None and extracts["doc-b"] is None
    assert extracts["doc-c"] == extracts["doc-d"] == "patient-from-first-doc"
    assert [d.status for d in job.docs] == ["skipped", "done", "done", "done"]


def test_the_slots_are_shared_by_every_job():
    rec = Recorder(delay=0.1)
    runner = JobRunner(rec.stages(), max_workers=6, extract_concurrency=2)
    jobs = [runner.create(_files(f"{j}1", f"{j}2", f"{j}3"), patient_id=f"p-{j}") for j in "xy"]
    done = [runner.wait(j.id) for j in jobs]
    assert all(j.state == "review" for j in done)
    assert rec.max_p2 == 2  # two jobs at once, but never more extractions than slots


def test_a_mismatch_in_parallel_skips_the_documents_not_yet_started():
    rec = Recorder(mismatch={"a"}, delay=0.3)
    stages = rec.stages()
    slow_extract = stages.extract

    def extract(doc, patient_id=None, expected_patient=None):
        if doc == "doc-a":  # the mismatch is found at once, while b is still extracting
            rec.log("extract", doc, patient_id)
            raise IdentityMismatch(doc, expected_patient or {}, Candidate(name="Someone Else"))
        return slow_extract(doc, patient_id=patient_id, expected_patient=expected_patient)

    stages.extract = extract
    runner = JobRunner(stages, max_workers=6, extract_concurrency=2)
    job = runner.create(_files("a", "b", "c", "d", "e"), registry_patient={"name": "Ramesh Kumar"},
                        patient_id="registry-patient")  # fmt: skip
    job = runner.wait(job.id)
    assert job.state == "mismatch" and job.docs[0].status == "mismatch"
    # once the mismatch is known no extraction starts; only b can have been running beside a
    # (it then finishes: each document checks the chosen patient itself)
    assert job.docs[1].status in ("done", "skipped")
    assert [d.status for d in job.docs[2:]] == ["skipped"] * 3
    assert not any(c[0] == "extract" and c[1] in ("doc-c", "doc-d", "doc-e") for c in rec.calls)
    assert not any(c[0] == "merge" for c in rec.calls)


def test_the_medication_agent_is_queued_for_each_filed_document_and_never_blocks():
    """Backlog E12-S4: once a document is filed for review its agent is queued (advisory, beside
    the review); a queueing failure never fails the document or the job."""
    rec = Recorder(skip={"b"})
    stages = rec.stages()
    queued: list[str] = []

    def agent(doc):
        queued.append(doc)
        if doc == "doc-c":
            raise ConnectionError("broker down")

    stages.agent = agent
    runner = JobRunner(stages)
    job = runner.wait(runner.create(_files("a", "b", "c")).id)
    assert sorted(queued) == ["doc-a", "doc-c"]  # b was skipped: nothing to check
    assert job.state == "review" and [d.status for d in job.docs] == ["done", "skipped", "done"]
