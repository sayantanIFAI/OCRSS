"""E9-S2 unit: the CPU pool (ingest/OCR/classify) and the GPU pool (extract) are separate.

What is checked here: the queue each task is routed to and the queue list / concurrency of each
pool; and that a stored job crosses the phase boundary exactly once - the CPU-pool runner finishes
Phase 1, saves it, gives its lease up and dispatches to the GPU queue, and the GPU-pool runner
resumes at extraction *without* repeating ingest, OCR or classification. The runner is driven with
recorder stages and the in-memory store of E10-S9, so none of this needs Redis, a GPU or a model.

The same split against real PostgreSQL + MinIO and a real Celery worker belongs to the load test
(E9-S6); the throughput claim in the story's acceptance criteria is measured there, not here.
"""

from __future__ import annotations

from typing import Any

import pytest

from cdi_adapter.config import Settings, get_settings
from cdi_adapter.webapp.jobs import DocState, Job, JobRunner, phase1_done
from tests.test_apptier_unit import MemStore, _files
from tests.test_jobs_unit import Recorder

PHASE1_CALLS = ("ingest", "ocr", "classify")


class Dispatched:
    """Stands in for ``jobstore.dispatch_extract``: the GPU queue, recorded."""

    def __init__(self, fail: bool = False) -> None:
        self.jobs: list[str] = []
        self.fail = fail

    def __call__(self, job_id: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.jobs.append(job_id)


def _runner(rec: Recorder, store: MemStore, role: str, owner: str = "r1",
            gpu: Dispatched | None = None) -> JobRunner:  # fmt: skip
    return JobRunner(rec.stages(), max_workers=4, store=store, owner=owner, role=role,
                     dispatch_extract=gpu or Dispatched())  # fmt: skip


def _new(store: MemStore, *names: str) -> Job:
    job = Job(id=f"{len(store.rows):032x}", docs=[DocState(filename=n) for n in names])
    store.put(job, _files(*names))
    return job


@pytest.fixture
def queue_mode(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``CDI_JOB_EXECUTION=queue``: there is a queue to hand the GPU half of a job to."""
    monkeypatch.setenv("CDI_JOB_EXECUTION", "queue")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ queues and sizing


def test_only_extraction_is_routed_to_the_gpu_queue() -> None:
    from cdi_adapter.worker import CPU_QUEUE, GPU_QUEUE, WEB_QUEUE, celery_app

    routes = celery_app.conf.task_routes
    assert routes["cdi.extract_document"] == {"queue": GPU_QUEUE}
    assert routes["cdi.web_job"] == {"queue": WEB_QUEUE}
    # every other stage of the folder-watch chain stays on the queue it has always been on,
    # so a worker started the way it was before E9-S2 still does that work
    assert celery_app.conf.task_default_queue == CPU_QUEUE == "cdi"
    for stage in ("cdi.ingest_file", "cdi.classify_document", "cdi.ocr_document",
                  "cdi.bind_document"):  # fmt: skip
        assert stage not in routes


def test_the_queue_names_are_the_same_on_both_sides_of_the_handoff() -> None:
    from cdi_adapter import worker
    from cdi_adapter.webapp import jobstore

    assert jobstore.GPU_QUEUE == worker.GPU_QUEUE
    assert jobstore.WEB_QUEUE == worker.WEB_QUEUE
    assert jobstore.WEB_TASK == worker.web_job.name


def test_each_pool_has_its_own_queue_list_and_a_role_follows_the_queues() -> None:
    from cdi_adapter.worker import CPU_QUEUE, GPU_QUEUE, WEB_QUEUE
    from cdi_adapter.worker import queues_for_role as q
    from cdi_adapter.worker import role_for_queues as r

    assert q("cpu") == "cdi,cdi-web" and q("gpu") == "cdi-gpu"
    assert set(q("all").split(",")) == {CPU_QUEUE, WEB_QUEUE, GPU_QUEUE}
    assert r({WEB_QUEUE}) == "cpu" and r({CPU_QUEUE, WEB_QUEUE}) == "cpu"
    assert r({GPU_QUEUE}) == "gpu"  # the GPU pool alone
    assert r({WEB_QUEUE, GPU_QUEUE}) == "all" and r({CPU_QUEUE, GPU_QUEUE}) == "all"
    with pytest.raises(ValueError, match="unknown worker role"):
        q("both")


def test_the_two_pool_sizes_are_configured_independently() -> None:
    from cdi_adapter.worker import concurrency_for_role as c

    auto = Settings(job_max_workers=6, mlserve_backend="hf")  # hf: one forward pass at a time
    assert c("cpu", auto) == 6 and c("gpu", auto) == 1
    # raising the CPU pool buys Phase-1 throughput and leaves the GPU pool exactly as it was
    cpu_up = Settings(worker_cpu_concurrency=12, job_max_workers=6, mlserve_backend="hf")
    assert c("cpu", cpu_up) == 12 and c("gpu", cpu_up) == c("gpu", auto) == 1
    gpu_up = Settings(worker_gpu_concurrency=4, job_max_workers=6, mlserve_backend="hf")
    assert c("gpu", gpu_up) == 4 and c("cpu", gpu_up) == 6
    # a worker serving both is bounded by the GPU, as the single worker was before E9-S2
    assert c("all", Settings(mlserve_backend="vllm", vllm_max_num_seqs=3)) == 3


# ------------------------------------------------------------------ the phase boundary


def test_phase1_done_needs_the_work_to_be_in_the_database() -> None:
    assert not phase1_done(DocState("a.pdf"))
    assert not phase1_done(DocState("a.pdf", steps_done=["ingest", "ocr", "classify"]))  # no id
    assert not phase1_done(DocState("a.pdf", document_id="d1", steps_done=["ingest", "ocr"]))
    assert phase1_done(DocState("a.pdf", document_id="d1",
                                steps_done=["ingest", "ocr", "classify"]))  # fmt: skip


def test_the_cpu_pool_runs_phase1_and_hands_the_job_to_the_gpu_queue(queue_mode) -> None:
    rec, store, gpu = Recorder(), MemStore(), Dispatched()
    job = _new(store, "a", "b")

    assert _runner(rec, store, "cpu", gpu=gpu).run_stored(job.id) == "running"

    assert [c[0] for c in rec.calls].count("extract") == 0  # no GPU work on the CPU pool
    assert sorted({c[0] for c in rec.calls}) == sorted(PHASE1_CALLS)
    row, docs = store.rows[job.id], store.state(job.id)["docs"]
    assert gpu.jobs == [job.id]  # it is on cdi-gpu, with its Phase 1 saved
    assert all(phase1_done(DocState(**d)) for d in docs)
    assert not row["finished"] and job.id not in store.finished
    assert row["lease_until"] is not None and row["owner"] == "r1"  # the lease was given up
    assert store.orphans(0, 10) == [job.id]  # claimable by the GPU pool at once
    assert row["attempts"] == 1  # the handoff is the one run going on, not a retry


def test_the_gpu_pool_resumes_at_extraction_and_repeats_no_phase1_work(queue_mode) -> None:
    rec, store, gpu = Recorder(), MemStore(), Dispatched()
    job = _new(store, "a", "b")
    _runner(rec, store, "cpu", gpu=gpu).run_stored(job.id)
    before = list(rec.calls)
    fetched: list[list[int]] = []
    files = store.files
    store.files = lambda j, idx: (fetched.append(list(idx)), files(j, idx))[1]  # type: ignore

    assert _runner(rec, store, "gpu", owner="gpu1").run_stored(job.id, handoff=True) == "review"

    after = rec.calls[len(before):]  # fmt: skip
    assert fetched == [[]]  # no staged upload is read again: Phase 1 is in the database
    assert not [c for c in after if c[0] in PHASE1_CALLS]  # nothing ingested or OCR'd twice
    assert [c[0] for c in after].count("extract") == 2
    row = store.rows[job.id]
    assert row["finished"] and store.state(job.id)["state"] == "review"
    assert [d["status"] for d in store.state(job.id)["docs"]] == ["done", "done"]
    assert row["attempts"] == 1 and row["owner"] is None


def test_one_worker_serving_both_queues_runs_both_phases_itself(queue_mode) -> None:
    rec, store, gpu = Recorder(), MemStore(), Dispatched()
    job = _new(store, "a", "b")
    assert _runner(rec, store, "all", gpu=gpu).run_stored(job.id) == "review"
    assert gpu.jobs == [] and store.rows[job.id]["finished"]
    assert [c[0] for c in rec.calls].count("extract") == 2


def test_without_a_queue_the_role_changes_nothing() -> None:
    """``CDI_JOB_EXECUTION=inprocess`` (dev, tests): there is nobody to hand a job to, so a
    runner does both phases however its role is set - a stray CDI_WORKER_ROLE cannot strand a job.
    """
    rec, store, gpu = Recorder(), MemStore(), Dispatched()
    job = _new(store, "a")
    assert _runner(rec, store, "cpu", gpu=gpu).run_stored(job.id) == "review"
    assert gpu.jobs == [] and store.rows[job.id]["finished"]


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(AssertionError, match="unknown runner role"):
        JobRunner(Recorder().stages(), role="gpu-ish")


# ------------------------------------------------------------------ when the handoff is lost


def test_a_lost_handoff_leaves_an_orphan_whose_phase1_is_kept(queue_mode) -> None:
    rec, store = Recorder(), MemStore()
    job = _new(store, "a", "b")
    down = Dispatched(fail=True)  # Redis refused the dispatch

    assert _runner(rec, store, "cpu", gpu=down).run_stored(job.id) == "running"

    row = store.rows[job.id]
    assert not row["finished"] and store.orphans(0, 10) == [job.id]
    assert all(phase1_done(DocState(**d)) for d in store.state(job.id)["docs"])
    # the sweep of a CPU-pool runner sends it on rather than extracting it itself
    gpu = Dispatched()
    cpu = _runner(rec, store, "cpu", owner="r2", gpu=gpu)
    cpu.submit_stored(job.id)
    assert gpu.jobs == [job.id] and [c[0] for c in rec.calls].count("extract") == 0
    # and the GPU pool then finishes it, still having repeated nothing
    assert _runner(rec, store, "gpu", owner="gpu1").run_stored(job.id, handoff=True) == "review"
    assert store.rows[job.id]["finished"] and store.rows[job.id]["attempts"] == 1


def test_a_sweep_on_the_cpu_pool_still_runs_a_job_that_needs_phase1(queue_mode) -> None:
    """Only a job with *nothing but* extraction left belongs to the other pool. One still needing
    Phase 1 is this runner's work, and it hands it on at the boundary as usual."""
    rec, store, gpu = Recorder(), MemStore(), Dispatched()
    job = _new(store, "a", "b")
    runner = _runner(rec, store, "cpu", gpu=gpu)
    runner.submit_stored(job.id)
    runner._job_pool.shutdown(wait=True)
    assert gpu.jobs == [job.id]  # it ran Phase 1 first
    assert sorted({c[0] for c in rec.calls}) == sorted(PHASE1_CALLS)
