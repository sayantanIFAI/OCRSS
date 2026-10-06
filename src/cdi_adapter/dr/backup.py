"""Backups of the data tier (backlog E10-S11; ``docs/dr.md``).

A backup folder holds, next to the ``pg_dump`` files that ``snapshot.sh`` writes:

    objects/<key>          every MinIO object, as a plain file (the key is the relative path)
    objects.json           manifest: key -> sha256, size, content type
    last_backup.json       the status the monitoring and the restore drill read

Objects are only ever added (a key's bytes never change: keys are content addresses, §5 S1), so
each run copies just the new ones. Every copy is read back and its sha256 checked before it is
entered in the manifest.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST = "objects.json"
STATUS = "last_backup.json"
OBJECTS = "objects"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def object_path(dest: Path, key: str) -> Path:
    """Where a key lives in the backup. Keys are relative, with no ``..``: never outside it."""
    pure = PurePosixPath(key)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"unsafe object key {key!r}")
    return dest / OBJECTS / Path(*pure.parts)


def read_manifest(dest: Path) -> dict[str, dict[str, Any]]:
    p = dest / MANIFEST
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def backup_objects(store: Any, dest: Path) -> dict[str, int]:
    """Copy every object of ``store``'s bucket not yet in the backup; verify each copy."""
    manifest = read_manifest(dest)
    added = 0
    for page in store.client.get_paginator("list_objects_v2").paginate(Bucket=store.bucket):
        for o in page.get("Contents", []):
            key = o["Key"]
            have = manifest.get(key)
            target = object_path(dest, key)
            if have and have["size"] == o["Size"] and target.exists():
                continue
            body = store.get_bytes(key)
            head = store.client.head_object(Bucket=store.bucket, Key=key)
            _atomic_write(target, body)
            sha = hashlib.sha256(body).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise RuntimeError(f"backup copy of {key} does not read back identical")
            manifest[key] = {
                "sha256": sha,
                "size": len(body),
                "content_type": head.get("ContentType") or "application/octet-stream",
            }
            added += 1
    _atomic_write(dest / MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode())
    return {"objects": len(manifest), "added": added}


def write_status(
    dest: Path,
    *,
    ok: bool,
    taken_at: dt.datetime,
    dump: str | None = None,
    objects: dict[str, int] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """``taken_at``: when ``pg_dump`` started (its snapshot), the moment the backup reflects.
    A failed run keeps the last good one's time, so the staleness alert keeps counting."""
    prev = read_status(dest) or {}
    status = {
        "ok": ok,
        "run_at": dt.datetime.now(dt.UTC).isoformat(),
        "taken_at": taken_at.isoformat(),
        "dump": dump,
        "objects": objects,
        "error": error,
        "last_success_at": taken_at.isoformat() if ok else prev.get("last_success_at"),
        "last_success_dump": dump if ok else prev.get("last_success_dump"),
    }
    _atomic_write(dest / STATUS, json.dumps(status, indent=1).encode())
    return status


def read_status(dest: Path) -> dict[str, Any] | None:
    p = dest / STATUS
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
