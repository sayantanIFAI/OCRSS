"""Kill-instance drill under load for the application tier (backlog E10-S9; docs/ha.md).

Runs on the pod / fallback host (Linux) against the running app tier (start_apptier.sh: the
balancer on :8888, two or more web app instances, the job workers, one or more model gateways):

    python scripts/app_ha_drill.py                       # defaults: 12 patients, 1-3 scans each
    python scripts/app_ha_drill.py --patients 20 --kill-gateway

1. Signs in a throwaway intake account (random password, never shown; deactivated at the end).
2. Uploads the synthetic patients of the E1-S7 corpus through the balancer, spread over
   ``--spread-s``, while a probe asks the balancer for ``/readyz`` every 100 ms.
3. Mid-burst it SIGKILLs web app instance ``--kill-webapp`` (no warning, as in a crash), then
   job worker ``--kill-worker`` while it is running jobs, and with ``--kill-gateway`` one model
   gateway replica of ``CDI_MLSERVE_URL``.
4. Waits for every acknowledged (202) upload's job to end, then checks from PostgreSQL:
   * **zero lost acknowledged uploads**: every acknowledged job finished and none of its
     documents was left unprocessed (pending / running / staged upload missing / gave up);
   * the killed instance **left rotation within the health-check window** (interval + timeout
     of Caddyfile.lb): the balancer's own log line for the failed check, after the kill;
   * no document was processed twice into the results (one extraction row, one open review
     item), and which jobs were taken over from the killed worker (``attempts`` > 1).
5. Restarts what it killed (start_apptier.sh) and checks the instance is back in rotation.

Report: ``$WS/logs/ha/app-drill-<time>.json`` and ``last-app-drill.json``; exit 1 on a failed check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
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
LB_FILE = ROOT / "infra" / "runpod" / "Caddyfile.lb"


def _synthetic():  # noqa: ANN202 - the E1-S7 corpus module
    spec = importlib.util.spec_from_file_location(
        "synthetic", ROOT / "scripts/loadtest/synthetic.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["synthetic"] = mod
    spec.loader.exec_module(mod)
    return mod


def health_window_s(caddyfile: str) -> float:
    """interval + timeout of the balancer's active check: the most a dead instance can stay in
    rotation (the passive check usually takes it out sooner, on the first failed connection)."""

    def secs(name: str) -> float:
        m = re.search(rf"^\s*{name}\s+(\d+(?:\.\d+)?)(ms|s)\s*$", caddyfile, re.M)
        assert m, name
        return float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)

    return secs("health_interval") + secs("health_timeout")


def left_rotation_at(log_lines: list[str], upstream: str, after: float) -> float | None:
    """Time of the balancer's first failed active check of ``upstream`` after ``after``."""
    for line in log_lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (e.get("logger", "").endswith("health_checker.active")
                and e.get("msg") == "HTTP request failed" and e.get("host") == upstream
                and float(e.get("ts", 0)) >= after):  # fmt: skip
            return float(e["ts"])
    return None


def back_in_rotation_at(log_lines: list[str], upstream: str, after: float) -> float | None:
    for line in log_lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (e.get("msg") == "host is up" and e.get("host") == upstream
                and float(e.get("ts", 0)) >= after):  # fmt: skip
            return float(e["ts"])
    return None


def pid_on_port(port: int) -> int | None:
    """The process listening on 127.0.0.1:``port`` (Linux /proc; no extra tools needed)."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for r in rows:
            f = r.split()
            if f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:  # LISTEN
                inodes.add(f[9])
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                link = os.readlink(fd)
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return int(proc.name)
        except OSError:
            continue
    return None


class Drill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.host = args.host.rstrip("/")
        self.events: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.probe: list[tuple[float, bool]] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def event(self, what: str, **kw: Any) -> float:
        t = time.time()
        self.events.append({"t": t, "at": datetime.fromtimestamp(t, UTC).isoformat(),
                            "event": what, **kw})  # fmt: skip
        print(f"[{self.events[-1]['at']}] {what} {kw if kw else ''}", flush=True)
        return t

    # -------------------------------------------------------- account
    def account(self) -> tuple[str, str]:
        from sqlalchemy import text

        from cdi_adapter import repo
        from cdi_adapter.db import session_scope
        from cdi_adapter.webapp.auth import hash_password

        user, pw = f"ha-drill-{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
        with session_scope() as c:
            c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                           "VALUES (:u, 'E10-S9 HA drill', 'intake', :p)"),
                      {"u": user, "p": hash_password(pw)})  # fmt: skip
            repo.write_audit(
                c,
                actor="ha-drill",
                action="create",
                entity="web_user",
                entity_id=user,
                detail={"role": "intake", "purpose": "E10-S9 drill"},
            )
        return user, pw

    def retire(self, user: str) -> None:
        from sqlalchemy import text

        from cdi_adapter import repo
        from cdi_adapter.db import session_scope

        with session_scope() as c:
            c.execute(text("UPDATE web_user SET active = false WHERE username = :u"), {"u": user})
            c.execute(
                text(
                    "DELETE FROM web_session WHERE user_id = "
                    "(SELECT id FROM web_user WHERE username = :u)"
                ),
                {"u": user},
            )
            repo.write_audit(
                c,
                actor="ha-drill",
                action="update",
                entity="web_user",
                entity_id=user,
                detail={"active": False},
            )

    def session(self, user: str, pw: str) -> httpx.Client:
        c = httpx.Client(base_url=self.host, timeout=120, headers={"Origin": self.host})
        r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        token = r.cookies.get("cdi_session")
        if r.status_code != 303 or not token:
            raise SystemExit(f"drill sign-in failed: HTTP {r.status_code}")
        # the cookie is Secure (staff use TLS); on this loopback hop send it explicitly
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

    def probing(self) -> None:
        with httpx.Client(base_url=self.host, timeout=5) as c:
            while not self._stop.is_set():
                try:
                    ok = c.get("/readyz").status_code == 200
                except httpx.HTTPError:
                    ok = False
                self.probe.append((time.time(), ok))
                time.sleep(0.1)

    # -------------------------------------------------------- kills
    @staticmethod
    def _pid(name: str) -> int:
        return int((WS / "run" / f"{name}.pid").read_text().strip())

    def kill(self, what: str, pid: int, **kw: Any) -> float:
        os.kill(pid, signal.SIGKILL)
        return self.event(f"SIGKILL {what}", pid=pid, **kw)

    def run(self) -> int:
        a = self.a
        caddy = LB_FILE.read_text(encoding="utf-8")
        window = health_window_s(caddy)
        base = int(os.environ.get("CDI_WEBAPP_BASE_PORT", "8881"))
        victim = f"127.0.0.1:{base + a.kill_webapp - 1}"
        gw_urls = [u.strip() for u in os.environ.get("CDI_MLSERVE_URL", "").split(",") if u.strip()]
        syn = _synthetic()
        seed = int(time.time()) % 100000  # unique names and scans: no dedupe across runs
        patients = [syn.make_patient(i, seed=seed, docs_min=a.docs_min, docs_max=a.docs_max)
                    for i in range(a.patients)]  # fmt: skip
        user, pw = self.account()
        started = self.event("drill start", patients=a.patients,
                             documents=sum(len(p.documents) for p in patients),
                             victim_instance=victim, health_window_s=window)  # fmt: skip
        report: dict[str, Any] = {"started_at": datetime.fromtimestamp(started, UTC).isoformat()}
        try:
            clients = [self.session(user, pw) for _ in range(min(8, a.patients))]
            prober = threading.Thread(target=self.probing, daemon=True)
            prober.start()
            t0 = time.monotonic()
            step = a.spread_s / max(1, a.patients - 1)
            threads = [threading.Thread(target=self.upload,
                                        args=(clients[i % len(clients)], p, i * step, t0))
                       for i, p in enumerate(patients)]  # fmt: skip
            for t in threads:
                t.start()
            time.sleep(a.kill_webapp_at_s)
            t_web = self.kill(
                f"web app instance web{a.kill_webapp}",
                self._pid(f"webapp-{a.kill_webapp}"),
                upstream=victim,
            )
            t_worker = t_gw = None
            if a.kill_worker:
                time.sleep(max(0.0, a.kill_worker_at_s - a.kill_webapp_at_s))
                t_worker = self.kill(
                    f"job worker worker{a.kill_worker}", self._pid(f"worker-{a.kill_worker}")
                )
            if a.kill_gateway and len(gw_urls) > 1:
                port = int(gw_urls[0].rsplit(":", 1)[1])
                gpid = pid_on_port(port)
                if gpid:
                    t_gw = self.kill("model gateway replica", gpid, url=gw_urls[0])
            for t in threads:
                t.join()
            self.event("uploads done")
            acked = [u for u in self.uploads if u.get("job_id")]
            report["uploads"] = {"sent": len(self.uploads), "acknowledged": len(acked),
                                 "not_acknowledged": [u for u in self.uploads
                                                      if not u.get("job_id")]}  # fmt: skip
            jobs = self.wait_jobs([u["job_id"] for u in acked], a.job_timeout_s)
            self._stop.set()
            prober.join(2)
            report["jobs"] = jobs
            log = (WS / "logs" / "balancer.log").read_text(encoding="utf-8").splitlines()
            out = left_rotation_at(log, victim, t_web)
            report["left_rotation_s"] = round(out - t_web, 3) if out else None
            report["health_window_s"] = window
            failed = [t for t, ok in self.probe if not ok]
            report["probe"] = {
                "requests": len(self.probe),
                "failed": len(failed),
                "failed_after_kill_s": [round(t - t_web, 3) for t in failed],
            }
            report["kills"] = {"webapp": t_web, "worker": t_worker, "gateway": t_gw}
            # bring everything back; the instance must rejoin rotation
            t_restart = self.event("restarting killed processes (start_apptier.sh)")
            env = dict(os.environ, REPO=str(ROOT), WS=str(WS))  # this checkout, wherever it is
            r = subprocess.run(["bash", str(ROOT / "infra/runpod/start_apptier.sh")], env=env,
                               capture_output=True, text=True)  # fmt: skip
            if r.returncode:
                report["restart_error"] = (r.stdout + r.stderr)[-800:]
            back = None
            for _ in range(0 if r.returncode else 30):
                log = (WS / "logs" / "balancer.log").read_text(encoding="utf-8").splitlines()
                back = back_in_rotation_at(log, victim, t_restart)
                if back:
                    break
                time.sleep(1)
            report["back_in_rotation_s"] = round(back - t_restart, 3) if back else None
        finally:
            self.retire(user)
        report["checks"] = checks = self.checks(report)
        report["ok"] = all(c["ok"] for c in checks.values())
        report["events"] = self.events
        self.write(report)
        for name, c in checks.items():
            print(f"{'PASS' if c['ok'] else 'FAIL'}  {name}: {c['detail']}")
        print("DRILL", "PASSED" if report["ok"] else "FAILED")
        return 0 if report["ok"] else 1

    def wait_jobs(self, ids: list[str], timeout: float) -> dict[str, Any]:
        from sqlalchemy import text

        from cdi_adapter.db import session_scope

        deadline = time.monotonic() + timeout
        while True:
            with session_scope() as c:
                open_ = c.execute(
                    text("SELECT count(*) FROM web_job WHERE id = ANY(:i) AND finished_at IS NULL"),
                    {"i": ids},
                ).scalar_one()
            if open_ == 0 or time.monotonic() > deadline:
                break
            time.sleep(2)
        with session_scope() as c:
            rows = c.execute(text("SELECT id, state, attempts, docs, finished_at, created_at, "
                                  "accepted_by FROM web_job WHERE id = ANY(:i)"),
                             {"i": ids}).mappings().all()  # fmt: skip
            doc_ids = [d["document_id"] for r in rows for d in r["docs"] if d.get("document_id")]
            extractions = dict(c.execute(text(
                "SELECT document_id::text, count(*) FROM extraction "
                "WHERE document_id::text = ANY(:d) GROUP BY 1"), {"d": doc_ids}).all())  # fmt: skip
            items = dict(c.execute(text(
                "SELECT source_document_id::text, count(*) FROM rv_item "
                "WHERE source_document_id::text = ANY(:d) "
                "AND status IN ('workbasket', 'claimed') GROUP BY 1"),
                {"d": doc_ids}).all())  # fmt: skip
        out = []
        for r in rows:
            out.append({"job_id": r["id"], "state": r["state"], "attempts": r["attempts"],
                        "accepted_by": r["accepted_by"],
                        "finished": r["finished_at"] is not None,
                        "seconds": (r["finished_at"] - r["created_at"]).total_seconds()
                        if r["finished_at"] else None,
                        "docs": [{"status": d["status"], "error": d.get("error"),
                                  "document_id": d.get("document_id"),
                                  "extractions": extractions.get(d.get("document_id") or "", 0),
                                  "open_review_items": items.get(d.get("document_id") or "", 0)}
                                 for d in r["docs"]]})  # fmt: skip
        missing = sorted(set(ids) - {r["id"] for r in rows})
        return {"total": len(ids), "missing_rows": missing, "list": out}

    @staticmethod
    def checks(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
        jobs = rep.get("jobs", {"list": [], "missing_rows": [], "total": 0})
        lost = [j["job_id"] for j in jobs["list"] if not j["finished"]] + jobs["missing_rows"]
        lost_docs = [
            (j["job_id"], d["status"], d["error"])
            for j in jobs["list"]
            for d in j["docs"]
            if d["status"] in ("pending", "running")
            or (d["error"] or "").startswith(("FileNotFoundError", "gave up"))
        ]
        doubled = [
            (j["job_id"], d["document_id"])
            for j in jobs["list"]
            for d in j["docs"]
            if d["extractions"] > 1 or d["open_review_items"] > 1
        ]
        resumed = [j["job_id"] for j in jobs["list"] if j["attempts"] > 1]
        out_s, window = rep.get("left_rotation_s"), rep.get("health_window_s", 0)
        acked = rep.get("uploads", {}).get("acknowledged", 0)
        return {
            "uploads acknowledged": {
                "ok": acked > 0,
                "detail": f"{acked} of {rep.get('uploads', {}).get('sent', 0)} uploads got 202",
            },
            "zero lost acknowledged uploads": {
                "ok": not lost and not lost_docs,
                "detail": f"{jobs['total']} acknowledged jobs; unfinished {lost}; "
                f"documents left unprocessed {lost_docs}",
            },
            "killed instance left rotation within the health-check window": {
                "ok": out_s is not None and out_s <= window,
                "detail": f"{out_s} s after the kill (window {window} s)",
            },
            "no document processed twice into the results": {
                "ok": not doubled,
                "detail": f"doubled {doubled}",
            },
            "jobs of the killed worker taken over": {
                "ok": True,
                "detail": f"{len(resumed)} resumed: {resumed}",
            },
            "killed instance back in rotation after restart": {
                "ok": rep.get("back_in_rotation_s") is not None,
                "detail": f"{rep.get('back_in_rotation_s')} s after start_apptier.sh",
            },
        }

    @staticmethod
    def write(report: dict[str, Any]) -> None:
        d = WS / "logs" / "ha"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        body = json.dumps(report, indent=2, default=str)
        (d / f"app-drill-{stamp}.json").write_text(body, encoding="utf-8")
        (d / "last-app-drill.json").write_text(body, encoding="utf-8")
        print(f"report: {d / f'app-drill-{stamp}.json'}")


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per probe: too many
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--host", default=f"http://127.0.0.1:{os.environ.get('CDI_WEBAPP_PORT', '8888')}"
    )
    ap.add_argument("--patients", type=int, default=12)
    ap.add_argument("--docs-min", type=int, default=1)
    ap.add_argument("--docs-max", type=int, default=3)
    ap.add_argument("--spread-s", type=float, default=20.0, help="uploads spread over this time")
    ap.add_argument("--kill-webapp", type=int, default=1, help="web app instance to kill (1-based)")
    ap.add_argument("--kill-webapp-at-s", type=float, default=6.0)
    ap.add_argument("--kill-worker", type=int, default=1, help="job worker to kill; 0 = none")
    ap.add_argument("--kill-worker-at-s", type=float, default=14.0)
    ap.add_argument(
        "--kill-gateway",
        action="store_true",
        help="also kill the first model gateway replica of CDI_MLSERVE_URL",
    )
    ap.add_argument("--job-timeout-s", type=float, default=1800.0)
    args = ap.parse_args(argv)
    return Drill(args).run()


if __name__ == "__main__":
    sys.exit(main())
