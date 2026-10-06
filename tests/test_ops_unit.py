"""E0-S16: ops scripts, compose alt-deploy, Makefile, pod env, sample-doc tooling."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNPOD = ROOT / "infra" / "runpod"


def _bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    git = shutil.which("git")
    if git:  # Git for Windows: <git>/cmd/git.exe -> <git>/bin/bash.exe
        cand = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if cand.exists():
            return str(cand)
    return None


@pytest.mark.parametrize("script", ["bootstrap_pod.sh", "start_all.sh", "start_mlserve.sh",
                                    "snapshot.sh", "golden_nightly.sh"])  # fmt: skip
def test_pod_scripts_are_valid_bash(script):
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    r = subprocess.run([bash, "-n", str(RUNPOD / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (RUNPOD / script).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "docker" not in text.lower() or script == "bootstrap_pod.sh"  # pod: no Docker


def test_bootstrap_matches_the_persistence_model():
    t = (RUNPOD / "bootstrap_pod.sh").read_text(encoding="utf-8")
    assert "/var/lib/postgresql/16/cdi" in t  # PGDATA on the overlay (§3.3)
    assert "pg_restore" in t and "alembic upgrade head" in t
    assert "--appendonly yes" in t and "$WS/redis" in t  # Redis AOF on /workspace
    assert "*/15 * * * *" in t and "snapshot.sh" in t  # 15-minute dump cron
    assert "git clone" in t and "MINIO_TAG" in t and "sha256sum -c" in t  # MinIO from source
    assert "--system-site-packages" in t  # torch inherited from the pod image


def test_start_all_checks_a_content_marker_not_just_status():
    t = (RUNPOD / "start_all.sh").read_text(encoding="utf-8")
    assert "Agentic EMR generation" in t and "LIVE URL" in t  # §10.4 fake-502 guard


def test_snapshot_keeps_last_ten_and_verifies_before_promoting():
    t = (RUNPOD / "snapshot.sh").read_text(encoding="utf-8")
    assert "pg_dump -Fc" in t and "tail -n +11" in t and "pg_restore --list" in t


def test_compose_alt_deploy_shape():
    c = yaml.safe_load((ROOT / "infra/compose/docker-compose.yml").read_text(encoding="utf-8"))
    services = set(c["services"])
    assert services == {
        "postgres",
        "redis",
        "minio",
        "createbuckets",
        "migrate",
        "api",
        "worker",
        "watcher",
    }  # fmt: skip  (TECH_STACK §13; webapp/mlserve = E1-S3)
    assert c["services"]["postgres"]["image"] == "postgres:16"
    assert c["services"]["redis"]["image"] == "redis:7-alpine"
    assert c["services"]["minio"]["image"] == "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
    worker_cmd = c["services"]["worker"]["command"][:4]
    assert worker_cmd == ["celery", "-A", "cdi_adapter.worker.celery_app", "worker"]
    assert "cdi_adapter.api:app" in c["services"]["api"]["command"]
    for s in ("postgres", "redis", "minio"):
        assert "healthcheck" in c["services"][s]
    for port in c["services"]["postgres"]["ports"] + c["services"]["minio"]["ports"]:
        assert port.startswith("127.0.0.1:")  # never exposed beyond the host


def test_dockerfile_base():
    t = (ROOT / "infra/compose/Dockerfile").read_text(encoding="utf-8")
    assert t.splitlines()[2].startswith("FROM python:3.11-slim")
    assert "postgresql-client" in t and "libgl1" in t and "USER cdi" in t


def test_makefile_targets():
    t = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", t, re.M))
    assert {"venv", "up", "down", "nuke", "logs", "migrate", "api", "worker"} <= targets


def test_env_runpod_keys_are_all_real_settings():
    from cdi_adapter.config import Settings

    keys = [ln.split("=", 1)[0] for ln in (ROOT / ".env.runpod").read_text().splitlines()
            if ln and not ln.startswith("#")]  # fmt: skip
    fields = {f"CDI_{f}".upper() for f in Settings.model_fields}
    assert keys and set(keys) <= fields, set(keys) - fields
    env = dict(ln.split("=", 1) for ln in (ROOT / ".env.runpod").read_text().splitlines()
               if ln and not ln.startswith("#"))  # fmt: skip
    assert env["CDI_MLSERVE_BACKEND"] == "hf" and env["CDI_WEBAPP_PORT"] == "8888"


@pytest.mark.parametrize("script", ["pipeline_smoke.py", "ingest_batch.py",
                                    "make_sample_docs.py"])  # fmt: skip
def test_scripts_have_working_help(script):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src"},
                       cwd=ROOT)  # fmt: skip
    assert r.returncode == 0 and "usage" in r.stdout.lower()


def test_sample_docs_survive_scan_noise_ocr_and_classification(tmp_path):
    """Rotated, speckled sample scans -> deskew -> real RapidOCR -> heuristic or VLM fallback."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_sample_docs as msd
    finally:
        sys.path.pop(0)
    from cdi_adapter.classify.service import _heuristic_classify
    from cdi_adapter.ingest.pages import normalize_image, sniff_mime
    from cdi_adapter.ocr.rapid import run_rapidocr

    files = msd.generate(tmp_path)
    assert len(files) == 6
    # the noisy vitals scan OCRs its heading as "VITALSIGNSCHART" and "Sp02": the heuristic
    # declines (score < 5) and §5 S2 hands the page to the VLM — never a guess
    expected = {"prescription_00": "prescription", "lab_report_01": "lab_report",
                "vitals_sheet_02": None}  # fmt: skip
    for name, doc_type in expected.items():
        pdf, scan = tmp_path / f"{name}.pdf", tmp_path / f"{name}.scan.png"
        assert sniff_mime(pdf.read_bytes()[:16], pdf.name) == "application/pdf"
        norm, pre = normalize_image(scan.read_bytes())
        assert "deskew" in pre["steps"] or abs(pre["skew_deg"]) <= 0.15
        text = "\n".join(ln.text for ln in run_rapidocr(norm))
        res = _heuristic_classify(text)
        got = res.doc_type if res else None
        assert got == doc_type, (name, text[:300])
