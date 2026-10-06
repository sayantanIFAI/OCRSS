"""E1-S2: the pipeline_run audit fails the build on any stage != ok (needs PostgreSQL)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from cdi_adapter import repo

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load("ci_pipeline_audit")


@pytest.fixture(scope="module")
def engine(migrated_db):
    eng = create_engine(migrated_db)
    yield eng
    eng.dispose()


def _doc(c, sha: str) -> str:
    return str(
        c.execute(
            text(
                "INSERT INTO source_document (sha256, filename, mime_type, byte_size, storage_key) "
                "VALUES (:s, 'x.pdf', 'application/pdf', 10, :k) RETURNING id"
            ),
            {"s": sha, "k": f"ci/{sha}"},
        ).scalar_one()
    )


def _all_ok(c, doc: str) -> None:
    for stage in audit.STAGES:
        repo.finish_run(c, repo.start_run(c, document_id=doc, stage=stage), metrics={})


def test_all_stages_ok_is_clean(engine):
    with engine.begin() as c:
        doc = _doc(c, "b" * 64)
        _all_ok(c, doc)
        assert audit.audit_pipeline_runs(c, [doc]) == []


def test_failed_stage_is_reported(engine):
    with engine.begin() as c:
        doc = _doc(c, "c" * 64)
        _all_ok(c, doc)
        repo.fail_run(c, repo.start_run(c, document_id=doc, stage="extract"), error="boom")
        problems = audit.audit_pipeline_runs(c, [doc])
    assert [(p["stage"], p["status"], p["error"]) for p in problems] == [
        ("extract", "failed", "boom")
    ]


def test_still_running_and_missing_stages_are_reported(engine):
    with engine.begin() as c:
        doc = _doc(c, "d" * 64)
        repo.finish_run(c, repo.start_run(c, document_id=doc, stage="ingest"), metrics={})
        repo.start_run(c, document_id=doc, stage="ocr")  # never finished
        problems = audit.audit_pipeline_runs(c, [doc])
    statuses = {p["stage"]: p["status"] for p in problems}
    assert statuses["ocr"] == "running"
    assert {s for s, v in statuses.items() if v == "missing"} == set(audit.STAGES) - {
        "ingest",
        "ocr",
    }


def test_other_documents_failed_runs_are_not_audited(engine):
    """Failure-path integration tests leave `failed` runs behind on purpose."""
    with engine.begin() as c:
        mine, other = _doc(c, "e" * 64), _doc(c, "f" * 64)
        _all_ok(c, mine)
        repo.fail_run(c, repo.start_run(c, document_id=other, stage="ocr"), error="on purpose")
        assert audit.audit_pipeline_runs(c, [mine]) == []


def test_no_documents_is_a_problem(engine):
    with engine.connect() as c:
        assert audit.audit_pipeline_runs(c, []) != []
