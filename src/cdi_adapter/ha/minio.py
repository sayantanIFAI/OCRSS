"""Erasure-coded MinIO (backlog E10-S6; ``docs/ha.md``).

Every object is split into data + parity shards across the drives (4 drives: 2 data + 2 parity,
MinIO's default). Losing any one drive loses no data and no writes (write quorum 3 of 4), and a
replacement drive is healed by MinIO itself.

- **Several hosts** (what survives a lost node): one MinIO process per host, each started with the
  same list of every host's drive (``http://<host>:<port>/<path>``) and its own ``--address``.
  MinIO refuses loopback addresses for this inside a container, and §12 keeps every service on
  loopback, so it needs real hosts on a private network (``docs/ha.md``).
- **One host**: one process on 127.0.0.1 with several drive folders. It covers a lost or corrupted
  drive, not a lost host; it is what can be drilled on the pod or Colab.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Cluster:
    """``addresses``: one ``host:port`` per MinIO process (one per host). ``drives``: every drive of
    the erasure set, local folders (one host) or ``http://host:port/path`` (several hosts)."""

    binary: Path
    addresses: tuple[str, ...]
    drives: tuple[str, ...]
    run_dir: Path  # pid files

    @classmethod
    def one_host(cls, binary: Path, base: Path, port: int = 9000, drives: int = 4) -> Cluster:
        folders = tuple((base / f"d{i}").as_posix() for i in range(1, drives + 1))
        return cls(binary, (f"127.0.0.1:{port}",), folders, base)

    def url(self, i: int = 1) -> str:
        return f"http://{self.addresses[i - 1]}"

    def urls(self) -> list[str]:
        return [self.url(i) for i in self.nodes()]

    def nodes(self) -> range:
        return range(1, len(self.addresses) + 1)

    def command(self, i: int = 1) -> list[str]:
        host, port = self.addresses[i - 1].rsplit(":", 1)
        return [
            str(self.binary),
            "server",
            "--address",
            f"{host}:{port}",
            "--console-address",
            f"{host}:{int(port) + 1000}",
            *self.drives,
        ]

    def local_drives(self) -> list[Path]:
        return [Path(d) for d in self.drives if "://" not in d]

    def pidfile(self, i: int = 1) -> Path:
        return self.run_dir / f"minio-{i}.pid"


def node_env(access_key: str, secret_key: str, *, root_disk_ok: bool = False) -> dict[str, str]:
    """Root credentials from the secrets store, in the environment only (E1-S5). ``root_disk_ok``:
    MinIO refuses erasure drives on the machine's root disk; a drill on a single-disk host (Colab)
    lifts that with MinIO's own threshold setting. Not needed on the pod (/workspace is MooseFS)."""
    env = {
        **os.environ,
        "MINIO_ROOT_USER": access_key,
        "MINIO_ROOT_PASSWORD": secret_key,
        "MINIO_BROWSER": "off",
        # its /minio/v2/metrics/cluster (drives online/offline) without a token: it listens on
        # loopback only (§12), and the web app's collector reads it (E10-S6)
        "MINIO_PROMETHEUS_AUTH_TYPE": "public",
    }
    if root_disk_ok:
        env["MINIO_ROOTDISK_THRESHOLD_SIZE"] = "1KiB"  # only a drive this small counts as "root"
    return env


def start_node(c: Cluster, i: int, env: dict[str, str], logs: Path) -> int:
    for d in c.local_drives():
        d.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    c.run_dir.mkdir(parents=True, exist_ok=True)
    out = open(logs / f"minio-{i}.log", "ab")  # noqa: SIM115 - handed to the child
    p = subprocess.Popen(
        c.command(i),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        start_new_session=True,
    )
    c.pidfile(i).write_text(str(p.pid), encoding="utf-8")
    return p.pid


def stop_node(c: Cluster, i: int, *, hard: bool = False) -> bool:
    """``hard``: SIGKILL, a process that just dies; otherwise a clean stop."""
    try:
        pid = int(c.pidfile(i).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # SIGKILL does not exist on Windows (local tests); there a hard stop is TerminateProcess
    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if hard else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    for _ in range(100):
        if not node_live(c.url(i)):
            break
        time.sleep(0.1)
    c.pidfile(i).unlink(missing_ok=True)
    return True


def _ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def node_live(url: str) -> bool:
    return _ok(f"{url}/minio/health/live")


def cluster_writable(url: str) -> bool:
    """200 only while the erasure set has write quorum (MinIO's own cluster health check)."""
    return _ok(f"{url}/minio/health/cluster")


def wait(pred: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False


def start(
    c: Cluster, env: dict[str, str], logs: Path, *, node: int | None = None, timeout: float = 120
) -> None:
    """Start this host's process (``node``; every process when all run here), then wait for
    write quorum."""
    for i in [node] if node else list(c.nodes()):
        if not node_live(c.url(i)):
            start_node(c, i, env, logs)
    if not wait(lambda: any(cluster_writable(u) for u in c.urls()), timeout):
        raise RuntimeError(f"MinIO did not reach write quorum; see {logs}/minio-*.log")


def online(urls: list[str]) -> int:
    return sum(node_live(u) for u in urls)


def copy_bucket(src: Any, dst: Any) -> dict[str, int]:
    """Copy every object of ``src``'s bucket to ``dst`` (two ``ObjectStore``s) and read each copy
    back: the move from the single-drive store to the erasure set, verified byte for byte."""
    dst.ensure_bucket()
    copied = skipped = 0
    for page in src.client.get_paginator("list_objects_v2").paginate(Bucket=src.bucket):
        for o in page.get("Contents", []):
            key = o["Key"]
            body = src.get_bytes(key)
            if dst.exists(key) and dst.get_bytes(key) == body:
                skipped += 1
                continue
            head = src.client.head_object(Bucket=src.bucket, Key=key)
            dst.put_bytes(key, body, head.get("ContentType") or "application/octet-stream")
            if hashlib.sha256(dst.get_bytes(key)).digest() != hashlib.sha256(body).digest():
                raise RuntimeError(f"copy of {key} does not read back identical")
            copied += 1
    return {"copied": copied, "already_there": skipped}
