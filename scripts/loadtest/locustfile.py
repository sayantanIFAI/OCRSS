"""Locust load test: N synthetic patients x 5-10 documents, concurrently (backlog E1-S7; ADR-0002).

One Locust user = one patient, who does exactly what the counter does (§7.1/§7.2):

    POST /api/jobs (all of the patient's files at once) -> poll GET /api/jobs/{id} until the job
    leaves "running" -> GET /api/jobs/{id}/fhir

and is then done. The measured latency is from the end of the upload (the SLA's "last upload") to
the bundles being returned. When every patient has finished, the test stops and writes the report
(scripts/loadtest/report.py). Run it through scripts/load_test.py, which sets the defaults.
"""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path
from typing import Any

import gevent
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from report import Sample, build, dump, render_text
from synthetic import Patient, make_patient

STATE: dict[str, Any] = {"patients": [], "samples": [], "next": itertools.count()}
# per-request timeouts: a hung server must not stall a patient past --cdi-job-timeout-s
UPLOAD_TIMEOUT_S, POLL_TIMEOUT_S, FHIR_TIMEOUT_S = 300, 30, 120


@events.init_command_line_parser.add_listener
def _args(parser: Any) -> None:
    g = parser.add_argument_group("CDI load test (E1-S7)")
    g.add_argument("--cdi-docs-min", type=int, default=5, help="documents per patient, lowest")
    g.add_argument("--cdi-docs-max", type=int, default=10, help="documents per patient, highest")
    g.add_argument("--cdi-seed", type=int, default=7, help="same seed = identical documents")
    g.add_argument("--cdi-sla-s", type=float, default=300.0, help="SLA, last upload -> ready")
    g.add_argument("--cdi-bin-s", type=float, default=30.0, help="histogram bin width")
    g.add_argument("--cdi-poll-s", type=float, default=2.0, help="job poll interval")
    g.add_argument("--cdi-job-timeout-s", type=float, default=1800.0, help="give up on one job")
    g.add_argument("--cdi-report", default="load_report", help="report path prefix (.json/.txt)")
    g.add_argument("--cdi-enforce-sla", action="store_true", help="exit 1 when the SLA is missed")
    g.add_argument(
        "--cdi-user",
        default="",
        help="sign in as this intake user (E11-S1); "
        "password from the CDI_LOAD_PASSWORD environment variable",
    )


@events.test_start.add_listener
def _prepare(environment: Any, **_: Any) -> None:
    o = environment.parsed_options
    STATE["patients"] = [
        make_patient(i, seed=o.cdi_seed, docs_min=o.cdi_docs_min, docs_max=o.cdi_docs_max)
        for i in range(o.num_users)
    ]
    STATE["samples"] = []
    STATE["next"] = itertools.count()
    docs = sum(len(p.documents) for p in STATE["patients"])
    print(f"cdi-load: {o.num_users} patients, {docs} documents prepared -> {environment.host}")


def _finish(environment: Any) -> None:
    o = environment.parsed_options
    samples = sorted(STATE["samples"], key=lambda s: s.patient)
    target = {
        "host": environment.host,
        "patients": o.num_users,
        "docs_min": o.cdi_docs_min,
        "docs_max": o.cdi_docs_max,
        "seed": o.cdi_seed,
    }
    report = build(samples, sla_s=o.cdi_sla_s, bin_s=o.cdi_bin_s, target=target)
    Path(o.cdi_report).parent.mkdir(parents=True, exist_ok=True)
    dump(report, f"{o.cdi_report}.json")
    text = render_text(report)
    Path(f"{o.cdi_report}.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"cdi-load: report -> {o.cdi_report}.json / .txt")
    complete = len(samples) == o.num_users
    if not complete:
        print(f"cdi-load: only {len(samples)}/{o.num_users} patients finished (run-time limit?)")
    environment.process_exit_code = 0
    if (
        not complete
        or any(not s.ok for s in samples)
        or (o.cdi_enforce_sla and not report["sla_met"])
    ):
        environment.process_exit_code = 1


@events.test_stop.add_listener
def _report(environment: Any, **_: Any) -> None:
    _finish(environment)


class CounterPatient(HttpUser):
    """One patient at the counter: upload everything, wait for the result, fetch the bundles."""

    wait_time = constant(0)
    sign_in_error: str | None = None

    def on_start(self) -> None:
        # E11-S1: same-origin requests from a signed-in counter (intake) account
        self.client.headers["Origin"] = self.host.rstrip("/")
        user = self.environment.parsed_options.cdi_user
        if not user:
            return
        with self.client.post("/login", name="POST /login", allow_redirects=False,
                              data={"username": user,
                                    "password": os.environ.get("CDI_LOAD_PASSWORD", "")},
                              timeout=POLL_TIMEOUT_S, catch_response=True) as resp:  # fmt: skip
            if resp.status_code != 303 or "cdi_session" not in self.client.cookies:
                self.sign_in_error = f"sign-in as {user!r} failed (HTTP {resp.status_code})"
                resp.failure(self.sign_in_error)

    def _record(self, sample: Sample) -> None:
        STATE["samples"].append(sample)
        if sample.ready_s is not None:
            events.request.fire(
                request_type="E2E",
                name="upload -> bundle-ready",
                response_time=sample.ready_s * 1000,
                response_length=0,
                exception=None if sample.ok else RuntimeError(sample.state),
                context={},
            )
        if len(STATE["samples"]) >= len(STATE["patients"]):
            gevent.spawn_later(0.1, self.environment.runner.quit)  # every patient is done

    @task
    def visit(self) -> None:
        index = next(STATE["next"])
        if index >= len(STATE["patients"]):
            raise StopUser()
        patient: Patient = STATE["patients"][index]
        o = self.environment.parsed_options
        n = len(patient.documents)
        if self.sign_in_error:
            self._record(Sample(index, n, 0.0, None, "request_failed", self.sign_in_error))
            raise StopUser()

        files = [("files", (d.filename, d.content, d.mime)) for d in patient.documents]
        t0 = time.monotonic()
        with self.client.post(
            "/api/jobs",
            files=files,
            name="POST /api/jobs",
            timeout=UPLOAD_TIMEOUT_S,
            catch_response=True,
        ) as resp:
            uploaded = time.monotonic()
            if resp.status_code != 202:
                resp.failure(f"HTTP {resp.status_code}")
                self._record(
                    Sample(
                        index,
                        n,
                        uploaded - t0,
                        None,
                        "request_failed",
                        f"upload HTTP {resp.status_code}",
                    )
                )
                raise StopUser()
            job_id = resp.json()["job_id"]

        state, job = "running", {}
        while state == "running":
            if time.monotonic() - uploaded > o.cdi_job_timeout_s:
                self._record(Sample(index, n, uploaded - t0, None, "timeout", job_id))
                raise StopUser()
            gevent.sleep(o.cdi_poll_s)
            with self.client.get(
                f"/api/jobs/{job_id}",
                name="GET /api/jobs/[id]",
                timeout=POLL_TIMEOUT_S,
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code}")
                    continue  # a transient poll failure is retried until the job timeout
                job = resp.json()
                state = job.get("state", "running")

        docs = job.get("documents") or []
        failed = sum(1 for d in docs if d.get("status") == "error")
        detail = job_id
        if failed:
            first = next(d.get("error") for d in docs if d.get("status") == "error")
            detail = f"{job_id}: {failed}/{len(docs)} documents failed, first: {str(first)[:160]}"
        if state == "review" and docs and failed == len(docs):
            # nothing was ingested, so there is no patient and no bundle to fetch
            self._record(Sample(index, n, uploaded - t0, None, "documents_failed", detail, failed))
            raise StopUser()
        if state == "review":  # a mismatch has no bundle: the identity guard purged its facts
            with self.client.get(
                f"/api/jobs/{job_id}/fhir",
                name="GET /api/jobs/[id]/fhir",
                timeout=FHIR_TIMEOUT_S,
                catch_response=True,
            ) as resp:
                try:
                    bundles = resp.json().get("bundles") if resp.status_code == 200 else None
                    valid = (
                        isinstance(bundles, list)
                        and bool(bundles)
                        and all(
                            isinstance(item, dict)
                            and isinstance(item.get("bundle"), dict)
                            and item["bundle"].get("resourceType") == "Bundle"
                            and item["bundle"].get("type") == "document"
                            for item in bundles
                        )
                    )
                except (ValueError, AttributeError):
                    valid = False
                if not valid:
                    resp.failure(f"missing/invalid document bundles (HTTP {resp.status_code})")
                    self._record(
                        Sample(
                            index,
                            n,
                            uploaded - t0,
                            None,
                            "request_failed",
                            f"fhir HTTP {resp.status_code}",
                            failed,
                        )
                    )
                    raise StopUser()
        ready = time.monotonic()
        ready_s = ready - uploaded if state in ("review", "mismatch") else None
        self._record(Sample(index, n, uploaded - t0, ready_s, state, detail, failed))
        raise StopUser()
