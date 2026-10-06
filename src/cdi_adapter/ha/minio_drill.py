"""MinIO drive-loss drill on one host (backlog E10-S6 "Tests: node-loss drill", the part one host
can prove; the node-loss drill itself needs MinIO on separate hosts, ``docs/ha.md``).

On a throwaway erasure set (one MinIO process on 127.0.0.1, 4 drive folders; never the app's
store), through the app's own ``ObjectStore``:

1. write objects of mixed sizes (1 KB to 1.5 MB, like pages and scans)
2. **lose drive 1** while MinIO runs (its folder disappears)
3. every object reads back byte-identical, the set is still writable, new objects are written
4. an empty replacement drive goes in, and MinIO heals it: it holds every object's shard again
5. no object was lost

    python -m cdi_adapter.ha minio drill --binary /workspace/tools/minio-<tag>
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from cdi_adapter.config import Settings
from cdi_adapter.ha import minio as mc
from cdi_adapter.storage import ObjectStore


def healed(drive: Path, bucket: str, keys: list[str]) -> int:
    """How many of ``keys`` the drive holds again (MinIO keeps an ``xl.meta`` per object)."""
    return sum((drive / bucket / k / "xl.meta").exists() for k in keys)


def drive_counts(url: str) -> dict[str, float]:
    """MinIO's own drive gauges (``/minio/v2/metrics/cluster``), as the collector reads them."""
    import httpx

    text = httpx.get(f"{url}/minio/v2/metrics/cluster", timeout=5).text
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("minio_cluster_drive_") and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name.split("{", 1)[0]] = float(value)
    return out


def run_drill(
    binary: Path | str,
    *,
    port: int = 9321,
    objects: int = 30,
    keep: bool = False,
    heal_timeout: float = 300,
    root_disk_ok: bool = True,
) -> dict[str, Any]:
    base = Path(tempfile.mkdtemp(prefix="cdi-minio-drill-"))
    c = mc.Cluster.one_host(Path(binary), base, port=port)
    access, secret = "drill" + secrets.token_hex(4), secrets.token_urlsafe(24)
    env = mc.node_env(access, secret, root_disk_ok=root_disk_ok)
    logs = base / "logs"
    lost = c.local_drives()[0]
    out: dict[str, Any] = {"drives": len(c.drives)}
    try:
        mc.start(c, env, logs)
        store = ObjectStore(
            Settings(
                s3_endpoint_url=c.url(),
                s3_access_key=access,
                s3_secret_key=secret,
                s3_bucket="drill",
            )
        )
        store.ensure_bucket()
        sums: dict[str, str] = {}

        def put(prefix: str, n: int) -> list[str]:
            keys = []
            for k in range(n):
                key = f"{prefix}/{k:03d}.bin"
                data = secrets.token_bytes(1024 * (1 + (k * 37) % 1500))
                store.put_bytes(key, data, "application/octet-stream")
                sums[key] = hashlib.sha256(data).hexdigest()
                keys.append(key)
            return keys

        def intact(keys: list[str]) -> int:
            return sum(hashlib.sha256(store.get_bytes(k)).hexdigest() == sums[k] for k in keys)

        before = put("before", objects)
        out["on_drive_1_before"] = healed(lost, "drill", before)
        shutil.rmtree(lost)  # the drive is gone, with everything on it
        time.sleep(3)
        out["drives_reported"] = drive_counts(c.url())
        out["read_with_drive_1_lost"] = f"{intact(before)}/{len(before)}"
        out["writable_with_drive_1_lost"] = mc.cluster_writable(c.url())
        during = put("during", max(5, objects // 3))
        out["written_with_drive_1_lost"] = len(during)

        lost.mkdir()  # an empty replacement drive
        everything = before + during
        t0 = time.monotonic()
        ok = mc.wait(lambda: healed(lost, "drill", everything) == len(everything), heal_timeout)
        out["replacement_healed"] = f"{healed(lost, 'drill', everything)}/{len(everything)}"
        out["heal_s"] = round(time.monotonic() - t0, 1)
        out["intact_at_end"] = f"{intact(everything)}/{len(everything)}"
        n, m = len(before), len(everything)
        checks = {
            "every object reads back identical with a drive lost": out["read_with_drive_1_lost"]
            == f"{n}/{n}",
            "writes continue with a drive lost": out["writable_with_drive_1_lost"]
            and out["written_with_drive_1_lost"] > 0,
            "the replacement drive was healed with every object": ok,
            "no object lost": out["intact_at_end"] == f"{m}/{m}",
        }
        out["checks"] = checks
        out["pass"] = all(checks.values())
        return out
    finally:
        mc.stop_node(c, 1)
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
