"""E10-S9 unit: durable jobs (lease, resume, take-over), gateway replica failover, /readyz, the
balancer config and the start script. The same runner against real PostgreSQL + MinIO, and the
kill-instance drill, are in test_apptier_integration.py and scripts/app_ha_drill.py."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from cdi_adapter import metrics
from cdi_adapter.ml import client as mc
from cdi_adapter.webapp.jobs import DocState, Job, JobRunner
from tests.test_jobs_unit import Recorder
from tests.test_ops_unit import _bash

ROOT = Path(__file__).resolve().parents[1]
RUNPOD = ROOT / "infra" / "runpod"


class MemStore:
    """The ``JobStore`` contract in memory, with the lease rules of ``PgJobStore``."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.staged: dict[str, dict[int, tuple[str, bytes, str | None]]] = {}
        self.saves = 0
        self.finished: list[str] = []
        self.lock = threading.Lock()
        self.fail_files = False

    def put(self, job: Job, files: list[tuple[str, bytes, str | None]], *,
            owner: str | None = None, lease_until: float | None = None) -> None:  # fmt: skip
        self.rows[job.id] = {"job": self._snap(job), "owner": owner, "lease_until": lease_until,
                             "attempts": job.attempts, "finished": False}  # fmt: skip
        self.staged[job.id] = dict(enumerate(files))

    @staticmethod
    def _snap(job: Job) -> dict[str, Any]:
        return {"id": job.id, "state": job.state, "patient_id": job.patient_id,
                "registry_patient": job.registry_patient, "mismatch": job.mismatch,
                "docs": [dataclasses.asdict(d) for d in job.docs]}  # fmt: skip

    def job(self, job_id: str) -> Job:
        r = self.rows[job_id]
        s = copy.deepcopy(r["job"])
        return Job(id=s["id"], docs=[DocState(**d) for d in s["docs"]], state=s["state"],
                   patient_id=s["patient_id"], registry_patient=s["registry_patient"],
                   mismatch=s["mismatch"], attempts=r["attempts"])  # fmt: skip

    def claim(self, job_id: str, owner: str, lease_s: float, *,
              attempt: bool = True) -> Job | None:  # fmt: skip
        with self.lock:
            r = self.rows.get(job_id)
            if r is None or r["finished"]:
                return None
            if r["lease_until"] is not None and r["lease_until"] >= time.monotonic():
                return None
            r.update(owner=owner, lease_until=time.monotonic() + lease_s,
                     attempts=r["attempts"] + (1 if attempt else 0))  # fmt: skip
            return self.job(job_id)

    def load(self, job_id: str) -> Job | None:
        """As stored, claiming nothing (E9-S2: what work is left)."""
        return self.job(job_id) if job_id in self.rows else None

    def save(self, job: Job, owner: str) -> bool:
        with self.lock:
            r = self.rows[job.id]
            if r["owner"] != owner or r["finished"]:
                return False
            r["job"] = self._snap(job)
            self.saves += 1
            return True

    def finish(self, job: Job, owner: str) -> None:
        with self.lock:
            r = self.rows[job.id]
            if r["owner"] != owner or r["finished"]:
                return
            r["job"] = self._snap(job)
            r.update(finished=True, owner=None, lease_until=None)
            self.staged.pop(job.id, None)
            self.finished.append(job.id)

    def files(self, job: Job, indices: list[int]) -> dict[int, tuple[str, bytes, str | None]]:
        if self.fail_files:
            raise ConnectionError("object store down")
        return {i: f for i, f in self.staged.get(job.id, {}).items() if i in indices}

    def release(self, job_id: str, owner: str) -> None:
        with self.lock:
            if self.rows[job_id]["owner"] == owner:
                self.rows[job_id]["lease_until"] = time.monotonic() - 1

    def heartbeat(self, owner: str, lease_s: float) -> int:
        with self.lock:
            mine = [r for r in self.rows.values() if r["owner"] == owner and not r["finished"]]
            for r in mine:
                r["lease_until"] = time.monotonic() + lease_s
            return len(mine)

    def orphans(self, unclaimed_s: float, limit: int) -> list[str]:
        now = time.monotonic()
        return [k for k, r in self.rows.items() if not r["finished"]
                and r["lease_until"] is not None and r["lease_until"] < now][:limit]  # fmt: skip

    def state(self, job_id: str) -> dict[str, Any]:
        return self.rows[job_id]["job"]


def _files(*names: str) -> list[tuple[str, bytes, str | None]]:
    return [(n, b"x", None) for n in names]


def _new(store: MemStore, *names: str, **kw: Any) -> Job:
    job = Job(id=f"{len(store.rows):032x}", docs=[DocState(filename=n) for n in names])
    store.put(job, _files(*names), **kw)
    return job


def _stored_runner(rec: Recorder, store: MemStore, owner: str = "r1") -> JobRunner:
    return JobRunner(rec.stages(), max_workers=4, store=store, owner=owner)


# ------------------------------------------------------------------ durable jobs


def test_a_stored_job_runs_saves_each_step_and_finishes() -> None:
    rec, store = Recorder(), MemStore()
    job = _new(store, "a", "b")
    assert _stored_runner(rec, store).run_stored(job.id) == "review"
    row = store.rows[job.id]
    assert row["finished"] and row["owner"] is None and row["attempts"] == 1
    assert [d["status"] for d in store.state(job.id)["docs"]] == ["done", "done"]
    assert store.saves >= 8  # every step boundary of both documents was written
    assert job.id not in store.staged  # the staged uploads go once the job has ended


def test_a_live_lease_or_a_finished_job_is_not_claimed() -> None:
    rec, store = Recorder(), MemStore()
    held = _new(store, "a", owner="other", lease_until=time.monotonic() + 60)
    assert _stored_runner(rec, store).run_stored(held.id) == "not_claimed"
    assert rec.calls == []
    done = _new(store, "b")
    _stored_runner(rec, store).run_stored(done.id)
    assert _stored_runner(rec, store, "r2").run_stored(done.id) == "not_claimed"


def test_resume_keeps_finished_documents_and_continues_past_extraction() -> None:
    """The runner that died left: a done document, one extracted but not validated, one in OCR
    and one never started. The take-over repeats only what is missing."""
    rec, store = Recorder(), MemStore()
    job = Job(id="f" * 32, patient_id="p-1", docs=[
        DocState("done.pdf", status="done", document_id="doc-done.pdf",
                 steps_done=["ingest", "ocr", "classify", "extract", "terminology", "validate"]),
        DocState("ext.pdf", status="running", step="terminology", document_id="doc-ext.pdf",
                 steps_done=["ingest", "ocr", "classify", "extract"]),
        DocState("ocr.pdf", status="running", step="ocr", document_id="doc-ocr.pdf",
                 steps_done=["ingest"]),
        DocState("new.pdf"),
    ])  # fmt: skip
    store.put(job, _files("done.pdf", "ext.pdf", "ocr.pdf", "new.pdf"), owner="dead",
              lease_until=time.monotonic() - 1)  # fmt: skip
    store.rows[job.id]["attempts"] = 1
    before = metrics.JOB_RESUMES._value.get()
    runner = _stored_runner(rec, store, "r2")
    assert runner.sweep() == [job.id]
    for _ in range(100):
        if store.rows[job.id]["finished"]:
            break
        time.sleep(0.05)
    row = store.rows[job.id]
    assert row["finished"] and row["attempts"] == 2
    assert metrics.JOB_RESUMES._value.get() == before + 1
    docs = {d["filename"]: d for d in store.state(job.id)["docs"]}
    assert all(d["status"] == "done" for d in docs.values()), docs
    touched = {c[1] for c in rec.calls if c[0] != "merge"}
    assert "doc-done.pdf" not in touched  # finished: left alone
    assert ("extract", "doc-ext.pdf", "p-1") not in rec.calls  # not extracted twice
    assert ("terminology", "doc-ext.pdf") in rec.calls and ("validate", "doc-ext.pdf") in rec.calls
    for name in ("ocr.pdf", "new.pdf"):  # started again from the staged upload
        assert ("ingest", name) in rec.calls and ("extract", f"doc-{name}", "p-1") in rec.calls


def test_a_runner_that_lost_its_lease_stops_and_writes_nothing_more() -> None:
    rec, store = Recorder(delay=0.3), MemStore()
    job = _new(store, "a", "b")
    runner = _stored_runner(rec, store, "slow")
    t = threading.Thread(target=runner.run_stored, args=(job.id,))
    t.start()
    for _ in range(100):  # wait until it is extracting, then another runner takes the job
        if any(c[0] == "extract" for c in rec.calls) or any(
                d["step"] == "extract" for d in store.state(job.id)["docs"]):  # fmt: skip
            break
        time.sleep(0.02)
    with store.lock:
        store.rows[job.id].update(owner="new-owner", lease_until=time.monotonic() + 60)
        taken_over = copy.deepcopy(store.rows[job.id]["job"])
    t.join(10)
    assert not t.is_alive()
    row = store.rows[job.id]
    assert not row["finished"] and row["owner"] == "new-owner"
    assert row["job"] == taken_over  # the old runner wrote nothing after losing it
    assert not any(d["status"] == "error" for d in row["job"]["docs"])  # not a document error
    assert job.id not in store.finished


def test_a_job_that_keeps_killing_its_runner_is_given_up(monkeypatch) -> None:
    from cdi_adapter.config import get_settings

    rec, store = Recorder(), MemStore()
    job = _new(store, "a", owner="dead", lease_until=time.monotonic() - 1)
    store.rows[job.id]["attempts"] = get_settings().job_max_attempts
    assert _stored_runner(rec, store).run_stored(job.id) == "error"
    d = store.state(job.id)["docs"][0]
    assert d["status"] == "error" and d["error"].startswith("gave up after")
    assert rec.calls == [] and store.rows[job.id]["finished"]


def test_a_missing_staged_upload_fails_that_document_only() -> None:
    rec, store = Recorder(), MemStore()
    job = _new(store, "a", "b")
    del store.staged[job.id][0]
    assert _stored_runner(rec, store).run_stored(job.id) == "review"
    a, b = store.state(job.id)["docs"]
    assert a["status"] == "error" and "staged upload is missing" in a["error"]
    assert b["status"] == "done"


def test_object_store_down_releases_the_lease_for_a_later_retry() -> None:
    rec, store = Recorder(), MemStore()
    job = _new(store, "a")
    store.fail_files = True
    with pytest.raises(ConnectionError):
        _stored_runner(rec, store).run_stored(job.id)
    assert store.orphans(0, 10) == [job.id]  # swept again at once, not after a whole lease
    store.fail_files = False
    assert _stored_runner(rec, store, "r2").run_stored(job.id) == "review"


def test_the_sweep_hands_each_orphan_to_the_pool_once() -> None:
    rec, store = Recorder(delay=0.2), MemStore()
    job = _new(store, "a", owner="dead", lease_until=time.monotonic() - 1)
    runner = _stored_runner(rec, store, "r2")
    runner._job_pool.submit(time.sleep, 0.3)  # the pool is busy: the orphan waits its turn
    assert runner.sweep() == [job.id]
    assert runner.sweep() == []  # already handed over, not queued twice
    for _ in range(100):
        if store.rows[job.id]["finished"]:
            break
        time.sleep(0.05)
    assert store.rows[job.id]["finished"] and store.rows[job.id]["attempts"] == 1


def test_the_heartbeat_keeps_a_long_job_owned(monkeypatch) -> None:
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_JOB_LEASE_S", "3")
    get_settings.cache_clear()
    try:
        rec, store = Recorder(delay=4.5), MemStore()  # extraction outlives one lease
        job = _new(store, "a")
        runner = _stored_runner(rec, store)
        runner.start_background()
        try:
            t = threading.Thread(target=runner.run_stored, args=(job.id,))
            t.start()
            time.sleep(3.8)  # past the first lease: renewed, so nobody may take the job over
            assert store.orphans(0, 10) == []
            assert _stored_runner(Recorder(), store, "r2").run_stored(job.id) == "not_claimed"
            t.join(10)
        finally:
            runner.stop_background()
        assert store.rows[job.id]["finished"] and store.rows[job.id]["attempts"] == 1
    finally:
        get_settings.cache_clear()


def test_without_a_store_jobs_stay_in_memory_as_before() -> None:
    rec = Recorder()
    runner = JobRunner(rec.stages(), max_workers=2)
    job = runner.wait(runner.create(_files("a")).id)
    assert job.state == "review" and runner.store is None


# ------------------------------------------------------------------ accept: durable before 202


class _Objects:
    def __init__(self, fail_on: int | None = None) -> None:
        self.put, self.deleted, self.fail_on = [], [], fail_on

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail_on is not None and len(self.put) == self.fail_on:
            raise ConnectionError("MinIO down")
        self.put.append(key)

    def delete(self, key: str) -> None:
        self.deleted.append(key)


def test_an_upload_is_refused_unless_files_and_row_are_both_stored(monkeypatch) -> None:
    from cdi_adapter.webapp import jobstore

    objs, inserted, dispatched = _Objects(fail_on=1), [], []
    monkeypatch.setattr(jobstore.PgJobStore, "objects", property(lambda self: objs))
    monkeypatch.setattr(jobstore.PgJobStore, "insert", lambda self, *a, **k: inserted.append(a))
    # E9-S3: no job carries this upload's idempotency key yet, so it is a first submission
    monkeypatch.setattr(jobstore.PgJobStore, "by_key", lambda self, key: None)
    monkeypatch.setattr(jobstore, "dispatch", dispatched.append)
    with pytest.raises(jobstore.NotAccepted):
        jobstore.submit(_files("a", "b"), actor="counter1")
    assert inserted == [] and dispatched == []
    assert objs.deleted == objs.put  # nothing half-accepted is left behind


def test_an_upload_is_staged_then_recorded_then_dispatched(monkeypatch) -> None:
    from cdi_adapter import tracing
    from cdi_adapter.webapp import jobstore

    tracing.setup(tracing.WEBAPP)  # no exporter: spans exist, so the job has a trace

    order: list[str] = []
    objs = _Objects()
    monkeypatch.setattr(jobstore.PgJobStore, "objects", property(lambda self: objs))

    def by_key(self, key):  # noqa: ANN001, ANN202 - E9-S3: looked up before anything is staged
        order.append("lookup")
        assert key.startswith("body:")  # derived from the upload: no client header here
        return None

    def insert(self, job, files, *, actor, accepted_by, **kw):  # noqa: ANN001, ANN202
        order.append(f"insert:{len(objs.put)} staged")
        assert [f["key"] for f in files] == objs.put and actor == "counter1"
        assert all(len(f["sha256"]) == 64 for f in files)
        # E9-S3: the row carries the upload's idempotency key and its lane
        assert kw["key"].startswith("body:") and kw["priority"] == jobstore.NORMAL_PRIORITY

    monkeypatch.setattr(jobstore.PgJobStore, "by_key", by_key)
    monkeypatch.setattr(jobstore.PgJobStore, "insert", insert)
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id: order.append("dispatch"))
    job = jobstore.submit(_files("a", "b"), actor="counter1")
    assert order == ["lookup", "insert:2 staged", "dispatch"]
    assert objs.put == [f"uploads/{job.id}/00", f"uploads/{job.id}/01"]
    assert job.trace_id and job.span_id


def test_a_failed_dispatch_still_acknowledges_the_durable_upload(monkeypatch) -> None:
    from cdi_adapter.config import get_settings
    from cdi_adapter.webapp import jobstore

    monkeypatch.setenv("CDI_JOB_EXECUTION", "queue")
    get_settings.cache_clear()
    try:
        from cdi_adapter import worker

        def down(*a: Any, **k: Any) -> None:
            raise ConnectionError("redis down")

        monkeypatch.setattr(worker.celery_app, "send_task", down)
        jobstore.dispatch("a" * 32)  # logged; a worker's sweep claims the job later
    finally:
        get_settings.cache_clear()


def test_unknown_job_ids_never_reach_the_database() -> None:
    from cdi_adapter.webapp.jobstore import PgJobStore

    assert PgJobStore().load("nope") is None
    assert PgJobStore().load("../" + "a" * 29) is None


def test_the_web_job_task_is_routed_to_its_own_queue() -> None:
    from cdi_adapter.worker import celery_app, web_job

    assert web_job.name == "cdi.web_job" and web_job.acks_late
    assert celery_app.conf.task_routes["cdi.web_job"] == {"queue": "cdi-web"}


# ------------------------------------------------------------------ /readyz


def test_readyz_is_public_and_says_what_is_not_ready(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cdi_adapter import db, storage
    from cdi_adapter.config import get_settings
    from cdi_adapter.webapp import app as webapp
    from cdi_adapter.webapp.auth import POLICY, PUBLIC

    assert POLICY[("GET", "/readyz")] == PUBLIC
    monkeypatch.setenv("CDI_DATABASE_URL", "postgresql+psycopg://127.0.0.1:9/none")
    monkeypatch.setenv("CDI_S3_ENDPOINT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CDI_JOB_EXECUTION", "queue")
    monkeypatch.setenv("CDI_REDIS_URL", "redis://127.0.0.1:9/0")
    monkeypatch.setenv("CDI_INSTANCE_NAME", "web2")
    get_settings.cache_clear()
    storage.get_store.cache_clear()
    db.reset_engine()
    try:
        r = TestClient(webapp.app).get("/readyz")
    finally:
        get_settings.cache_clear()
        storage.get_store.cache_clear()
        db.reset_engine()
    assert r.status_code == 503
    assert r.json() == {"ready": False, "instance": "web2", "db": False, "s3": False,
                        "queue": False}  # fmt: skip


# ------------------------------------------------------------------ gateway replicas


@pytest.fixture(autouse=True)
def _replicas():
    mc.reset_replicas()
    yield
    mc.reset_replicas()


def _gw(behaviour: dict[str, Any], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        b = behaviour[host]
        if isinstance(b, Exception):
            raise b
        if b != 200:
            return httpx.Response(b, json={"detail": "x"})
        return httpx.Response(200, json={"text": host, "model": "m", "status": "ok"})

    return httpx.MockTransport(handler)


def test_a_refused_replica_is_skipped_and_the_call_answered_by_the_next() -> None:
    seen: list[str] = []
    beh = {"a": httpx.ConnectError("refused"), "b": 200}
    c = mc.HttpMLClient("http://a:1, http://b:2/", transport=_gw(beh, seen))
    assert c.urls == ["http://a:1", "http://b:2"]
    before = metrics.MLSERVE_FAILOVERS._value.get()
    assert [c.generate("p") for _ in range(4)] == ["b"] * 4
    assert seen.count("a") == 1  # down after one refusal: skipped by the next calls
    assert metrics.MLSERVE_FAILOVERS._value.get() == before + 1
    assert c.replica == "http://b:2"


def test_503_moves_on_but_a_4xx_or_a_slow_answer_does_not() -> None:
    seen: list[str] = []
    c = mc.HttpMLClient("http://a:1,http://b:2", transport=_gw({"a": 503, "b": 200}, seen))
    assert {c.generate("p") for _ in range(3)} == {"b"}
    for bad in (422, httpx.ReadTimeout("slow")):  # an answer, or slow: the same elsewhere
        mc.reset_replicas()
        seen.clear()
        c = mc.HttpMLClient("http://a:1,http://b:2", transport=_gw({"a": bad, "b": bad}, seen))
        with pytest.raises((httpx.HTTPStatusError, httpx.ReadTimeout)):
            c.generate("p")
        assert len(seen) == 1


def test_every_replica_down_raises_the_last_error() -> None:
    seen: list[str] = []
    beh = {"a": httpx.ConnectError("refused"), "b": httpx.ConnectError("refused")}
    c = mc.HttpMLClient("http://a:1,http://b:2", transport=_gw(beh, seen))
    with pytest.raises(httpx.ConnectError):
        c.generate("p")
    assert sorted(seen) == ["a", "b"]


def test_one_gateway_behaves_as_before() -> None:
    seen: list[str] = []
    c = mc.HttpMLClient("http://a:1", transport=_gw({"a": 503}, seen))
    with pytest.raises(httpx.HTTPStatusError):
        c.generate("p")
    assert seen == ["a"] and c.urls == ["http://a:1"]


def test_healthz_answers_from_the_first_replica_up() -> None:
    seen: list[str] = []
    beh = {"a": httpx.ConnectError("refused"), "b": 200}
    c = mc.HttpMLClient("http://a:1,http://b:2", transport=_gw(beh, seen))
    assert c.healthz()["status"] == "ok"


def test_a_rollout_side_with_replicas_must_serve_the_version_on_every_one(monkeypatch) -> None:
    from cdi_adapter.modelops import ops

    good = {"status": "ok", "version": "v1", "model": "m", "backend": "hf"}
    answers: dict[str, Any] = {"http://a:1": good, "http://b:2": good}
    monkeypatch.setattr(ops, "_one_health", lambda u: answers[u])
    h = ops.gateway_health("http://a:1,http://b:2")
    assert (
        h is not None
        and h["replicas"] == 2
        and ops.serves(h, {"version": "v1", "model_id": "m", "backend": "hf"})
    )
    answers["http://b:2"] = {**good, "version": "v0"}
    assert ops.gateway_health("http://a:1,http://b:2")["status"] == "mixed"
    answers["http://b:2"] = None
    assert ops.gateway_health("http://a:1,http://b:2") is None


# ------------------------------------------------------------------ balancer + scripts


LB = (RUNPOD / "Caddyfile.lb").read_text(encoding="utf-8")
APPTIER = (RUNPOD / "start_apptier.sh").read_text(encoding="utf-8")


def _directive(name: str) -> str:
    m = re.search(rf"^\s*{name}\s+(.+)$", LB, re.M)
    assert m, name
    return m.group(1).strip()


def test_the_balancer_checks_readiness_by_content_and_drops_a_dead_instance_fast() -> None:
    assert _directive("health_uri") == "/readyz"
    assert _directive("health_body") == '"\\"ready\\":true"'  # a marker, not the status alone
    assert _directive("health_fails") == "1" and _directive("max_fails") == "1"
    from scripts.app_ha_drill import health_window_s

    assert health_window_s(LB) <= 5.0  # a killed instance is out within 5 s at the latest
    assert "lb_try_duration" in LB  # a failed connection goes to another instance
    code = [ln for ln in LB.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code)
    assert "unhealthy_status" not in code  # an app 503 is an answer, not a dead instance
    assert "CDI_BACKEND_DOWN" in LB  # the same marker as the E1-S9 ingress
    assert "admin 127.0.0.1:" in LB  # the admin API on loopback only (§12)


def test_instances_bind_loopback_and_only_the_balancer_holds_the_public_port() -> None:
    assert "CDI_WEBAPP_HOST=127.0.0.1" in APPTIER
    assert "bind {$CDI_LB_BIND}" in LB and "reverse_proxy {$CDI_WEBAPP_UPSTREAMS}" in LB
    assert re.search(r'-Q "\$q" --pool threads', APPTIER)  # one runner, shared GPU slots
    # E9-S2: a pool per side of the pipeline, its queues and size read from cdi_adapter.worker
    assert "pool cpu " in APPTIER and "pool gpu " in APPTIER
    assert "queues_for_role" in APPTIER and "concurrency_for_role" in APPTIER
    assert "sha512sum -c" in APPTIER  # the pinned Caddy, checksum-verified


def test_the_pod_and_the_ingress_vm_pin_the_same_caddy() -> None:
    from cdi_adapter.podmanifest import parse_versions

    pod = parse_versions((RUNPOD / "versions.env").read_text(encoding="utf-8"))
    vm = parse_versions((ROOT / "infra/ingress/versions.env").read_text(encoding="utf-8"))
    assert pod["CADDY_VERSION"] == vm["CADDY_VERSION"]
    assert re.fullmatch(r"[0-9a-f]{128}", pod["CADDY_SHA512"])


def test_start_all_starts_the_app_tier() -> None:
    start = (RUNPOD / "start_all.sh").read_text(encoding="utf-8")
    assert 'bash "$HERE/start_apptier.sh"' in start


def test_the_app_tier_script_is_valid_bash() -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    r = subprocess.run([bash, "-n", str(RUNPOD / "start_apptier.sh")], capture_output=True,
                       text=True)  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert APPTIER.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in APPTIER


def test_the_drill_reads_the_balancers_own_log_lines() -> None:
    from scripts.app_ha_drill import back_in_rotation_at, left_rotation_at

    lines = [json.dumps(e) for e in (
        {"ts": 99.0, "logger": "http.handlers.reverse_proxy.health_checker.active",
         "msg": "HTTP request failed", "host": "127.0.0.1:8881"},  # before the kill: ignored
        {"ts": 101.5, "logger": "http.handlers.reverse_proxy.health_checker.active",
         "msg": "HTTP request failed", "host": "127.0.0.1:8882"},  # the other instance
        {"ts": 102.0, "logger": "http.handlers.reverse_proxy.health_checker.active",
         "msg": "HTTP request failed", "host": "127.0.0.1:8881"},
        {"ts": 130.0, "logger": "http.handlers.reverse_proxy.health_checker.active",
         "msg": "host is up", "host": "127.0.0.1:8881"},
    )] + ["not json"]  # fmt: skip
    assert left_rotation_at(lines, "127.0.0.1:8881", after=100.0) == 102.0
    assert back_in_rotation_at(lines, "127.0.0.1:8881", after=120.0) == 130.0
    assert left_rotation_at(lines, "127.0.0.1:8883", after=0) is None
