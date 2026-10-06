"""E0-S16 integration: ``scripts/verify_s3.sql`` runs cleanly on real PostgreSQL."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def _psql() -> str | None:
    local = ROOT / ".tools" / "pg16_14" / "pgsql" / "bin" / "psql.exe"
    return str(local) if local.exists() else shutil.which("psql")


def test_verify_s3_sql_runs(migrated_db):
    psql = _psql()
    if psql is None:
        pytest.skip("psql not available")
    u = make_url(migrated_db)
    r = subprocess.run(
        [psql, "-h", u.host, "-p", str(u.port or 5432), "-U", u.username, "-d", u.database,
         "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "scripts" / "verify_s3.sql")],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": u.password or ""},
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "bad_bbox" in r.stdout and "OCR blocks per document" in r.stdout
