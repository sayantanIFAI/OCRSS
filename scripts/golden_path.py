"""Golden-path regression (backlog E1-S6): the ARCHITECTURE §8.2 "Anjali Das" 3-document example.

Runs the three §8.2 documents through the pipeline, measures what landed in PostgreSQL, and
compares it with `tests/golden/anjali_das.json`. Any difference is drift: exit 1, and a line is
appended to `--alert-log` (the nightly run's alert channel until E10 observability exists).

Modes
  full    S1 -> S9 exactly as the pipeline runs (scripts/pipeline_smoke.py over the rendered,
          scan-like samples). Needs PostgreSQL + a real MinIO (+ the GPU for --baseline real_vlm).
  seeded  S2 -> S9 on PostgreSQL only: the §8.2 text is seeded as OCR lines (no scan, no object
          store) and S4 runs through the same write path as `extract_document`. Stub only.

Baselines (in the golden file)
  stub      what the deterministic stub backend must produce (CI; no GPU)
  real_vlm  the §8.2 recorded run of the real 7B model (nightly on the pod)

The counts are defined on a FRESH database (as §8.2 was recorded): shared actors such as the
Patient and the Organization are stored once and attributed to the first document that created
them, so a database that already holds them yields fewer rows. `--fresh-db NAME` resets a
dedicated database to Alembic head first; it refuses to touch the configured (live) database.

    python scripts/golden_path.py --mode seeded --fresh-db cdi_golden
    python scripts/golden_path.py --mode full --baseline real_vlm --fresh-db cdi_golden
        --alert-log /workspace/logs/golden_alerts.log     (one command; wrapped here)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "anjali_das.json"
sys.path.insert(0, str(ROOT / "scripts"))


def load_golden(path: Path = GOLDEN) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# -------------------------------------------------------------------------------- fresh db
def prepare_fresh_db(name: str) -> str:
    """Reset database `name` (same server + credentials as the configured one) to Alembic head
    and point this process at it. Returns its URL. Never the configured database itself."""
    from alembic import command
    from alembic.config import Config

    from cdi_adapter import db
    from cdi_adapter.config import get_settings

    live = make_url(get_settings().database_url)
    if name == live.database:
        raise SystemExit(f"--fresh-db {name!r} is the configured database; refusing to reset it")
    target = live.set(database=name).render_as_string(hide_password=False)
    admin = create_engine(live.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": name}).first():
            c.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    eng = create_engine(target, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("DROP SEQUENCE IF EXISTS patient_mpi_seq"))
    eng.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "db" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", target.replace("%", "%%"))
    command.upgrade(cfg, "head")
    os.environ["CDI_DATABASE_URL"] = target
    get_settings.cache_clear()
    db.reset_engine()
    return target


# ---------------------------------------------------------------------------------- measure
def measure(conn: Connection, docs: Mapping[str, str]) -> dict[str, Any]:
    """Per-document and total counts for the documents of one golden run."""
    out: dict[str, Any] = {"documents": {}, "totals": {}}
    for name, doc in docs.items():
        q = {"d": doc}
        row = {
            "doc_type": conn.execute(
                text("SELECT doc_type FROM doc_classification WHERE document_id = :d "
                     "ORDER BY created_at DESC LIMIT 1"), q).scalar(),
            "facts": conn.execute(
                text("SELECT count(DISTINCT fact_id) FROM fact_provenance "
                     "WHERE source_doc_id = :d"), q).scalar_one(),
            "provenance": conn.execute(
                text("SELECT count(*) FROM fact_provenance WHERE source_doc_id = :d"), q
            ).scalar_one(),
            "resources": conn.execute(
                text("SELECT count(*) FROM fhir_resource WHERE source_document_id = :d"), q
            ).scalar_one(),
            "bundles": conn.execute(
                text("SELECT count(*) FROM fhir_bundle WHERE source_document_id = :d"), q
            ).scalar_one(),
        }  # fmt: skip
        b = conn.execute(
            text("SELECT artifact_type, status FROM fhir_bundle WHERE source_document_id = :d "
                 "ORDER BY created_at DESC LIMIT 1"), q).mappings().first()  # fmt: skip
        row["artifact_type"] = b["artifact_type"] if b else None
        row["status"] = b["status"] if b else None
        errs = conn.execute(
            text("SELECT metrics->>'errors' FROM pipeline_run WHERE document_id = :d "
                 "AND stage = 'project' AND status = 'ok' ORDER BY started_at DESC LIMIT 1"), q
        ).scalar()  # fmt: skip
        row["structural_errors"] = int(errs) if errs is not None else None
        out["documents"][name] = row
    for key in ("facts", "provenance", "resources", "bundles", "structural_errors"):
        vals = [d[key] for d in out["documents"].values()]
        out["totals"][key] = None if None in vals else sum(vals)
    return out


def compare(measured: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """Drift lines: every expected key whose measured value differs (extra keys are ignored)."""
    drift: list[str] = []
    for name, exp in expected["documents"].items():
        got = measured["documents"].get(name)
        if got is None:
            drift.append(f"{name}: document missing from the run")
            continue
        for key, want in exp.items():
            if got.get(key) != want:
                drift.append(f"{name}.{key}: expected {want!r}, got {got.get(key)!r}")
    for key, want in expected.get("totals", {}).items():
        if measured["totals"].get(key) != want:
            drift.append(f"totals.{key}: expected {want!r}, got {measured['totals'].get(key)!r}")
    return drift


# -------------------------------------------------------------------------------------- run
def run_full(folder: Path) -> dict[str, str]:
    """S1 -> S9 over the rendered samples, exactly as scripts/pipeline_smoke.py drives them."""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_sample_docs.py"), "--out", str(folder)],
        check=True,
    )
    import pipeline_smoke

    return {Path(r["file"]).stem: r["document_id"] for r in pipeline_smoke.run(folder, True)}


def run_seeded() -> dict[str, str]:
    """S2 -> S9 on PostgreSQL only: the §8.2 text seeded as OCR lines, stub extraction."""
    from make_sample_docs import DOCS

    from cdi_adapter import repo
    from cdi_adapter.classify.service import classify_document
    from cdi_adapter.config import get_settings
    from cdi_adapter.db import session_scope
    from cdi_adapter.extract.prompt import build_extraction_prompt, max_tokens_for
    from cdi_adapter.extract.service import SCHEMA_FOR_DOC_TYPE, write_extraction
    from cdi_adapter.fhir.service import project_document
    from cdi_adapter.ml.client import StubMLClient
    from cdi_adapter.terminology.service import bind_document
    from cdi_adapter.validate.service import validate_document

    if get_settings().mlserve_backend != "stub":
        raise SystemExit("--mode seeded is stub-only (CDI_MLSERVE_BACKEND=stub)")
    docs: dict[str, str] = {}
    patient = None
    for name, lines in DOCS.items():
        with session_scope() as c:
            # > 700 KB: S9 references the scan by URL (ARCHITECTURE §7.2), no object-store read
            doc = repo.insert_source_document(
                c, sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                filename=f"{name}.pdf", mime_type="application/pdf", byte_size=1_000_000,
                storage_key=f"golden/{name}",
            )  # fmt: skip
            page = repo.insert_document_page(
                c, document_id=doc, page_no=1, storage_key=f"golden/{name}/p1",
                thumb_key=f"golden/{name}/t1", width=1654, height=2339, dpi=200, preproc={},
            )  # fmt: skip
            repo.insert_ocr_blocks(c, [
                {"document_id": doc, "page_id": page, "block_type": "line", "text": t,
                 "bbox": [100, 100 + 40 * i, 1200, 130 + 40 * i], "polygon": None,
                 "ocr_conf": 0.96, "reading_order": i, "engine": "rapidocr",
                 "pipeline_run_id": None}
                for i, t in enumerate(lines)
            ])  # fmt: skip
        doc_id = str(doc)
        doc_type = classify_document(doc_id)["doc_type"]
        with session_scope() as c:
            blocks = repo.get_ocr_blocks(c, doc_id)
        prompt, block_map = build_extraction_prompt(doc_type, blocks)
        payload = StubMLClient().vlm_json(None, prompt, SCHEMA_FOR_DOC_TYPE[doc_type],
                                          max_tokens=max_tokens_for(doc_type))  # fmt: skip
        with session_scope() as c:
            out = write_extraction(
                c, document_id=doc_id, doc_type=doc_type, payload=payload,
                block_id_map=block_map, blocks=blocks, run_ids=[],
                model_stack={"extractor": "stub", "schema": SCHEMA_FOR_DOC_TYPE[doc_type]},
                patient_id=patient,
            )  # fmt: skip
        patient = patient or out["patient_id"]
        bind_document(doc_id)
        validate_document(doc_id)
        project_document(doc_id)
        docs[name] = doc_id
    return docs


def alert(log: Path, baseline: str, mode: str, drift: list[str]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} GOLDEN DRIFT baseline={baseline} mode={mode}: {'; '.join(drift)}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("full", "seeded"), default="full")
    ap.add_argument("--baseline", choices=("stub", "real_vlm"))
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--report", type=Path, help="write the measured counts + drift as JSON")
    ap.add_argument("--alert-log", type=Path, help="append one line here on drift")
    ap.add_argument("--fresh-db", metavar="NAME", help="reset this database to head and use it")
    args = ap.parse_args(argv)

    from cdi_adapter.config import get_settings
    from cdi_adapter.db import get_engine

    baseline = args.baseline or ("stub" if get_settings().mlserve_backend == "stub" else "real_vlm")
    if args.mode == "seeded" and baseline != "stub":
        ap.error("--mode seeded only has a stub baseline")
    expected = load_golden(args.golden)[baseline]
    if args.fresh_db:
        prepare_fresh_db(args.fresh_db)

    if args.mode == "full":
        with tempfile.TemporaryDirectory() as tmp:
            docs = run_full(Path(tmp))
    else:
        docs = run_seeded()
    with get_engine().connect() as conn:
        measured = measure(conn, docs)
    drift = compare(measured, expected)

    print(json.dumps(measured["totals"]), flush=True)
    for line in drift:
        print(f"[golden] DRIFT {line}", flush=True)
    print(f"[golden] {baseline}/{args.mode}: {'OK' if not drift else f'{len(drift)} drift(s)'}")
    if args.report:
        report = {"baseline": baseline, "mode": args.mode, "measured": measured, "drift": drift}
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if drift and args.alert_log:
        alert(args.alert_log, baseline, args.mode, drift)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
