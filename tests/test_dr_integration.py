"""E10-S11 "the restore drill itself" with real PostgreSQL 16.

A source database of its own is built on the test server: documents whose original scans are in
an (in-memory) object store, a web user, the lot at Alembic head. It is backed up exactly as
``snapshot.sh`` does (``pg_dump -Fc`` + ``backup_objects`` + the status), then the drill rebuilds
it in a clean throwaway cluster from the backup folder alone, proves every scan byte-identical,
runs the golden path there and measures RPO and RTO. A corrupted backup must fail the drill.
Needs the Postgres tools (``CDI_PG_BIN`` or the local dev build); with a real MinIO the same
drill runs on Colab / the pod. There the drill also runs the golden path against the restored
environment; that step stores its scans in the restored MinIO (S9 embeds each scan, §5 S9), so the
in-memory store here runs the drill without it (``golden=False``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cdi_adapter import repo
from cdi_adapter.dr import backup as bk
from cdi_adapter.dr import drill as dr
from cdi_adapter.ha import pg as ha
from cdi_adapter.storage import original_key
from tests.conftest import alembic_cfg
from tests.test_dr_unit import FakeStore
from tests.test_ha_integration import _bindir

pytestmark = pytest.mark.integration


@pytest.fixture
def source(pg_url, tmp_path):
    """A database of this test's own (never the shared one), with scans in a store."""
    bindir = _bindir()
    if bindir is None:
        pytest.skip("no PostgreSQL 16 tools here (set CDI_PG_BIN)")
    if os.name == "posix" and os.geteuid() == 0:
        pytest.skip("as root the drill runs the servers as postgres: use the CLI on the pod")
    name = "cdi_dr_src_" + uuid.uuid4().hex[:8]
    admin = create_engine(make_url(pg_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_cfg(url), "head")
    store = FakeStore()
    eng = create_engine(url)
    with eng.begin() as c:
        for i in range(4):
            body = f"%PDF-1.7 scan {i} {uuid.uuid4()}".encode() * 50
            sha = hashlib.sha256(body).hexdigest()
            key = original_key(sha, "pdf")
            store.put_bytes(key, body, "application/pdf")
            repo.insert_source_document(c, sha256=sha, filename=f"scan{i}.pdf",
                                        mime_type="application/pdf", byte_size=len(body),
                                        storage_key=key)  # fmt: skip
    eng.dispose()
    yield url, store, bindir, tmp_path / "backup"
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _snapshot(url: str, store: FakeStore, bindir: Path, backup: Path) -> dt.datetime:
    """What snapshot.sh does: pg_dump, then the objects, then the status."""
    backup.mkdir(parents=True, exist_ok=True)
    taken = dt.datetime.now(dt.UTC).replace(microsecond=0)
    name = f"cdi.{taken:%Y%m%dT%H%M%SZ}.dump"
    ha.Pg(bindir).run("pg_dump", "-Fc", "-d", url.replace("+psycopg", ""), "-f",
                      str(backup / name))  # fmt: skip
    counts = bk.backup_objects(store, backup)
    bk.write_status(backup, ok=True, taken_at=taken, dump=name, objects=counts)
    return taken


def test_the_data_tier_is_rebuilt_from_the_backups_alone(source, tmp_path):
    url, store, bindir, backup = source
    taken = _snapshot(url, store, bindir, backup)
    restored = FakeStore()
    rep = dr.run_drill(backup, pg_bin=bindir, store_factory=lambda *_: restored,
                       ports=(55471, 0), report_dir=tmp_path / "dr", golden=False,
                       loss_at=taken + dt.timedelta(minutes=7))  # fmt: skip
    assert rep["ok"], rep
    assert rep["rows"]["source_document"] == 4
    assert rep["scans"] == {"documents": 4, "intact": 4, "missing": [], "mismatched": []}
    assert rep["objects"] == {"objects": 4, "restored": 4, "corrupt_in_backup": 0}
    assert rep["schema_at_head"] and rep["pg_restore_ok"]
    assert "golden path passes on the restored environment" not in rep["checks"]  # pod / Colab
    assert rep["rpo_s"] == 420.0  # 7 minutes after the snapshot
    assert 0 < rep["rto_s"] < dr.RTO_TARGET_S
    assert restored.objects == store.objects
    last = dr.read_last(tmp_path / "dr")
    assert last is not None and last["ok"] and last["rto_s"] == rep["rto_s"]


def test_a_backup_that_does_not_restore_fails_the_drill(source, tmp_path):
    url, store, bindir, backup = source
    _snapshot(url, store, bindir, backup)
    key = sorted(store.objects)[0]
    bk.object_path(backup, key).write_bytes(b"corrupted on the backup disk")
    rep = dr.run_drill(backup, pg_bin=bindir, store_factory=lambda *_: FakeStore(),
                       ports=(55472, 0), report_dir=tmp_path / "dr", golden=False)  # fmt: skip
    assert rep["ok"] is False
    assert rep["objects"]["corrupt_in_backup"] == 1
    assert rep["scans"]["intact"] == 3 and len(rep["scans"]["missing"]) == 1
    assert not rep["checks"]["every original scan byte-identical"]
    assert dr.read_last(tmp_path / "dr")["ok"] is False  # -> CdiRestoreDrillFailed


def test_a_damaged_dump_fails_the_drill(source, tmp_path):
    url, store, bindir, backup = source
    _snapshot(url, store, bindir, backup)
    dump = dr.newest_dump(backup)
    dump.write_bytes(dump.read_bytes()[:2000])  # truncated on disk
    rep = dr.run_drill(backup, pg_bin=bindir, store_factory=lambda *_: FakeStore(),
                       ports=(55473, 0), golden=False)  # fmt: skip
    assert rep["ok"] is False and rep.get("pg_restore_ok") is False
