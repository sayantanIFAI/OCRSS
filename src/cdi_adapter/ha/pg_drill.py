"""Postgres failover drill with a measured RPO and RTO (backlog E10-S6 "Tests: failover drill").

On two throwaway clusters (never the app's database): a writer commits a numbered row every
``1/rate`` s through the same multi-host URL form the app uses; the primary is **crashed**
(``pg_ctl stop -m immediate``, no clean shutdown), the standby is promoted with ``failover``, and
the writer carries on by itself. Then:

- **RPO**: acknowledged commits missing on the new primary, and how much time they span
- **RTO**: from the crash to the writer's first successful commit on the new primary
- the old primary is fenced, rejoins as a standby, and receives new rows

    python -m cdi_adapter.ha pg drill --bindir /usr/lib/postgresql/16/bin
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from cdi_adapter.config import Settings
from cdi_adapter.db import engine_url
from cdi_adapter.ha import pg as ha

RPO_TARGET_S = 60.0  # the block's promise ("RPO < 1 minute")


class Writer(threading.Thread):
    def __init__(self, url: URL, rate_hz: float) -> None:
        super().__init__(daemon=True)
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        self.period = 1.0 / rate_hz
        self.acked: dict[int, float] = {}
        self.errors = 0
        self.stop = threading.Event()

    def run(self) -> None:
        seq = 0
        while not self.stop.is_set():
            seq += 1
            try:
                with self.engine.begin() as c:
                    c.execute(text("INSERT INTO drill (seq) VALUES (:s)"), {"s": seq})
                self.acked[seq] = time.monotonic()  # acknowledged: the commit returned
            except Exception:  # noqa: BLE001 - the primary is down: keep trying, like the app
                self.errors += 1
                self.engine.dispose()
            time.sleep(self.period)
        self.engine.dispose()


def _wait(pred: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.2)
    raise ha.HaError(f"timed out waiting for {what}")


def run_drill(
    bindir: Path | str,
    *,
    ports: tuple[int, int] = (55431, 55432),
    seconds: float = 6.0,
    rate_hz: float = 25.0,
    keep: bool = False,
    workdir: Path | None = None,
) -> dict[str, Any]:
    pw = secrets.token_urlsafe(18)
    work = workdir or Path(tempfile.mkdtemp(prefix="cdi-pg-drill-"))
    pg = ha.Pg.for_host(bindir, admin_user="cdi", admin_password=pw)
    pg.owned(work)
    a, b = ha.Node("pg-a", ports[0], work / "a"), ha.Node("pg-b", ports[1], work / "b")
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    pg.owned(logs)
    out: dict[str, Any] = {}
    writer: Writer | None = None
    try:
        ha.init_cluster(pg, a, superuser="cdi", password=pw, log=logs / "a.log")
        pg.sql(a, "CREATE DATABASE drill")
        drill_db = ha.Pg(
            bindir, os_user=pg.os_user, admin_user="cdi", admin_password=pw, dbname="drill"
        )
        drill_db.sql(a, "CREATE TABLE drill (seq int PRIMARY KEY, at timestamptz DEFAULT now())")
        repl_pw = secrets.token_urlsafe(18)
        ha.prepare_primary(pg, a, repl_password=repl_pw, monitor_role="cdi")
        passfile = ha.write_pgpass(
            work / "pgpass", [("127.0.0.1", p, ha.REPL_USER, repl_pw) for p in ports], pg
        )
        ha.init_standby(pg, a, b, passfile=passfile, log=logs / "b.log")
        _wait(lambda: ha.status(pg, b).get("receiving"), 60, "the standby to stream")

        # exactly the app's own connection: CDI_DATABASE_URL + CDI_DATABASE_HOSTS -> engine_url()
        url = engine_url(
            Settings(
                database_url=f"postgresql+psycopg://cdi:{pw}@127.0.0.1:{a.port}/drill",
                database_hosts=f"127.0.0.1:{a.port},127.0.0.1:{b.port}",
            )
        )
        writer = Writer(url, rate_hz)
        writer.start()
        time.sleep(seconds)
        before = ha.status(pg, a)
        out["lag_before_crash"] = before.get("standbys")

        pg.stop(a, "immediate")  # a crash: no shutdown checkpoint, nothing flushed on purpose
        # taken once the primary is surely dead: every commit acknowledged by it is counted
        crash_at = time.monotonic()
        acked_before = dict(writer.acked)
        out["failover"] = ha.failover(pg, a, b)
        _wait(
            lambda: any(t > crash_at for t in writer.acked.values()),
            60,
            "the writer to commit on the new primary",
        )
        resumed = min(t for t in writer.acked.values() if t > crash_at)
        time.sleep(1.0)
        writer.stop.set()
        writer.join(10)

        present = {r[0] for r in drill_db.sql(b, "SELECT seq FROM drill")}
        lost = sorted(s for s in acked_before if s not in present)
        last_kept = max((acked_before[s] for s in acked_before if s in present), default=crash_at)
        rpo = max((acked_before[s] - last_kept for s in lost), default=0.0)
        out |= {
            "acked_before_crash": len(acked_before),
            "lost_acked_commits": len(lost),
            "rpo_seconds": round(rpo, 3),
            "rto_seconds": round(resumed - crash_at, 2),
            "writer_errors_during_failover": writer.errors,
            "fence_file": (a.pgdata / ha.FENCE).exists(),
        }

        # the old primary returns as a standby of the new one, and receives new rows
        aside = ha.rejoin(pg, a, b, passfile=passfile, log=logs / "a2.log")
        _wait(lambda: ha.status(pg, a).get("receiving"), 60, "the rejoined node to stream")
        drill_db.sql(b, "INSERT INTO drill (seq) VALUES (-1)")
        _wait(
            lambda: bool(drill_db.sql(a, "SELECT 1 FROM drill WHERE seq = -1")),
            30,
            "a new row on the rejoined standby",
        )
        out |= {
            "rejoined_as": ha.status(pg, a)["role"],
            "old_data_kept_at": aside.name,
            "old_data_fenced": (aside / ha.FENCE).exists(),
        }

        checks = {
            f"RPO under {RPO_TARGET_S:.0f} s": out["rpo_seconds"] < RPO_TARGET_S,
            "the new primary is writable and the app URL found it": out["rto_seconds"] < 60,
            "the old primary was fenced before promotion": out["fence_file"],
            "the old primary rejoined as a standby and replicates": out["rejoined_as"] == "standby",
        }
        out["checks"] = checks
        out["pass"] = all(checks.values())
        return out
    finally:
        if writer is not None:
            writer.stop.set()
        for n in (a, b):
            pg.stop(n, "immediate")
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
