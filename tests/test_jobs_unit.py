"""E0-S13 unit: the job orchestrator (§7.2) with pluggable stages — ordering and failure rules.

The stage functions here are recorders so the *orchestration* can be checked on its own; the
real stages are covered by their own blocks and by the end-to-end job test (needs MinIO).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from cdi_adapter.extract.service import IdentityMismatch
from cdi_adapter.mpi.service import Candidate
from cdi_adapter.webapp.jobs import JobRunner, Stages


class Recorder:
    def __init__(self, *, fail_ingest=(), handwritten=(), mismatch=(), skip=()):
        self.calls, self.lock = [], threading.Lock()
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
            time.sleep(0.02)
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


def test_phase1_parallel_phase2_serial_then_review():
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
    assert set(j) == {"job_id", "state", "patient_id", "mismatch", "documents"}
    assert j["documents"][0]["filename"] == "a"
