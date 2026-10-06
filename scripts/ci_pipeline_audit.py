"""CI pipeline audit (backlog E1-S2): run the §8.2 sample set S1->S9 on the stub VLM, then fail the
build if any `pipeline_run` row of that run has a status other than `ok`.

Only the documents this run ingests are audited: the integration tests create `failed` runs on
purpose (failure-path tests), and those must not turn the build red.

Needs PostgreSQL at head + a real MinIO (ARCHITECTURE §3.3), configured through the usual `CDI_*`
settings, and `CDI_MLSERVE_BACKEND=stub`.

    python scripts/ci_pipeline_audit.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("ingest", "ocr", "classify", "extract", "terminology", "validate", "project")


def audit_pipeline_runs(conn: Connection, document_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Problems for these documents: every run not `ok`, and every S1-S9 stage with no run."""
    if not document_ids:
        return [{"document_id": None, "stage": None, "status": "no documents", "error": None}]
    rows = conn.execute(
        text(
            "SELECT document_id::text AS document_id, stage, status, error FROM pipeline_run "
            "WHERE document_id = ANY(CAST(:ids AS uuid[])) ORDER BY document_id, started_at"
        ),
        {"ids": list(document_ids)},
    ).mappings()
    problems: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {d: set() for d in document_ids}
    for r in rows:
        seen[r["document_id"]].add(r["stage"])
        if r["status"] != "ok":
            problems.append(dict(r))
    for doc, stages in seen.items():
        for stage in STAGES:
            if stage not in stages:
                problems.append(
                    {"document_id": doc, "stage": stage, "status": "missing", "error": None}
                )
    return problems


def run_samples(folder: Path) -> list[str]:
    """Generate the §8.2 samples and drive them S1->S9; return the ingested document ids."""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_sample_docs.py"), "--out", str(folder)],
        check=True,
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    import pipeline_smoke

    rows = pipeline_smoke.run(folder, full=True)
    return [r["document_id"] for r in rows]


def main() -> int:
    from cdi_adapter.config import get_settings
    from cdi_adapter.db import get_engine

    if get_settings().mlserve_backend != "stub":
        print("[audit] CDI_MLSERVE_BACKEND must be 'stub' in CI", flush=True)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        doc_ids = run_samples(Path(tmp))
    with get_engine().connect() as conn:
        problems = audit_pipeline_runs(conn, doc_ids)
    print(f"[audit] {len(doc_ids)} documents, {len(problems)} problem run(s)", flush=True)
    for p in problems:
        print(f"[audit]   {p}", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
