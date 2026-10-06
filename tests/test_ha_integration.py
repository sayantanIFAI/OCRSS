"""E10-S6 "failover drill" with real PostgreSQL 16 servers.

Two throwaway clusters (their own folders and ports, never the test database): a standby streams
from the primary through a slot, a writer commits continuously through the app's own multi-host
URL (``engine_url``), the primary is crashed, the standby is promoted, and the drill measures the
commits lost (RPO) and the time until the writer commits again (RTO); the old primary is fenced
and rejoins as a standby. Needs the Postgres tools: ``CDI_PG_BIN`` or the local dev build.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cdi_adapter.ha.pg_drill import RPO_TARGET_S, run_drill

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]


def _bindir() -> Path | None:
    for d in (os.environ.get("CDI_PG_BIN"), ROOT / ".tools" / "pg16_14" / "pgsql" / "bin",
              "/usr/lib/postgresql/16/bin"):  # fmt: skip
        exe = "pg_basebackup.exe" if os.name == "nt" else "pg_basebackup"
        if d and (Path(d) / exe).exists():
            return Path(d)
    return None


def test_failover_drill_loses_no_acknowledged_commit_and_rejoins(tmp_path):
    bindir = _bindir()
    if bindir is None:
        pytest.skip("no PostgreSQL 16 tools here (set CDI_PG_BIN)")
    if os.name == "posix" and os.geteuid() == 0:
        pytest.skip("as root the drill runs the servers as postgres: use the CLI on the pod")
    out = run_drill(bindir, ports=(55441, 55442), seconds=4.0)
    assert out["pass"], out
    assert out["lost_acked_commits"] == 0 and out["rpo_seconds"] < RPO_TARGET_S
    assert out["acked_before_crash"] > 20
    assert out["failover"]["old_primary_fenced"] and out["old_data_fenced"]
    assert out["rejoined_as"] == "standby"
    assert out["rto_seconds"] < 30
