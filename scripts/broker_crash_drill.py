"""Broker crash-durability drill: does the broker lose or duplicate work? (backlog E9-S8)

E9-S1 chose Celery with a **Redis** broker and named RabbitMQ as the hardened option, so one
question is still open: killed mid-batch, does Redis lose a document or run one twice? This drill
is the measurement the decision rests on. It runs on the pod / fallback host (Linux), against the
running app tier, under the **E9-S6 load shape** (30 patients × 5-10 documents):

    python scripts/broker_crash_drill.py                      # 3 kills, one run, the live broker
    python scripts/broker_crash_drill.py --runs 3 --kills 1   # the acceptance: >= 3 kills in all
    CDI_BROKER_URL=amqp://guest:guest@127.0.0.1:5672// python scripts/broker_crash_drill.py \
        --label rabbitmq --broker-restart "systemctl start rabbitmq-server"

Each run:

1. records what the broker's own storage is set to (``durability.persistence`` - for Redis the
   live ``CONFIG GET``, which is what "Redis's default persistence configuration" means here);
2. uploads the burst through the balancer, spread over ``--spread-s``;
3. **kills the broker** (SIGKILL, no warning) at randomised points strictly inside the burst -
   ``--kills`` of them, from ``--seed``, so a run can be repeated exactly - and brings it back
   with ``--broker-restart`` after ``--down-s``. ``--kill-worker`` also kills a job worker, which
   is the other half of "the broker/worker process is killed mid-batch";
4. waits for every acknowledged job to end, then measures from PostgreSQL
   (``cdi_adapter.durability``): documents **lost** (never resumed), **duplicated** (a second
   extraction row or open review item - what E9-S3's idempotency keys do *not* already catch),
   given up, and how many jobs were resumed;
5. judges it against the agreed tolerance: **0 lost, 0 duplicated, 0 given up**.

Report: ``$WS/logs/ha/broker-drill-<label>-<time>.json`` and ``last-broker-drill.json``; exit 1
when any run misses the tolerance. A failing Redis run is the trigger in E9-S8 to set
``CDI_BROKER_URL`` to RabbitMQ and run this again - the queues, routing, lanes and task code are
the same either way (``cdi_adapter.worker.broker_conf``) - and to write the verdict, with these
numbers, into ARCHITECTURE.md §7.6. Kafka is out of scope by decision, not oversight.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
WS = Path(os.environ.get("WS", "/workspace"))


def _synthetic():  # noqa: ANN202 - the E1-S7 corpus module
    spec = importlib.util.spec_from_file_location(
        "synthetic", ROOT / "scripts/loadtest/synthetic.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["synthetic"] = mod
    spec.loader.exec_module(mod)
    return mod


def kill_schedule(kills: int, window_s: float, seed: int, margin_s: float = 1.0) -> list[float]:
    """When to kill the broker, in seconds from the start of the burst.

    Randomised, because a kill always at the same moment proves one moment only - but **inside**
    the burst (``margin_s`` away from either end, so the first upload is in flight and the last
    has not been sent yet), ordered, and never twice at the same instant. Derived from ``seed``
    alone, so a run that found a loss can be replayed exactly.
    """
    if kills <= 0:
        return []
    lo, hi = margin_s, window_s - margin_s
    if hi <= lo:
        raise ValueError(f"a {window_s}s burst is too short for a kill {margin_s}s inside it")
    rng = random.Random(seed)
    picked: list[float] = []
    for _ in range(kills):
        for _try in range(100):
            t = round(rng.uniform(lo, hi), 3)
            if all(abs(t - p) >= 0.25 for p in picked):
                picked.append(t)
                break
        else:  # pragma: no cover - only with many kills in a very short burst
            raise ValueError(f"cannot fit {kills} kills into {window_s}s")
    return sorted(picked)


def pid_of(pattern: str) -> int | None:
    """The pid of the single process whose command line matches (``pgrep -f``)."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True,
                             check=False)  # fmt: skip
    except FileNotFoundError:  # pragma: no cover - pgrep is on the pod
        return None
    pids = [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != os.getpid()]
    return pids[0] if pids else None


class Drill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.host = args.host.rstrip("/")
        self.events: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def event(self, what: str, **kw: Any) -> float:
        t = time.time()
        self.events.append({"t": t, "at": datetime.fromtimestamp(t, UTC).isoformat(),
                            "event": what, **kw})  # fmt: skip
        print(f"[{self.events[-1]['at']}] {what} {kw if kw else ''}", flush=True)
        return t

    # -------------------------------------------------------- account (as the E10-S9 drill)
    def account(self) -> tuple[str, str]:
        from sqlalchemy import text

        from cdi_adapter import repo
        from cdi_adapter.db import session_scope
        from cdi_adapter.webapp.auth import hash_password

        user, pw = f"broker-drill-{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
        with session_scope() as c:
            c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                           "VALUES (:u, 'E9-S8 broker drill', 'intake', :p)"),
                      {"u": user, "p": hash_password(pw)})  # fmt: skip
            detail = {"role": "intake", "purpose": "E9-S8"}
            repo.write_audit(c, actor="broker-drill", action="create", entity="web_user",
                             entity_id=user, detail=detail)  # fmt: skip
        return user, pw

    def retire(self, user: str) -> None:
        from sqlalchemy import text

        from cdi_adapter.db import session_scope

        with session_scope() as c:
            c.execute(text("UPDATE web_user SET active = false WHERE username = :u"),
                      {"u": user})  # fmt: skip
            c.execute(text("DELETE FROM web_session WHERE user_id = "
                           "(SELECT id FROM web_user WHERE username = :u)"),
                      {"u": user})  # fmt: skip

    def session(self, user: str, pw: str) -> httpx.Client:
        c = httpx.Client(base_url=self.host, timeout=120, headers={"Origin": self.host})
        r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        token = r.cookies.get("cdi_session")
        if r.status_code != 303 or not token:
            raise SystemExit(f"drill sign-in failed: HTTP {r.status_code}")
        c.headers["Cookie"] = f"cdi_session={token}"
        return c

    # -------------------------------------------------------- load
    def upload(self, client: httpx.Client, patient: Any, delay: float, t0: float) -> None:
        time.sleep(max(0.0, t0 + delay - time.monotonic()))
        files = [("files", (d.filename, d.content, d.mime)) for d in patient.documents]
        rec: dict[str, Any] = {"patient": patient.index, "docs": len(files), "t": time.time()}
        try:
            r = client.post("/api/jobs", files=files)
            rec["status"] = r.status_code
            if r.status_code == 202:
                rec["job_id"] = r.json()["job_id"]
            else:
                rec["error"] = r.text[:200]
        except httpx.HTTPError as exc:
            rec["status"], rec["error"] = None, f"{type(exc).__name__}: {exc}"
        with self.lock:
            self.uploads.append(rec)

    # -------------------------------------------------------- kills
    def kill_broker(self, at: float) -> dict[str, Any]:
        pid = pid_of(self.a.broker_process)
        rec: dict[str, Any] = {"at_s": at, "target": self.a.broker_process, "pid": pid}
        if pid is None:
            rec["skipped"] = "process not found"
            self.event("broker not found", pattern=self.a.broker_process)
            return rec
        os.kill(pid, signal.SIGKILL)
        rec["t"] = self.event("SIGKILL broker", pid=pid, at_s=at)
        time.sleep(self.a.down_s)
        if self.a.broker_restart:
            subprocess.run(self.a.broker_restart, shell=True, check=False)  # noqa: S602
            rec["restarted"] = self.event("broker restarted", cmd=self.a.broker_restart)
        return rec

    def kill_worker(self) -> dict[str, Any]:
        pid = pid_of(self.a.worker_process)
        if pid is None:
            self.event("worker not found", pattern=self.a.worker_process)
            return {"skipped": "process not found"}
        os.kill(pid, signal.SIGKILL)
        t = self.event("SIGKILL worker", pid=pid)
        if self.a.worker_restart:
            subprocess.run(self.a.worker_restart, shell=True, check=False)  # noqa: S602
            self.event("worker restarted", cmd=self.a.worker_restart)
        return {"t": t, "pid": pid}

    def wait_jobs(self, job_ids: list[str], timeout_s: float) -> dict[str, Any]:
        """Wait for every acknowledged job to end. A job that never ends is the loss this drill
        is looking for, so the wait has a deadline and reports what was still open."""
        from sqlalchemy import text

        from cdi_adapter.db import session_scope

        deadline = time.monotonic() + timeout_s
        open_ids = list(job_ids)
        while open_ids and time.monotonic() < deadline:
            with session_scope() as c:
                open_ids = list(c.execute(
                    text("SELECT id FROM web_job WHERE id = ANY(:i) AND finished_at IS NULL"),
                    {"i": job_ids}).scalars())  # fmt: skip
            if open_ids:
                time.sleep(2)
        return {"waited_s": round(timeout_s - max(0.0, deadline - time.monotonic()), 1),
                "still_open": open_ids}  # fmt: skip

    # -------------------------------------------------------- one run
    def run_once(self, run: int, user: str, pw: str) -> dict[str, Any]:
        from cdi_adapter import durability
        from cdi_adapter.db import session_scope
        from cdi_adapter.worker import broker_url

        a = self.a
        url = broker_url()
        seed = a.seed + run
        syn = _synthetic()
        corpus_seed = (int(time.time()) + run) % 100000  # fresh scans: no ingest dedupe across runs
        patients = [syn.make_patient(i, seed=corpus_seed, docs_min=a.docs_min, docs_max=a.docs_max)
                    for i in range(a.patients)]  # fmt: skip
        schedule = kill_schedule(a.kills, a.spread_s, seed, margin_s=a.kill_margin_s)
        out: dict[str, Any] = {
            "run": run,
            "label": a.label,
            "broker": url.split("://", 1)[0],
            "seed": seed,
            "kill_schedule_s": schedule,
            "persistence": durability.persistence(url),
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.event("run start", run=run, patients=a.patients,
                   documents=sum(len(p.documents) for p in patients), kills=schedule,
                   broker=out["broker"], label=a.label)  # fmt: skip

        self.uploads = []
        clients = [self.session(user, pw) for _ in range(min(8, a.patients))]
        t0 = time.monotonic()
        step = a.spread_s / max(1, a.patients - 1)
        threads = [threading.Thread(target=self.upload,
                                    args=(clients[i % len(clients)], p, i * step, t0))
                   for i, p in enumerate(patients)]  # fmt: skip
        for t in threads:
            t.start()
        kills = []
        for at in schedule:
            time.sleep(max(0.0, t0 + at - time.monotonic()))
            kills.append(self.kill_broker(at))
        if a.kill_worker:
            kills.append({"worker": self.kill_worker()})
        out["kills"] = kills
        for t in threads:
            t.join()
        for c in clients:
            c.close()
        self.event("uploads done", acknowledged=sum(1 for u in self.uploads if u.get("job_id")))

        acked = [u for u in self.uploads if u.get("job_id")]
        out["uploads"] = {"sent": len(self.uploads), "acknowledged": len(acked),
                          "not_acknowledged": [u for u in self.uploads
                                               if not u.get("job_id")]}  # fmt: skip
        out["wait"] = self.wait_jobs([u["job_id"] for u in acked], a.job_timeout_s)
        with session_scope() as c:
            out["measured"] = durability.measure(c, [u["job_id"] for u in acked])
        out["ok"] = bool(out["measured"]["ok"])
        self.event("run done", run=run, ok=out["ok"], counts=out["measured"]["counts"],
                   resumed=out["measured"]["resumed"])  # fmt: skip
        return out

    def run(self) -> int:
        a = self.a
        user, pw = self.account()
        scenario = {"patients": a.patients, "docs": [a.docs_min, a.docs_max],
                    "spread_s": a.spread_s, "runs": a.runs, "kills_per_run": a.kills}  # fmt: skip
        report: dict[str, Any] = {
            "story": "E9-S8",
            "label": a.label,
            "started_at": datetime.now(UTC).isoformat(),
            "scenario": scenario,
        }
        try:
            report["runs"] = [self.run_once(i, user, pw) for i in range(a.runs)]
        finally:
            self.retire(user)
            report["events"] = self.events
        runs = report["runs"]
        report["kills_total"] = sum(len(r["kill_schedule_s"]) for r in runs)
        report["totals"] = {
            k: sum(r["measured"]["counts"][k] for r in runs)
            for k in ("lost", "duplicated", "given_up", "jobs_sharing_a_key")
        }
        report["resumed_total"] = sum(r["measured"]["resumed"] for r in runs)
        # the acceptance asks for at least three kills across repeated runs
        report["kills_enough"] = report["kills_total"] >= 3
        report["ok"] = all(r["ok"] for r in runs) and report["kills_enough"]
        judgement = ("within tolerance (0/0/0)" if report["ok"] else
                     "OUTSIDE TOLERANCE: swap the broker (CDI_BROKER_URL=amqp://...) "
                     "and run this again")  # fmt: skip
        report["verdict"] = (
            f"{a.label}: {report['totals']['lost']} lost, {report['totals']['duplicated']} "
            f"duplicated, {report['totals']['given_up']} given up over {report['kills_total']} "
            f"kill(s) in {a.runs} run(s) - {judgement}"
        )
        out_dir = WS / "logs" / "ha"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        body = json.dumps(report, indent=2, default=str)
        (out_dir / f"broker-drill-{a.label}-{stamp}.json").write_text(body, encoding="utf-8")
        (out_dir / "last-broker-drill.json").write_text(body, encoding="utf-8")
        print(report["verdict"], flush=True)
        print(f"report: {out_dir / 'last-broker-drill.json'}", flush=True)
        return 0 if report["ok"] else 1


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument("--host", default=os.environ.get("CDI_DRILL_HOST", "http://127.0.0.1:8888"))
    p.add_argument("--label", default="redis", help="which broker this run is evidence for")
    # the E9-S6 scenario: 30 patients x 5-10 documents
    p.add_argument("--patients", type=int, default=30)
    p.add_argument("--docs-min", type=int, default=5)
    p.add_argument("--docs-max", type=int, default=10)
    p.add_argument("--spread-s", type=float, default=60.0, help="the burst window")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--kills", type=int, default=3, help="broker kills per run")
    p.add_argument("--kill-margin-s", type=float, default=2.0,
                   help="how far inside the burst a kill must stay")  # fmt: skip
    p.add_argument("--seed", type=int, default=20260308, help="replays a run's kill times exactly")
    p.add_argument("--down-s", type=float, default=5.0, help="how long the broker stays dead")
    p.add_argument("--broker-process", default="redis-server",
                   help="pgrep -f pattern of the broker to kill")  # fmt: skip
    p.add_argument("--broker-restart", default=os.environ.get("CDI_BROKER_RESTART", ""),
                   help="shell command that brings the broker back")  # fmt: skip
    p.add_argument("--kill-worker", action="store_true", help="also SIGKILL one job worker")
    p.add_argument("--worker-process", default="celery -A cdi_adapter.worker")
    p.add_argument("--worker-restart", default=os.environ.get("CDI_WORKER_RESTART", ""))
    p.add_argument("--job-timeout-s", type=float, default=1800.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse(argv)
    if a.kills < 1 and not a.kill_worker:
        print("nothing would be killed: pass --kills >= 1 or --kill-worker", file=sys.stderr)
        return 2
    if not a.broker_restart:
        print("warning: no --broker-restart, so the broker stays dead after the first kill",
              file=sys.stderr)  # fmt: skip
    return Drill(a).run()


if __name__ == "__main__":  # pragma: no cover - the drill
    raise SystemExit(main())
