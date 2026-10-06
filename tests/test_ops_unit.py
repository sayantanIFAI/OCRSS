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
        "webapp",
        "worker",
        "mlserve",
    }
    assert c["services"]["postgres"]["image"] == "postgres:16"
    assert c["services"]["redis"]["image"] == "redis:7-alpine"
    assert c["services"]["minio"]["build"]["dockerfile"] == "infra/compose/Dockerfile.minio"
    for name in ("webapp", "worker"):
        deps = c["services"][name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully"
        assert deps["createbuckets"]["condition"] == "service_completed_successfully"
        assert deps["mlserve"]["condition"] == "service_healthy"
    for name in ("postgres", "redis", "minio", "webapp", "mlserve"):
        assert "healthcheck" in c["services"][name]
    for name in ("postgres", "redis", "minio", "mlserve"):
        assert not c["services"][name].get("ports")
    assert c["services"]["webapp"]["ports"][0].startswith("127.0.0.1:")
    assert "env_file" not in c["x-app"]
    assert c["services"]["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"].startswith(
        "/run/secrets/"
    )


def test_dockerfile_base():
    t = (ROOT / "infra/compose/Dockerfile").read_text(encoding="utf-8")
    assert t.startswith("FROM python:3.12-slim-bookworm")
    assert "postgresql-client" in t and "libgl1" in t and "gosu" in t
    entry = (ROOT / "infra/compose/entrypoint.sh").read_text()
    assert 'exec gosu cdi "$@"' in entry and "install -m 600" in entry


def test_makefile_targets():
    t = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", t, re.M))
    assert {"venv", "up", "down", "nuke", "logs", "migrate", "api", "worker"} <= targets


def test_env_runpod_keys_are_all_real_settings():
    from cdi_adapter.config import Settings

    keys = [ln.split("=", 1)[0] for ln in (ROOT / ".env.runpod").read_text().splitlines()
            if ln and not ln.startswith("#")]  # fmt: skip
    fields = {f"CDI_{f}".upper() for f in Settings.model_fields}
    # E1-S9: the ingress keys are read by the lifecycle scripts only, never by the app
    tunnel = (RUNPOD / "start_tunnel.sh").read_text(encoding="utf-8")
    script_only = {k for k in keys if k.startswith("CDI_INGRESS_")}
    assert script_only and all(f"${{{k}" in tunnel for k in script_only), script_only
    # E10-S6: the data-tier HA keys are read by the bootstrap and the `cdi_adapter.ha` commands
    boot = (RUNPOD / "bootstrap_pod.sh").read_text(encoding="utf-8")
    ha_cli = (ROOT / "src" / "cdi_adapter" / "ha" / "__main__.py").read_text(encoding="utf-8")
    ha_keys = {"CDI_PG_STANDBY_PORT", "CDI_MINIO_MODE", "CDI_MINIO_ADDRESSES", "CDI_MINIO_DRIVES"}
    assert ha_keys <= set(keys)
    assert all(f"${{{k}" in boot or f'"{k}"' in ha_cli for k in ha_keys), ha_keys
    script_only |= ha_keys
    # E10-S9: how many instances / workers, and the balancer: read by start_apptier.sh only
    apptier = (RUNPOD / "start_apptier.sh").read_text(encoding="utf-8")
    app_keys = {"CDI_WEBAPP_INSTANCES", "CDI_WEBAPP_BASE_PORT", "CDI_WORKERS",
                "CDI_WORKER_CONCURRENCY", "CDI_LB_ADMIN_PORT",
                "CDI_GPU_WORKERS"}  # E9-S2: how many GPU-pool workers  # fmt: skip
    assert app_keys <= set(keys) and all(f"${{{k}" in apptier for k in app_keys), app_keys
    script_only |= app_keys
    assert keys and set(keys) - script_only <= fields, set(keys) - script_only - fields
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


def test_failed_restore_is_retried_before_boot_can_continue(tmp_path):
    import os

    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    boot = (RUNPOD / "bootstrap_pod.sh").read_text()
    block = boot.split("RESTORE_MARKER=$PGDATA/cdi.restore-required", 1)[1].split(
        "# ---------------------------------------------------------------- 3. venv", 1
    )[0]
    block = "set -euo pipefail\nRESTORE_MARKER=$PGDATA/cdi.restore-required" + block
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "su").write_text(
        '#!/bin/sh\ncase "$*" in\n'
        '  *SELECT*) [ ! -f "$PGDATA/database" ] || echo 1 ;;\n'
        '  *CREATE*) touch "$PGDATA/database" ;;\nesac\n'
    )
    (fake_bin / "pg_restore").write_text(
        '#!/bin/sh\necho attempt >> "$PGDATA/attempts"\n[ ! -f "$PGDATA/fail" ]\n'
    )
    for executable in fake_bin.iterdir():
        executable.chmod(0o755)
    dump = tmp_path / "backup.dump"
    dump.write_text("synthetic-backup")
    failure = tmp_path / "fail"
    failure.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PGDATA": str(tmp_path),
        "DUMP": str(dump),
        "PSQL_URL": "postgresql://localhost/cdi",
    }
    first = subprocess.run([bash, "-c", block], env=env, capture_output=True, text=True)
    assert first.returncode != 0
    marker = tmp_path / "cdi.restore-required"
    assert marker.exists() and (tmp_path / "database").exists()
    failure.unlink()
    second = subprocess.run([bash, "-c", block], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert not marker.exists()
    third = subprocess.run([bash, "-c", block], env=env, capture_output=True, text=True)
    assert third.returncode == 0
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt", "attempt"]
