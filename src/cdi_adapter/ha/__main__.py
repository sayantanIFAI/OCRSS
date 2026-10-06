"""``python -m cdi_adapter.ha``: data-tier HA commands (backlog E10-S6; runbook ``docs/ha.md``).

    pg setup-standby            replication role + slot on the primary; build, start the standby
    pg status                   role, streaming state and lag of both servers
    pg failover [--to standby]  fence the old primary, promote the standby
    pg rejoin                   the fenced old primary returns as a standby of the new one
    pg drill                    failover drill on throwaway clusters: measured RPO and RTO

    minio start | status        the erasure set (this host's process, then write quorum)
    minio stop-node N | start-node N
    minio migrate --from URL    copy the single-drive store into the erasure set, verified
    minio drill                 drive-loss drill on a throwaway erasure set (one host)

Paths and ports come from the environment (``.env`` on the pod): ``CDI_PG_BIN``, ``PGDATA``,
``CDI_PG_STANDBY_PORT``, ``CDI_PG_STANDBY_DATA``, ``CDI_MINIO_BIN``, ``CDI_MINIO_MODE``
(``erasure``: one host, ``CDI_MINIO_DRIVES`` folders under ``CDI_MINIO_DIR``; ``cluster``: one
process per host of ``CDI_MINIO_ADDRESSES``, this one ``CDI_MINIO_NODE``). Passwords come from the
secrets store only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cdi_adapter.ha import minio as mc
from cdi_adapter.ha import pg as ha

WS = Path(os.environ.get("WS", "/workspace"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def pg_nodes() -> tuple[ha.Node, ha.Node]:
    data = Path(_env("PGDATA", "/var/lib/postgresql/16/cdi"))
    primary = ha.Node("primary", int(_env("CDI_PG_PRIMARY_PORT", "5432")), data)
    standby_data = Path(_env("CDI_PG_STANDBY_DATA", str(data.with_name(data.name + "-standby"))))
    standby = ha.Node("standby", int(_env("CDI_PG_STANDBY_PORT", "5433")), standby_data)
    return primary, standby


def pg_tools() -> ha.Pg:
    # the pod's local superuser over loopback (initdb's local trust); never a password here
    return ha.Pg.for_host(_env("CDI_PG_BIN", "/usr/lib/postgresql/16/bin"))


def _secret(name: str) -> str:
    from cdi_adapter import secretstore

    store = secretstore.active_dir()
    value = secretstore.read(store, name) if store else None
    if not value:
        raise ha.HaError(f"{name} is not in the secrets store ($CDI_SECRETS_DIR); run its init")
    return value


def cluster() -> mc.Cluster:
    binary = Path(_env("CDI_MINIO_BIN", str(WS / "tools" / "minio")))
    base = Path(_env("CDI_MINIO_DIR", str(WS / "minio-erasure")))
    if _env("CDI_MINIO_MODE", "single") == "cluster":
        # several hosts: every host's drive, each process with the same list (docs/ha.md)
        addrs = tuple(a.strip() for a in _env("CDI_MINIO_ADDRESSES", "").split(",") if a.strip())
        if len(addrs) < 4:
            raise ha.HaError("CDI_MINIO_ADDRESSES must list at least 4 hosts (host:port)")
        drives = tuple(f"http://{a}{base.as_posix()}" for a in addrs)
        return mc.Cluster(binary, addrs, drives, base)
    port = int(_env("CDI_MINIO_ADDRESSES", "127.0.0.1:9000").rsplit(":", 1)[1])
    return mc.Cluster.one_host(binary, base, port=port, drives=int(_env("CDI_MINIO_DRIVES", "4")))


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.ha",
                                 description="Postgres standby + MinIO cluster")  # fmt: skip
    top = ap.add_subparsers(dest="area", required=True)
    p = top.add_parser("pg").add_subparsers(dest="cmd", required=True)
    p.add_parser("setup-standby")
    p.add_parser("status")
    f = p.add_parser("failover")
    f.add_argument("--to", choices=("standby", "primary"), default="standby",
                   help="the server to promote (the other one is fenced)")  # fmt: skip
    r = p.add_parser("rejoin")
    r.add_argument("--old", choices=("standby", "primary"), default="primary",
                   help="the fenced server that comes back as a standby")  # fmt: skip
    d = p.add_parser("drill")
    d.add_argument("--bindir", default=_env("CDI_PG_BIN", "/usr/lib/postgresql/16/bin"))
    d.add_argument("--seconds", type=float, default=6.0)
    m = top.add_parser("minio").add_subparsers(dest="cmd", required=True)
    m.add_parser("start")
    m.add_parser("status")
    for name in ("stop-node", "start-node"):
        m.add_parser(name).add_argument("node", type=int)
    g = m.add_parser("migrate")
    g.add_argument("--from", dest="src", required=True, help="the single-node MinIO URL")
    md = m.add_parser("drill")
    md.add_argument("--binary", default=_env("CDI_MINIO_BIN", str(WS / "tools" / "minio")))
    return ap


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the servers
    args = build_parser().parse_args(argv)
    try:
        if args.area == "pg":
            return _pg(args)
        return _minio(args)
    except (ha.HaError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


def _pg(args: argparse.Namespace) -> int:  # pragma: no cover - needs the servers
    if args.cmd == "drill":
        from cdi_adapter.ha.pg_drill import run_drill

        out = run_drill(args.bindir, seconds=args.seconds)
        _print(out)
        print("PG FAILOVER DRILL:", "PASS" if out["pass"] else "FAIL")
        return 0 if out["pass"] else 1
    pg = pg_tools()
    primary, standby = pg_nodes()
    logs = WS / "logs"
    # read by the standby server itself (primary_conninfo passfile=), so it lives with the data
    # folders, owned by postgres and 0600 (the secrets folder is root-only)
    passfile = standby.pgdata.parent / ".pgpass-replication"
    if args.cmd == "setup-standby":
        pw = _secret("cdi_replication_password")
        ha.prepare_primary(pg, primary, repl_password=pw, monitor_role="cdi")
        ha.write_pgpass(passfile, [(primary.host, primary.port, ha.REPL_USER, pw),
                                   (standby.host, standby.port, ha.REPL_USER, pw)], pg)  # fmt: skip
        # already built (a password rotation): the new password and file are all it needs; the
        # standby's next reconnect reads them
        if not (standby.pgdata / "PG_VERSION").exists():
            ha.init_standby(pg, primary, standby, passfile=passfile,
                            log=logs / "postgres-standby.log", socket_dir="/tmp")  # fmt: skip
        _print(ha.status(pg, standby))
    elif args.cmd == "status":
        _print([ha.status(pg, n) for n in (primary, standby)])
    elif args.cmd == "failover":
        new, old = (standby, primary) if args.to == "standby" else (primary, standby)
        _print(ha.failover(pg, old, new))
    elif args.cmd == "rejoin":
        old, new = (primary, standby) if args.old == "primary" else (standby, primary)
        aside = ha.rejoin(pg, old, new, passfile=passfile, log=logs / f"postgres-{old.name}.log",
                          socket_dir="/tmp")  # fmt: skip
        _print({"rejoined": old.name, "old_data_kept_at": str(aside), **ha.status(pg, old)})
    return 0


def _minio(args: argparse.Namespace) -> int:  # pragma: no cover - needs the servers
    if args.cmd == "drill":
        from cdi_adapter.ha.minio_drill import run_drill

        out = run_drill(args.binary)
        _print(out)
        print("MINIO DRIVE-LOSS DRILL:", "PASS" if out["pass"] else "FAIL")
        return 0 if out["pass"] else 1
    c = cluster()
    env = mc.node_env(_secret("cdi_s3_access_key"), _secret("cdi_s3_secret_key"))
    logs = WS / "logs"
    if args.cmd == "start":
        mc.start(c, env, logs, node=int(_env("CDI_MINIO_NODE", "0")) or None)
    elif args.cmd == "stop-node":
        mc.stop_node(c, args.node, hard=False)
    elif args.cmd == "start-node":
        mc.start_node(c, args.node, env, logs)
    elif args.cmd == "migrate":
        from cdi_adapter.config import get_settings
        from cdi_adapter.storage import ObjectStore

        s = get_settings()
        src = ObjectStore(s.model_copy(update={"s3_endpoint_url": args.src}))
        dst = ObjectStore(s.model_copy(update={"s3_endpoint_url": ",".join(c.urls())}))
        _print(mc.copy_bucket(src, dst))
    _print({"processes": len(c.addresses), "drives": len(c.drives), "online": mc.online(c.urls()),
            "writable": mc.cluster_writable(c.url(1)) or any(
                mc.cluster_writable(u) for u in c.urls()[1:])})  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
