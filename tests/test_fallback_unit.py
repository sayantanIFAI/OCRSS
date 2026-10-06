"""E1-S3: the native lifecycle scripts run on a non-RunPod fallback host (owner: no Docker)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.test_ops_unit import _bash

ROOT = Path(__file__).resolve().parents[1]
RUNPOD = ROOT / "infra" / "runpod"
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _lib(call: str, **env: str) -> str:
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    clean = {"PATH": __import__("os").environ["PATH"], **env}
    r = subprocess.run([bash, "-c", f". '{(RUNPOD / 'lib.sh').as_posix()}'; {call}"],
                       capture_output=True, text=True, env=clean)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_host_kind_detects_runpod_by_its_pod_id() -> None:
    assert _lib("host_kind", RUNPOD_POD_ID="g1a7lswmb5t79o") == "runpod"
    assert _lib("host_kind") == "fallback"


def test_live_url_on_the_pod_is_the_runpod_proxy() -> None:
    url = _lib("live_url 8888", RUNPOD_POD_ID="g1a7lswmb5t79o")
    assert url == "https://g1a7lswmb5t79o-8888.proxy.runpod.net"


def test_live_url_on_a_fallback_host_is_the_host_address() -> None:
    assert _lib("live_url 8888", CDI_PUBLIC_HOST="10.20.0.5") == "http://10.20.0.5:8888"
    assert re.fullmatch(r"http://[\w.:-]+:8888", _lib("live_url 8888"))


def test_bootstrap_installs_the_appendix_b_torch_only_where_it_is_missing() -> None:
    boot = (RUNPOD / "bootstrap_pod.sh").read_text(encoding="utf-8")
    torch, vision = re.search(
        r"torch / torchvision \| ([\d.]+)\+cu128 / ([\d.]+)\+cu128", ARCH
    ).groups()  # type: ignore[union-attr]  # noqa: E501
    assert f"TORCH_VERSION=${{TORCH_VERSION:-{torch}}}" in boot
    assert f"TORCHVISION_VERSION=${{TORCHVISION_VERSION:-{vision}}}" in boot
    guard = next(ln for ln in boot.splitlines() if "import torch" in ln)
    assert '"${CDI_MLSERVE_BACKEND:-stub}" = "hf"' in guard and "! " in guard
    assert "download.pytorch.org/whl/cu128" in boot


def test_start_all_uses_the_shared_url_and_warns_on_a_fallback_host() -> None:
    start = (RUNPOD / "start_all.sh").read_text(encoding="utf-8")
    assert '. "$HERE/lib.sh"' in start and 'live_url "$PORT"' in start
    assert "proxy.runpod.net" not in start  # the RunPod URL lives in lib.sh only
    tail = start[start.index('if [ "$(host_kind)" = fallback ]') :]
    assert "WARNING: fallback host" in tail.split("\nfi")[0]


def test_no_docker_in_the_lifecycle_scripts() -> None:
    for sh in RUNPOD.glob("*.sh"):
        body = [
            ln
            for ln in sh.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        ]
        assert not any(re.search(r"\bdocker\b", ln, re.I) for ln in body), sh.name


def test_runbook_covers_data_start_lock_and_verify() -> None:
    book = (ROOT / "docs" / "fallback-host.md").read_text(encoding="utf-8")
    for must in ("/workspace/backup/cdi.dump", "/workspace/minio-data/", "/workspace/secrets/",
                 "start_all.sh", "port 8888", "golden_path.py", "no Docker"):  # fmt: skip
        assert must in book, must
