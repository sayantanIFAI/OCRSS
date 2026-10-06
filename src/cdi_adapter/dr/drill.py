"""The restore / DR drill (backlog E10-S11; ``docs/dr.md``).

It rehearses the worst case: the pod's data tier is gone, the standby too (E10-S6), and only the
backups are left. In a clean environment, from the backup folder alone:

1. a new, empty PostgreSQL cluster; the newest good ``pg_dump`` restored into it
2. the schema brought to Alembic head (a restore from an older release must still come up)
3. a new, empty MinIO; every backed-up object put back, each checked against its sha256
4. **every document's original scan** read back from the restored MinIO and checked against the
   ``source_document.sha256`` recorded at ingest: the irreplaceable pairing of rows and bytes
5. the golden path (E1-S6, ``--mode seeded``) run against the restored server
6. RPO (from the backup's snapshot time to the moment of the simulated loss) and RTO (from the
   start of the rebuild until it is proven working) measured against ``RPO_TARGET_S`` and
   ``RTO_TARGET_S``

A report goes to ``<report dir>/restore-<time>.json`` and ``last.json``; the monitoring alerts on
a failed or overdue drill. The live database and store are never touched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cdi_adapter.dr import backup as bk
from cdi_adapter.ha import pg as ha

ROOT = Path(__file__).resolve().parents[3]
RPO_TARGET_S = 20 * 60  # the 15-minute backup cadence, plus the dump and the object copy
RTO_TARGET_S = 30 * 60  # a clean rebuild of the data tier, proven working
LAST = "last.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def newest_dump(backup_dir: Path) -> Path:
    status = bk.read_status(backup_dir) or {}
    name = status.get("last_success_dump")
    if name and (backup_dir / name).exists():
        return backup_dir / name
    dumps = sorted(backup_dir.glob("cdi.*Z.dump"))
    if not dumps:
        raise ha.HaError(f"no pg_dump in {backup_dir}")
    return dumps[-1]


def dump_time(dump: Path) -> dt.datetime:
    """``cdi.<YYYYmmddTHHMMSSZ>.dump`` (snapshot.sh): when ``pg_dump`` took its snapshot."""
    stamp = dump.name.split(".")[1]
    return dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)


def restore_objects(backup_dir: Path, store: Any) -> dict[str, int]:
    store.ensure_bucket()
    bad = 0
    manifest = bk.read_manifest(backup_dir)
    for key, meta in manifest.items():
        body = bk.object_path(backup_dir, key).read_bytes()
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            bad += 1  # a backup file that no longer matches its manifest: not restored silently
            continue
        store.put_bytes(key, body, meta["content_type"])
    return {"objects": len(manifest), "restored": len(manifest) - bad, "corrupt_in_backup": bad}


def verify_scans(rows: list[tuple[str, str, str]], store: Any) -> dict[str, Any]:
    """Each (document id, sha256, storage key): the original's bytes are there and identical."""
    missing, wrong = [], []
    for doc_id, sha, key in rows:
        try:
            body = store.get_bytes(key)
        except Exception:  # noqa: BLE001 - not there is the finding
            missing.append(str(doc_id))
            continue
        if hashlib.sha256(body).hexdigest() != sha:
            wrong.append(str(doc_id))
    return {
        "documents": len(rows),
        "intact": len(rows) - len(missing) - len(wrong),
        "missing": missing[:20],
        "mismatched": wrong[:20],
    }


def _env_for(url: str, s3: dict[str, str] | None, store: Path) -> dict[str, str]:
    # The application store is authoritative. Give subprocesses a private disposable store
    # so migrations and golden runs cannot inherit the live database or S3 credentials.
    from cdi_adapter import secretstore

    secretstore.init(store)
    secretstore.write(store, "cdi_database_url", url)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {f"CDI_{field.upper()}" for field in secretstore.FIELDS}
    }
    env.update(
        {
            "CDI_SECRETS_DIR": str(store),
            "CDI_DATABASE_HOSTS": "",
            "CDI_MLSERVE_BACKEND": "stub",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    for k, v in (s3 or {}).items():
        if k in {"CDI_S3_ACCESS_KEY", "CDI_S3_SECRET_KEY"}:
            secretstore.write(store, k.lower(), v)
        else:
            env[k] = v
    return env


def _run(cmd: list[str], env: dict[str, str], timeout: float = 900) -> tuple[bool, str]:
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def lost_since(live_url: str | None, taken_at: dt.datetime) -> int | None:
    """Documents the live database has that the backup does not (ingested after its snapshot)."""
    if not live_url:
        return None
    import psycopg

    with psycopg.connect(live_url.replace("+psycopg", ""), connect_timeout=5) as c:
        row = c.execute(
            "SELECT count(*) FROM source_document WHERE created_at > %s", (taken_at,)
        ).fetchone()
    return int(row[0]) if row else None


def run_drill(
    backup_dir: Path,
    *,
    pg_bin: Path | str,
    store_factory: Callable[[str, str, str], Any] | None = None,
    minio_bin: Path | str | None = None,
    ports: tuple[int, int] = (55461, 9361),
    live_url: str | None = None,
    golden: bool = True,
    report_dir: Path | None = None,
    keep: bool = False,
    loss_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """``store_factory(endpoint, access, secret)`` builds the object store client; a real MinIO
    is started from ``minio_bin`` when given (the pod, Colab)."""
    from cdi_adapter.config import Settings
    from cdi_adapter.storage import ObjectStore

    loss_at = loss_at or _now()
    t0 = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="cdi-restore-drill-"))
    pw = secrets.token_urlsafe(18)
    pg = ha.Pg.for_host(pg_bin, admin_user="cdi", admin_password=pw)
    pg.owned(work)
    node = ha.Node("restored", ports[0], work / "pgdata")
    report: dict[str, Any] = {
        "backup_dir": str(backup_dir),
        "loss_at": loss_at.isoformat(),
        "rpo_target_s": RPO_TARGET_S,
        "rto_target_s": RTO_TARGET_S,
    }
    minio = None
    try:
        dump = newest_dump(backup_dir)
        taken_at = dump_time(dump)
        report |= {
            "dump": dump.name,
            "backup_taken_at": taken_at.isoformat(),
            "rpo_s": round((loss_at - taken_at).total_seconds(), 1),
        }
        report["documents_lost_since_backup"] = lost_since(live_url, taken_at)

        # 1-2. Postgres from the dump alone, then to Alembic head
        ha.init_cluster(pg, node, superuser="cdi", password=pw, log=work / "pg.log")
        pg.sql(node, "CREATE DATABASE cdi")
        url = f"postgresql+psycopg://cdi:{pw}@127.0.0.1:{node.port}/cdi"
        r = pg.run(
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "-d",
            url.replace("+psycopg", ""),
            str(dump),
            check=False,
        )
        report["pg_restore_ok"] = r.returncode == 0
        if r.returncode != 0:
            report["pg_restore_error"] = (r.stderr or "")[-600:]
        ok, out = _run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            _env_for(url, None, work / "secrets"),
        )
        report["schema_at_head"] = ok
        if not ok:
            report["alembic_error"] = out[-600:]
        db = ha.Pg(pg_bin, os_user=pg.os_user, admin_user="cdi", admin_password=pw, dbname="cdi")
        report["rows"] = {
            t: int(db.sql(node, f"SELECT count(*) FROM {t}")[0][0])  # noqa: S608
            for t in (
                "source_document",
                "document_page",
                "clinical_fact",
                "review_task",
                "audit_log",
                "web_user",
            )
        }

        # 3. MinIO from the object backup alone
        access, secret = "restore" + secrets.token_hex(4), secrets.token_urlsafe(24)
        endpoint = f"http://127.0.0.1:{ports[1]}"
        if minio_bin is not None:
            from cdi_adapter.ha import minio as mc

            minio = mc.Cluster(
                Path(minio_bin), (f"127.0.0.1:{ports[1]}",), ((work / "minio").as_posix(),), work
            )
            mc.start(minio, mc.node_env(access, secret), work)
        if store_factory is not None:
            store = store_factory(endpoint, access, secret)
        else:
            store = ObjectStore(
                Settings(
                    s3_endpoint_url=endpoint,
                    s3_access_key=access,
                    s3_secret_key=secret,
                    s3_bucket="cdi-documents",
                )
            )
        report["objects"] = restore_objects(backup_dir, store)

        # 4. every original scan: rows and bytes still belong together
        rows = [
            (str(i), s, k)
            for i, s, k in db.sql(node, "SELECT id, sha256, storage_key FROM source_document")
        ]
        report["scans"] = verify_scans(rows, store)
        restored_at = time.monotonic()

        # 5. the golden path against the restored server (a database of its own there)
        if golden:
            s3env = {
                "CDI_S3_ENDPOINT_URL": endpoint,
                "CDI_S3_ACCESS_KEY": access,
                "CDI_S3_SECRET_KEY": secret,
            }
            ok, out = _run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "golden_path.py"),
                    "--mode",
                    "seeded",
                    "--baseline",
                    "stub",
                    "--fresh-db",
                    "cdi_golden",
                ],
                _env_for(url, s3env, work / "secrets"),
            )
            report["golden_path_ok"] = ok
            report["golden_path_tail"] = out[-500:]
        report["restore_s"] = round(restored_at - t0, 1)
        report["rto_s"] = round(time.monotonic() - t0, 1)

        sc = report["scans"]
        checks = {
            "Postgres restored from the dump alone": report["pg_restore_ok"],
            "schema at Alembic head": report["schema_at_head"],
            "every backed-up object restored": report["objects"]["corrupt_in_backup"] == 0,
            "every original scan byte-identical": sc["intact"] == sc["documents"],
            f"RPO within {RPO_TARGET_S // 60} min": report["rpo_s"] <= RPO_TARGET_S,
            f"RTO within {RTO_TARGET_S // 60} min": report["rto_s"] <= RTO_TARGET_S,
        }
        if golden:
            checks["golden path passes on the restored environment"] = report["golden_path_ok"]
        report["checks"] = checks
        report["ok"] = all(checks.values())
    except Exception as exc:  # noqa: BLE001 - a drill that cannot finish is a failed drill
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"[-800:]
        report["rto_s"] = round(time.monotonic() - t0, 1)
    finally:
        if minio is not None:
            from cdi_adapter.ha import minio as mc

            mc.stop_node(minio, 1)
        pg.stop(node, "immediate")
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    report["finished_at"] = _now().isoformat()
    if report_dir is not None:
        write_report(report_dir, report)
    return report


def write_report(report_dir: Path, report: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"restore-{stamp}.json"
    text = json.dumps(report, indent=1, default=str)
    path.write_text(text, encoding="utf-8")
    bk._atomic_write(report_dir / LAST, text.encode())
    return path


def read_last(report_dir: Path) -> dict[str, Any] | None:
    try:
        return dict(json.loads((report_dir / LAST).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
