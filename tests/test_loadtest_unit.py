"""E1-S7: load-test harness (Locust, 30 x 5-10 documents, p50/p95/p99 against the SLA)."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "loadtest"))

import load_test  # noqa: E402
import report as rep  # noqa: E402
import synthetic  # noqa: E402

# never import locust here: it gevent-monkey-patches the whole pytest process on import
if importlib.util.find_spec("locust") is None:
    pytest.skip("locust not installed (pip install -e .[load])", allow_module_level=True)


# ---------------------------------------------------------------- synthetic patients


def test_patients_are_synthetic_distinct_and_within_the_upload_limit() -> None:
    patients = [synthetic.make_patient(i, seed=7, dpi=36) for i in range(30)]  # low dpi: speed
    assert len({p.name for p in patients}) == 30
    for p in patients:
        assert 5 <= len(p.documents) <= 10  # ADR-0002: 5-10 per patient; §7.1: <= 10 per job
        assert len({d.filename for d in p.documents}) == len(p.documents)
        for d in p.documents:
            assert d.content.startswith(b"\x89PNG") and d.mime == "image/png"
    kinds = {d.doc_type for p in patients for d in p.documents}
    assert kinds == set(synthetic.DOC_TYPES)


def test_same_seed_same_documents_other_seed_other_documents() -> None:
    a = synthetic.make_patient(3, seed=7, docs_min=5, docs_max=5)
    b = synthetic.make_patient(3, seed=7, docs_min=5, docs_max=5)
    c = synthetic.make_patient(3, seed=8, docs_min=5, docs_max=5)
    assert [d.content for d in a.documents] == [d.content for d in b.documents]
    assert a.name != c.name and a.documents[0].content != c.documents[0].content


def test_documents_carry_the_patient_and_classifier_signals() -> None:
    import random

    from cdi_adapter.classify.service import _SIGNALS, _heuristic_classify  # the §5 S2 scorer

    p = synthetic.make_patient(0, seed=7, docs_min=5, docs_max=5)
    for doc_type in synthetic.DOC_TYPES:
        text = "\n".join(synthetic.RENDER[doc_type](p, random.Random(1), synthetic.date.today()))
        assert p.name in text
        if doc_type in _SIGNALS:  # classified with no VLM call (§7.2)
            got = _heuristic_classify(text)
            assert got is not None and got.doc_type == doc_type, (doc_type, got)
    Image.open(io.BytesIO(p.documents[0].content)).verify()


def test_bad_document_ranges_are_refused() -> None:
    for lo, hi in ((0, 5), (6, 5), (5, 11)):
        with pytest.raises(ValueError):
            synthetic.make_patient(0, seed=7, docs_min=lo, docs_max=hi)


# ---------------------------------------------------------------- report


def test_percentiles_are_exact_nearest_rank() -> None:
    vals = [float(v) for v in range(1, 101)]
    assert rep.percentile(vals, 50) == 50 and rep.percentile(vals, 95) == 95
    assert rep.percentile(vals, 99) == 99 and rep.percentile([7.0], 99) == 7
    assert rep.percentile([3.0, 1.0, 2.0], 50) == 2
    with pytest.raises(ValueError):
        rep.percentile([], 50)


def _s(i: int, ready: float | None, state: str = "review", failed: int = 0) -> rep.Sample:
    return rep.Sample(i, 5, 0.4, ready, state, "", failed)


def test_report_judges_the_sla_on_p95_and_counts_every_failure() -> None:
    samples = [_s(i, 60.0 + i) for i in range(19)] + [_s(19, 350.0)]
    r = rep.build(samples, sla_s=300, bin_s=30, target={"host": "h"})
    assert r["latency_s"]["p50"] == 69 and r["latency_s"]["p95"] == 78  # rank 19 of 20
    assert r["within_sla"] == 19 and r["sla_met"] is True

    failing = [*samples[:18], _s(18, None, "timeout"), _s(19, 90.0, "review", failed=1)]
    r = rep.build(failing, sla_s=300, bin_s=30, target={"host": "h"})
    assert r["ready"] == 18 and r["sla_met"] is False  # not every patient is ready
    assert r["states"] == {"review": 19, "timeout": 1} and r["documents_failed"] == 1

    slow = [_s(i, 400.0) for i in range(10)]
    assert rep.build(slow, sla_s=300, bin_s=30, target={})["sla_met"] is False


def test_mismatch_and_partial_failures_are_never_ready() -> None:
    assert _s(0, 30.0).ok
    assert not _s(0, 30.0, "mismatch").ok
    assert not _s(0, 30.0, failed=2).ok
    assert not _s(0, None, "request_failed").ok


def test_histogram_and_text_mark_everything_beyond_the_sla() -> None:
    r = rep.build([_s(0, 10.0), _s(1, 290.0), _s(2, 650.0)], sla_s=300, bin_s=100,
                  target={"host": "http://x"})  # fmt: skip
    bins = r["histogram"]
    assert [b["count"] for b in bins] == [1, 0, 1, 0, 0, 0, 1]
    assert [b["within_sla"] for b in bins][:4] == [True, True, True, False]
    text = rep.render_text(r)
    assert "p50 290.0s  p95 650.0s  p99 650.0s" in text and "SLA NOT MET" in text
    assert re.search(r"^!\s+600-700\s+s\s+1 #+$", text, re.M)


# ---------------------------------------------------------------- runner


def test_runner_defaults_are_the_adr_0002_burst() -> None:
    args = load_test.argparse.Namespace(
        host="https://emr.example.in", patients=30, docs_min=5, docs_max=10, seed=7, sla_s=300.0,
        poll_s=2.0, job_timeout_s=1800.0, report="r", enforce_sla=False)  # fmt: skip
    cmd = load_test.command(args, ["--loglevel", "WARNING"])
    joined = " ".join(cmd)
    assert cmd[1:3] == ["-m", "locust"] and "--headless" in cmd
    assert "-u 30 -r 30" in joined  # every patient arrives at once: the design burst
    assert "--host https://emr.example.in" in joined and "--cdi-sla-s 300.0" in joined
    assert cmd[-2:] == ["--loglevel", "WARNING"]
    src = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert 'default=30, help="concurrent patients' in src and "default=300.0" in src


# ---------------------------------------------------------------- the real harness, end to end
# The harness itself (Locust + locustfile + report) runs for real here, against a small server
# that speaks the §7.1 job API contract with controlled timings. This checks the harness's
# measurement; it is not the E1-S7 dry run against the CDI stack (that one needs real MinIO).


class _Contract(BaseHTTPRequestHandler):
    """POST /api/jobs, GET /api/jobs/{id}, GET /api/jobs/{id}/fhir - per §7.1."""

    jobs: dict[str, dict[str, Any]] = {}
    plan: list[tuple[float, str, int]] = []  # (seconds until done, final state, failed docs)
    lock = threading.Lock()

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    empty_bundles = False
    login_password: str | None = None  # set: /api/* requires the session cookie (E11-S1)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/login":
            if self.login_password and f"password={self.login_password}".encode() in body:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", "cdi_session=ok; HttpOnly; Path=/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(401, {"detail": "wrong password"})
        if self.login_password and "cdi_session=ok" not in (self.headers.get("Cookie") or ""):
            return self._send(401, {"detail": "sign in required"})
        files = body.count(b'name="files"')
        with self.lock:
            delay, final, failed = self.plan[len(self.jobs) % len(self.plan)]
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"t": time.monotonic() + delay, "final": final, "n": files,
                                 "failed": failed}  # fmt: skip
        self._send(202, {"job_id": job_id})

    def do_GET(self) -> None:  # noqa: N802
        parts = self.path.strip("/").split("/")
        job = self.jobs.get(parts[2]) if len(parts) >= 3 else None
        if job is None:
            return self._send(404, {"detail": "job not found"})
        done = time.monotonic() >= job["t"]
        if len(parts) == 4 and parts[3] == "fhir":
            bundles = (
                []
                if self.empty_bundles
                else [
                    {"bundle": {"resourceType": "Bundle", "type": "document"}}
                    for _ in range(job["n"])
                ]
            )
            return self._send(200, {"bundles": bundles}) if done else self._send(409, {})
        docs = [{"filename": f"d{i}", "status": "error" if i < job["failed"] and done else "done",
                 "error": "boom" if i < job["failed"] and done else None}
                for i in range(job["n"])]  # fmt: skip
        self._send(200, {"job_id": parts[2], "state": job["final"] if done else "running",
                         "documents": docs})  # fmt: skip


@pytest.fixture
def contract() -> Iterator[str]:
    _Contract.jobs = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Contract)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_the_harness_measures_a_concurrent_burst_end_to_end(contract: str, tmp_path: Path) -> None:
    _Contract.plan = [(1.0, "review", 0), (2.0, "review", 0), (3.0, "review", 0),
                      (1.0, "mismatch", 0), (1.0, "review", 2), (1.0, "error", 5)]  # fmt: skip
    out = tmp_path / "rep"
    t0 = time.monotonic()
    rc = load_test.main(["--host", contract, "--patients", "6", "--docs-min", "5",
                         "--docs-max", "6", "--poll-s", "0.2", "--job-timeout-s", "30",
                         "--sla-s", "2.5", "--report", str(out)])  # fmt: skip
    wall = time.monotonic() - t0
    r = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))

    assert rc == 1 and r["patients"] == 6  # functional failures always fail
    assert wall < 25  # concurrent, and it quits on its own when the last job is done
    assert r["states"] == {"review": 4, "mismatch": 1, "error": 1}
    assert r["ready"] == 3  # the review job with 2 failed documents is not ready
    assert r["documents_failed"] == 2 + 5
    lat = r["latency_s"]
    assert 0.9 <= lat["p50"] <= 2.9 and 2.9 <= lat["p95"] <= 4.5 and lat["p99"] == lat["p95"]
    assert r["within_sla"] == 2 and r["sla_met"] is False
    assert all(5 <= s["documents"] <= 6 for s in r["samples"])
    text = out.with_suffix(".txt").read_text(encoding="utf-8")
    assert "6 patients" in text and "SLA NOT MET" in text

    strict = ["--host", contract, "--patients", "2", "--docs-max", "5", "--poll-s", "0.2"]
    rc = load_test.main([*strict, "--sla-s", "0.5", "--report", str(out), "--enforce-sla"])
    assert rc == 1  # --enforce-sla: a missed SLA fails the run


def test_a_job_that_never_finishes_times_out(contract: str, tmp_path: Path) -> None:
    _Contract.plan = [(999.0, "review", 0)]
    out = tmp_path / "rep"
    rc = load_test.main(["--host", contract, "--patients", "1", "--docs-max", "5", "--poll-s",
                         "0.2", "--job-timeout-s", "1", "--report", str(out)])  # fmt: skip
    r = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert rc == 1 and r["states"] == {"timeout": 1} and r["ready"] == 0


def test_a_rejected_upload_is_a_failure_not_a_hang(tmp_path: Path) -> None:
    out = tmp_path / "rep"
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "load_test.py"), "--host", "http://127.0.0.1:9",
         "--patients", "2", "--docs-max", "5", "--report", str(out)],
        capture_output=True, text=True, timeout=120).returncode  # fmt: skip
    r = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert rc == 1 and r["states"] == {"request_failed": 2}


def test_the_harness_signs_in_as_a_counter_user(
    contract: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Contract.plan = [(0.5, "review", 0)]
    _Contract.login_password = "counter-passphrase-1"
    try:
        out = tmp_path / "rep"
        args = ["--host", contract, "--patients", "2", "--docs-max", "5", "--poll-s", "0.2",
                "--report", str(out), "--user", "counter1"]  # fmt: skip
        monkeypatch.setenv("CDI_LOAD_PASSWORD", "counter-passphrase-1")
        assert load_test.main(args) == 0
        r = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        assert r["states"] == {"review": 2}  # signed in: uploads accepted

        monkeypatch.setenv("CDI_LOAD_PASSWORD", "wrong")
        assert load_test.main(args) == 1
        r = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        assert r["states"] == {"request_failed": 2}
        assert all("sign-in as 'counter1' failed" in s["detail"] for s in r["samples"])
    finally:
        _Contract.login_password = None


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--poll-s", "0"),
        ("--sla-s", "nan"),
        ("--job-timeout-s", "inf"),
        ("--docs-min", "0"),
        ("--docs-max", "11"),
    ],
)
def test_runner_rejects_invalid_bounds_before_spawning(flag, value, monkeypatch):
    monkeypatch.setattr(load_test.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(SystemExit):
        load_test.main(["--host", "http://localhost", flag, value])


def test_histogram_includes_sample_at_last_bin_boundary():
    bins = rep.histogram([600.0], bin_s=30, sla_s=300)
    assert sum(b["count"] for b in bins) == 1
    assert bins[-1]["from_s"] == 600


@pytest.mark.parametrize("width", [0, -1, float("nan"), float("inf")])
def test_histogram_rejects_invalid_width_without_looping(width):
    with pytest.raises(ValueError):
        rep.histogram([1.0], bin_s=width, sla_s=300)


def test_empty_bundle_response_cannot_be_counted_as_ready(contract, tmp_path, monkeypatch):
    _Contract.plan = [(0.1, "review", 0)]
    monkeypatch.setattr(_Contract, "empty_bundles", True)
    out = tmp_path / "empty"
    rc = load_test.main(
        [
            "--host",
            contract,
            "--patients",
            "1",
            "--docs-max",
            "5",
            "--poll-s",
            "0.1",
            "--report",
            str(out),
        ]
    )
    report = json.loads(out.with_suffix(".json").read_text())
    assert rc == 1 and report["ready"] == 0
    assert report["states"] == {"request_failed": 1}
