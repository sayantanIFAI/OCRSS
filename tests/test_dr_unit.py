"""E10-S11 unit: the object backup (safe paths, incremental, verified), the backup status, the
restore of objects and the scan check, the drill's RPO input, the monitoring collector and the
snapshot / cron wiring. The drill itself against real PostgreSQL is
``tests/test_dr_integration.py``; with a real MinIO it runs on Colab / the pod."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from cdi_adapter import metrics
from cdi_adapter.dr import __main__ as cli
from cdi_adapter.dr import backup as bk
from cdi_adapter.dr import drill as dr

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = (ROOT / "infra" / "runpod" / "snapshot.sh").read_text(encoding="utf-8")
BOOT = (ROOT / "infra" / "runpod" / "bootstrap_pod.sh").read_text(encoding="utf-8")


class FakeStore:
    """The ``ObjectStore`` surface the backup and the drill use, in memory."""

    bucket = "cdi-documents"

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.types: dict[str, str] = {}
        self.client = self

    # boto3 client surface
    def get_paginator(self, name: str) -> FakeStore:
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str) -> list[dict[str, Any]]:  # noqa: N803 - boto3's name
        items = [{"Key": k, "Size": len(self.objects[k])} for k in sorted(self.objects)]
        return [{"Contents": items[:2]}, {"Contents": items[2:]}]  # two pages, like boto3

    def head_object(self, Bucket: str, Key: str) -> dict[str, str]:  # noqa: N803
        return {"ContentType": self.types.get(Key, "application/pdf")}

    # ObjectStore surface
    def ensure_bucket(self) -> None:
        pass

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key], self.types[key] = data, content_type


def _objects(n: int = 5) -> dict[str, bytes]:
    out = {}
    for i in range(n):
        body = f"scan {i}".encode() * (i + 1)
        sha = hashlib.sha256(body).hexdigest()
        out[f"documents/{sha[:2]}/{sha}/original.pdf"] = body
    return out


# ------------------------------------------------------------------ the object backup


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs/key", "", "a/../../b"])
def test_an_object_key_can_never_write_outside_the_backup(tmp_path, key):
    with pytest.raises(ValueError):
        bk.object_path(tmp_path, key)


def test_objects_are_copied_verified_and_only_once(tmp_path):
    store = FakeStore(_objects(5))
    first = bk.backup_objects(store, tmp_path)
    assert first == {"objects": 5, "added": 5}
    manifest = bk.read_manifest(tmp_path)
    for key, body in store.objects.items():
        assert bk.object_path(tmp_path, key).read_bytes() == body
        assert manifest[key]["sha256"] == hashlib.sha256(body).hexdigest()
        assert manifest[key]["content_type"] == "application/pdf"
    store.objects["documents/zz/new/original.pdf"] = b"one more"
    assert bk.backup_objects(store, tmp_path) == {"objects": 6, "added": 1}  # incremental


def test_a_failed_backup_keeps_the_last_good_time(tmp_path):
    good = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)
    bk.write_status(tmp_path, ok=True, taken_at=good, dump="cdi.20260924T100000Z.dump")
    st = bk.write_status(tmp_path, ok=False, taken_at=good + dt.timedelta(minutes=15),
                         error="pg_dump failed")  # fmt: skip
    assert st["ok"] is False and st["error"] == "pg_dump failed"
    assert st["last_success_at"] == good.isoformat()
    assert st["last_success_dump"] == "cdi.20260924T100000Z.dump"


# ------------------------------------------------------------------ restore + checks


def test_objects_come_back_and_a_corrupt_backup_file_is_refused(tmp_path):
    src = FakeStore(_objects(4))
    bk.backup_objects(src, tmp_path)
    bad_key = sorted(src.objects)[1]
    bk.object_path(tmp_path, bad_key).write_bytes(b"bit rot")
    dst = FakeStore()
    got = dr.restore_objects(tmp_path, dst)
    assert got == {"objects": 4, "restored": 3, "corrupt_in_backup": 1}
    assert bad_key not in dst.objects
    assert all(dst.objects[k] == src.objects[k] for k in dst.objects)


def test_every_scan_is_checked_against_its_recorded_sha256():
    objs = _objects(3)
    rows = [(f"doc{i}", k.split("/")[2], k) for i, k in enumerate(sorted(objs))]
    store = FakeStore(objs)
    assert dr.verify_scans(rows, store) == {"documents": 3, "intact": 3, "missing": [],
                                            "mismatched": []}  # fmt: skip
    store.objects[rows[0][2]] = b"changed"
    del store.objects[rows[1][2]]
    got = dr.verify_scans(rows, store)
    assert got["intact"] == 1 and got["mismatched"] == ["doc0"] and got["missing"] == ["doc1"]


def test_the_rpo_input_is_the_dumps_snapshot_time(tmp_path):
    for stamp in ("20260924T100000Z", "20260924T101500Z"):
        (tmp_path / f"cdi.{stamp}.dump").write_bytes(b"x")
    newest = dr.newest_dump(tmp_path)
    assert newest.name == "cdi.20260924T101500Z.dump"
    assert dr.dump_time(newest) == dt.datetime(2026, 9, 24, 10, 15, tzinfo=dt.UTC)
    # the status names the newest GOOD dump: a later, unfinished one is not trusted
    bk.write_status(tmp_path, ok=True, taken_at=dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC),
                    dump="cdi.20260924T100000Z.dump")  # fmt: skip
    assert dr.newest_dump(tmp_path).name == "cdi.20260924T100000Z.dump"


def test_the_targets_are_the_documented_ones():
    doc = (ROOT / "docs" / "dr.md").read_text(encoding="utf-8")
    assert dr.RPO_TARGET_S == 20 * 60 and dr.RTO_TARGET_S == 30 * 60
    assert "20 minutes" in doc and "30 minutes" in doc


# ------------------------------------------------------------------ monitoring + wiring


def _scrape(collector) -> dict[str, float]:
    reg = CollectorRegistry()
    reg.register(collector)
    text = generate_latest(reg).decode()
    return {s.name: s.value for f in text_string_to_metric_families(text) for s in f.samples}


def test_the_collector_reports_backup_and_drill():
    backup = {"ok": False, "last_success_at": "2026-09-24T10:00:00+00:00"}
    drill = {"ok": True, "finished_at": "2026-09-24T11:00:00+00:00", "rpo_s": 300.0,
             "rto_s": 120.5}  # fmt: skip
    got = _scrape(metrics.DrCollector(lambda: (backup, drill)))
    assert got["cdi_backup_last_ok"] == 0
    assert got["cdi_backup_last_success_timestamp_seconds"] == dt.datetime(
        2026, 9, 24, 10, tzinfo=dt.UTC).timestamp()  # fmt: skip
    assert got["cdi_dr_restore_last_ok"] == 1 and got["cdi_dr_rto_seconds"] == 120.5
    assert got["cdi_dr_rpo_seconds"] == 300.0
    assert _scrape(metrics.DrCollector(lambda: (None, None))) == {}


def test_every_snapshot_backs_up_the_objects_and_records_failures():
    assert "cdi_adapter.dr backup --dump" in SNAPSHOT
    assert "trap " in SNAPSHOT and "--failed" in SNAPSHOT
    dump_at = SNAPSHOT.index('mv "$TMP"')
    assert SNAPSHOT.index("cdi_adapter.dr backup --dump") > dump_at  # after the dump is good


def test_the_restore_drill_runs_weekly_from_cron():
    assert "30 3 * * 0" in BOOT and "cdi_adapter.dr drill" in BOOT
    assert "-e 'cdi_adapter.dr drill'" in BOOT  # re-running the bootstrap does not duplicate it


def test_every_command_parses():
    p = cli.build_parser()
    assert p.parse_args(["backup", "--dump", "d", "--taken-at", "2026-09-24T10:00:00Z"]).cmd
    assert p.parse_args(["backup", "--failed", "x", "--taken-at", "t"]).failed == "x"
    assert p.parse_args(["drill", "--no-golden", "--live"]).no_golden
    assert p.parse_args(["status"]).cmd == "status"
    with pytest.raises(SystemExit):
        p.parse_args(["backup", "--dump", "d", "--failed", "x", "--taken-at", "t"])


def test_restore_subprocess_uses_disposable_store_even_with_live_store(tmp_path, monkeypatch):
    from cdi_adapter import secretstore
    from cdi_adapter.config import get_settings

    live = tmp_path / "live"
    secretstore.init(live)
    before = {p.name: p.read_bytes() for p in live.iterdir()}
    monkeypatch.setenv("CDI_SECRETS_DIR", str(live))
    monkeypatch.setenv("CDI_DATABASE_URL", "postgresql://live.invalid/production")
    password = secretstore.part(secretstore.read(live, "cdi_database_url"), "password")
    target = f"postgresql+psycopg://cdi:{password}@127.0.0.1:55471/cdi"
    s3 = {
        "CDI_S3_ENDPOINT_URL": "http://127.0.0.1:9361",
        "CDI_S3_ACCESS_KEY": "synthetic-dr-access",
        "CDI_S3_SECRET_KEY": "synthetic-dr-secret",
    }
    env = dr._env_for(target, s3, tmp_path / "drill")
    assert "CDI_DATABASE_URL" not in env and "CDI_S3_SECRET_KEY" not in env
    with monkeypatch.context() as child:
        for key, value in env.items():
            child.setenv(key, value)
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.database_url == target
        assert settings.s3_secret_key == s3["CDI_S3_SECRET_KEY"]
        assert settings.s3_endpoint_url == s3["CDI_S3_ENDPOINT_URL"]
    get_settings.cache_clear()
    assert {p.name: p.read_bytes() for p in live.iterdir()} == before
