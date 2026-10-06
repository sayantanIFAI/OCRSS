"""Postgres primary + hot standby by streaming replication (backlog E10-S6; ``docs/ha.md``).

- The standby is a ``pg_basebackup -R`` copy that streams WAL from the primary through a physical
  replication slot, so the primary keeps every segment the standby has not received yet, even
  while the standby is away. Asynchronous: a commit does not wait for the standby, and the lag is
  normally well under a second (the RPO the failover drill measures; the alert fires at 30 s).
- Failover: **fence** the old primary first (stop it and leave ``cdi.fenced`` in its data folder,
  which ``bootstrap_pod.sh`` refuses to start), then promote the standby. Two writable primaries
  are never possible through these commands.
- The app needs no change at failover: ``CDI_DATABASE_URL`` lists both servers with
  ``target_session_attrs=read-write``, and new connections find the writable one.
- Rejoin: the old primary comes back as a standby of the new one, from a fresh base backup (its
  old data folder is kept aside, never reused as a primary).

When this runs as root (the pod, Colab), the Postgres tools run as the ``postgres`` OS user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

FENCE = "cdi.fenced"
SLOT = "cdi_standby"
REPL_USER = "cdi_repl"


class HaError(Exception):
    """A step that cannot be carried out safely; nothing further was changed."""


@dataclass(frozen=True)
class Node:
    name: str
    port: int
    pgdata: Path
    host: str = "127.0.0.1"


class Pg:
    """The Postgres tools of one installation, and admin SQL over TCP."""

    def __init__(
        self,
        bindir: Path | str,
        *,
        os_user: str | None = None,
        admin_user: str = "postgres",
        admin_password: str | None = None,
        dbname: str = "postgres",
    ) -> None:
        self.bindir = Path(bindir)
        self.os_user = os_user
        self.admin_user, self.admin_password, self.dbname = admin_user, admin_password, dbname

    @classmethod
    def for_host(cls, bindir: Path | str, **kw: Any) -> Pg:
        """As root on Linux the tools must run as ``postgres`` (they refuse root)."""
        root = sys.platform != "win32" and os.geteuid() == 0
        return cls(bindir, os_user="postgres" if root else None, **kw)

    def tool(self, name: str, *args: str) -> list[str]:
        exe = self.bindir / (name + (".exe" if sys.platform == "win32" else ""))
        cmd = [str(exe), *args]
        return ["runuser", "-u", self.os_user, "--", *cmd] if self.os_user else cmd

    def run(
        self,
        name: str,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        detach: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # a started server inherits our pipes: never capture them for `pg_ctl start`, or the
        # caller waits for an EOF that comes only when the server stops
        out = subprocess.DEVNULL if detach else subprocess.PIPE
        r = subprocess.run(
            self.tool(name, *args),
            env={**os.environ, **(env or {})},
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            timeout=600,
        )
        if check and r.returncode != 0:
            raise HaError(
                f"{name} {' '.join(args[:3])}... failed: {(r.stderr or '').strip()[-400:]}"
            )
        return r

    def conninfo(
        self,
        node: Node,
        *,
        dbname: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: int = 3,
    ) -> str:
        return psycopg.conninfo.make_conninfo(
            host=node.host,
            port=node.port,
            dbname=dbname or self.dbname,
            user=user or self.admin_user,
            password=password if password is not None else (self.admin_password or ""),
            connect_timeout=timeout,
        )

    def sql(
        self, node: Node, query: str | sql.Composed, params: tuple[Any, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.conninfo(node), autocommit=True) as c:
            cur = c.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    def is_up(self, node: Node) -> bool:
        try:
            self.sql(node, "SELECT 1")
        except psycopg.Error:
            return False
        return True

    def owned(self, path: Path) -> None:
        """Hand a file or folder to the OS user the server runs as (root on the pod)."""
        if self.os_user:
            shutil.chown(path, self.os_user, self.os_user)

    def start(self, node: Node, log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        self.owned(log)
        self.run(
            "pg_ctl",
            "-D",
            str(node.pgdata),
            "-l",
            str(log),
            "-w",
            "-t",
            "120",
            "start",
            detach=True,
        )
        if not self.is_up(node):
            raise HaError(f"{node.name} did not start on port {node.port}; see {log}")

    def stop(self, node: Node, mode: str = "fast") -> None:
        self.run("pg_ctl", "-D", str(node.pgdata), "-m", mode, "-w", "stop", check=False)


# ------------------------------------------------------------------ setup


def write_pgpass(path: Path, lines: list[tuple[str, int, str, str]], pg: Pg) -> Path:
    """A libpq password file (0600): the replication password never goes on a command line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    esc = [
        ":".join(str(f).replace("\\", "\\\\").replace(":", "\\:") for f in (h, p, "*", u, pw))
        for h, p, u, pw in lines
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(esc) + "\n")
    pg.owned(path)
    return path


def prepare_primary(
    pg: Pg,
    primary: Node,
    *,
    repl_password: str,
    slot: str = SLOT,
    repl_user: str = REPL_USER,
    monitor_role: str | None = None,
) -> None:
    """The replication role (password from the secrets store) and the standby's slot. Idempotent;
    nothing here needs a restart (PG16 defaults: wal_level=replica, 10 WAL senders and slots)."""
    level = pg.sql(primary, "SHOW wal_level")[0][0]
    if level not in ("replica", "logical"):
        raise HaError(f"{primary.name}: wal_level={level}; set replica and restart first")
    exists = pg.sql(primary, "SELECT 1 FROM pg_roles WHERE rolname = %s", (repl_user,))
    verb = "ALTER" if exists else "CREATE"
    pg.sql(
        primary,
        sql.SQL(verb + " ROLE {} WITH REPLICATION LOGIN PASSWORD {}").format(
            sql.Identifier(repl_user), sql.Literal(repl_password)
        ),
    )
    ensure_slot(pg, primary, slot)
    if monitor_role:  # the web app reads pg_stat_replication for its metrics
        pg.sql(primary, sql.SQL("GRANT pg_monitor TO {}").format(sql.Identifier(monitor_role)))


def ensure_slot(pg: Pg, node: Node, slot: str = SLOT) -> None:
    if not pg.sql(node, "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (slot,)):
        pg.sql(node, "SELECT pg_create_physical_replication_slot(%s)", (slot,))


def init_standby(
    pg: Pg,
    primary: Node,
    standby: Node,
    *,
    passfile: Path,
    log: Path,
    slot: str = SLOT,
    repl_user: str = REPL_USER,
    socket_dir: str = "",
) -> None:
    """``pg_basebackup -R`` from the primary into an empty folder, then start it streaming."""
    if standby.pgdata.exists() and any(standby.pgdata.iterdir()):
        raise HaError(
            f"{standby.pgdata} is not empty: a standby is only built into an empty folder"
        )
    standby.pgdata.parent.mkdir(parents=True, exist_ok=True)
    pg.run(
        "pg_basebackup",
        "-h",
        primary.host,
        "-p",
        str(primary.port),
        "-U",
        repl_user,
        "-D",
        str(standby.pgdata),
        "-X",
        "stream",
        "-R",
        "-S",
        slot,
        "-c",
        "fast",
        "-w",
        env={"PGPASSFILE": str(passfile)},
    )
    # the copied postgresql.conf is the primary's: this server's own port and address win (last
    # setting in the file); -R wrote primary_conninfo (with passfile=) and standby.signal
    conf = standby.pgdata / "postgresql.conf"
    with conf.open("a", encoding="utf-8") as f:
        f.write(
            f"\n# E10-S6 standby\nport = {standby.port}\nlisten_addresses = '{standby.host}'\n"
            f"unix_socket_directories = '{socket_dir}'\nhot_standby = on\n"
        )
    pg.start(standby, log)


def init_cluster(pg: Pg, node: Node, *, superuser: str, password: str, log: Path) -> None:
    """A fresh, empty cluster in ``node.pgdata`` (scram auth, loopback, no unix socket), started.
    For drills and restores: never the pod's own cluster."""
    pwfile = node.pgdata.parent / f".{node.name}.initpw"
    node.pgdata.parent.mkdir(parents=True, exist_ok=True)
    pg.owned(node.pgdata.parent)
    fd = os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password)
    pg.owned(pwfile)
    try:
        pg.run("initdb", "-D", str(node.pgdata), "-U", superuser, f"--pwfile={pwfile}",
               "-A", "scram-sha-256", "-E", "UTF8", "--locale=C")  # fmt: skip
    finally:
        pwfile.unlink(missing_ok=True)
    with (node.pgdata / "postgresql.conf").open("a", encoding="utf-8") as f:
        conf = [f"port = {node.port}", f"listen_addresses = '{node.host}'",
                "unix_socket_directories = ''"]  # fmt: skip
        f.write("\n" + "\n".join(conf) + "\n")
    pg.start(node, log)


# ------------------------------------------------------------------ status


def status(pg: Pg, node: Node) -> dict[str, Any]:
    """Role, and replication health from whichever side this is."""
    if not pg.is_up(node):
        return {"node": node.name, "up": False}
    standby = bool(pg.sql(node, "SELECT pg_is_in_recovery()")[0][0])
    out: dict[str, Any] = {
        "node": node.name,
        "up": True,
        "role": "standby" if standby else "primary",
        "fenced": (node.pgdata / FENCE).exists(),
    }
    if standby:
        lag, recv, replay = pg.sql(
            node,
            "SELECT extract(epoch FROM now() - pg_last_xact_replay_timestamp()), "
            "pg_last_wal_receive_lsn()::text, pg_last_wal_replay_lsn()::text",
        )[0]
        rx = pg.sql(node, "SELECT status FROM pg_stat_wal_receiver")
        out |= {
            "receiving": bool(rx and rx[0][0] == "streaming"),
            "seconds_since_last_replayed_commit": float(lag) if lag is not None else None,
            "receive_lsn": recv,
            "replay_lsn": replay,
        }
    else:
        rows = pg.sql(
            node,
            "SELECT application_name, state, extract(epoch FROM coalesce(replay_lag, '0'))::float, "
            "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint FROM pg_stat_replication",
        )
        slots = pg.sql(
            node, "SELECT slot_name, active FROM pg_replication_slots WHERE slot_type = 'physical'"
        )
        out |= {
            "standbys": [
                {"name": a, "state": s, "replay_lag_s": lag, "bytes_behind": b}
                for a, s, lag, b in rows
            ],
            "slots": {n: bool(act) for n, act in slots},
        }
    return out


# ------------------------------------------------------------------ failover


def fence(pg: Pg, node: Node) -> bool:
    """Mark the data folder so it is never started as a primary again, and stop the server.
    Returns whether the folder was reachable (a dead host cannot be fenced from here: then the
    host must not be restarted before `rejoin`; docs/ha.md)."""
    try:
        (node.pgdata / FENCE).write_text(
            f"fenced {time.strftime('%Y-%m-%dT%H:%M:%S')}\n", encoding="utf-8"
        )
    except OSError:
        return False
    pg.stop(node, "fast")
    return True


def promote(pg: Pg, standby: Node, timeout: int = 60) -> None:
    if not pg.sql(standby, "SELECT pg_is_in_recovery()")[0][0]:
        raise HaError(f"{standby.name} is not a standby")
    if not pg.sql(standby, "SELECT pg_promote(true, %s)", (timeout,))[0][0]:
        raise HaError(f"{standby.name} did not finish promotion within {timeout} s")


def failover(
    pg: Pg, primary: Node, standby: Node, *, slot: str = SLOT, clock: Any = time.perf_counter
) -> dict[str, Any]:
    """Fence the old primary, promote the standby, give it a slot for the old one's rejoin."""
    if not pg.is_up(standby):
        raise HaError(f"{standby.name} is not running: nothing to fail over to")
    t0 = clock()
    fenced = fence(pg, primary)
    promote(pg, standby)
    ensure_slot(pg, standby, slot)
    return {
        "new_primary": standby.name,
        "old_primary_fenced": fenced,
        "seconds": round(clock() - t0, 2),
    }


def rejoin(
    pg: Pg,
    old: Node,
    new_primary: Node,
    *,
    passfile: Path,
    log: Path,
    slot: str = SLOT,
    socket_dir: str = "",
) -> Path:
    """The fenced old primary returns as a standby of the new one. Its old folder is kept aside."""
    if pg.is_up(old) and not pg.sql(old, "SELECT pg_is_in_recovery()")[0][0]:
        pg.stop(old, "fast")
    aside = old.pgdata.with_name(f"{old.pgdata.name}.old-{time.strftime('%Y%m%dT%H%M%S')}")
    if old.pgdata.exists():
        old.pgdata.rename(aside)
    ensure_slot(pg, new_primary, slot)
    init_standby(pg, new_primary, old, passfile=passfile, log=log, slot=slot, socket_dir=socket_dir)
    return aside
