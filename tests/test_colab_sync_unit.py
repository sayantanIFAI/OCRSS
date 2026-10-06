"""Verification-host tooling (owner, 2026-09-25): the host runs exactly the verified local tree
(scripts/dev/colab_bundle.py -> infra/colab/sync.sh), and is reached at a standalone test URL
(infra/colab/start_test_tunnel.sh) instead of the Colab window's proxy."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cdi_adapter.podmanifest import parse_versions
from tests.test_ops_unit import _bash

ROOT = Path(__file__).resolve().parents[1]
COLAB = ROOT / "infra" / "colab"
SYNC = COLAB / "sync.sh"
TUNNEL = COLAB / "start_test_tunnel.sh"
PINS = parse_versions((ROOT / "infra" / "runpod" / "versions.env").read_text(encoding="utf-8"))

sys.path.insert(0, str(ROOT / "scripts" / "dev"))
import colab_bundle  # noqa: E402


def _need_bash() -> str:
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    return bash


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------- scripts + pins


@pytest.mark.parametrize("script", [SYNC, TUNNEL])
def test_scripts_are_valid_native_bash(script: Path) -> None:
    r = subprocess.run([_need_bash(), "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert b"\r" not in script.read_bytes()
    assert not re.search(r"\bdocker\b", _code(text), re.I)  # owner decision: no Docker


def test_the_test_tunnel_is_pinned_verified_and_test_only() -> None:
    assert re.fullmatch(r"\d{4}\.\d+\.\d+", PINS["CLOUDFLARED_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", PINS["CLOUDFLARED_SHA256"])
    text = TUNNEL.read_text(encoding="utf-8")
    code = _code(text)
    assert "releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64" in code
    assert 'echo "$CLOUDFLARED_SHA256  $BIN.partial" | sha256sum -c' in code
    assert "--no-autoupdate" in code  # the pinned binary stays the pinned binary
    # the tunnel decrypts at Cloudflare: never the production path (E1-S9 ingress is)
    assert "TEST HOSTS ONLY" in text and "infra/runpod/start_tunnel.sh" in text
    assert "start_test_tunnel" not in (ROOT / "infra/runpod/start_all.sh").read_text("utf-8")
    # the site's same-origin check must allow the tunnel host, and the site must be restarted
    assert "CDI_PUBLIC_ORIGINS=" in code and "start_apptier.sh" in code


# ---------------------------------------------------------------- colab_bundle.py


def _tree(root: Path, files: dict[str, bytes]) -> list[str]:
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return sorted(files)


def test_bundle_carries_every_file_and_a_manifest_of_their_hashes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    files = _tree(src, {"a.py": b"print(1)\n", "docs/backlog 1.md": b"# x\n",
                        "img.png": b"\x89PNG\r\n"})  # fmt: skip
    out = tmp_path / "cdi-src.tar.gz"
    bundle_id = colab_bundle.build(src, files, out)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        manifest = tar.extractfile(colab_bundle.MANIFEST).read().decode()  # type: ignore[union-attr]
    assert sorted(n for n in names if n != colab_bundle.MANIFEST) == files
    lines = dict(reversed(ln.split("  ", 1)) for ln in manifest.splitlines())  # path -> hash
    assert lines == {f: hashlib.sha256((src / f).read_bytes()).hexdigest() for f in files}
    assert bundle_id == hashlib.sha256(manifest.encode()).hexdigest()[:12]


def test_bundle_refuses_crlf_text_but_not_binaries(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"ok.sh": b"echo ok\n", "bad.sh": b"echo bad\r\n", "img.png": b"\r\n",
                             "blob.bin": b"\0\r\n"})  # fmt: skip
    assert colab_bundle.crlf_files(tmp_path, files) == ["bad.sh"]


# ---------------------------------------------------------------- sync.sh (run for real)

FAKE_PY = ('#!/usr/bin/env bash\ncase "$*" in *"alembic current"*) echo "0008 (head)";; '
           '*) echo "py $*" >>"$WS/py.calls";; esac\n')  # fmt: skip
FAKE_APPTIER = '#!/usr/bin/env bash\necho "apptier ${1:-start}" >>"$WS/apptier.calls"\n'


def _sync(repo: Path, ws: Path, arc: Path) -> subprocess.CompletedProcess[str]:
    run = ('to() { if command -v cygpath >/dev/null; then cygpath -u "$1"; else echo "$1"; fi; }; '
           'REPO=$(to "$R") WS=$(to "$W") exec bash "$(to "$S")" "$(to "$A")"')  # fmt: skip
    # The sync script stops its model gateway. A temporary fixture must never run
    # pkill against real host processes (including a parent shell running the tests).
    fake_bin = ws / "test-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    pkill = fake_bin / "pkill"
    pkill.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$W/pkill.calls"\nexit 1\n')
    pkill.chmod(0o755)
    pgrep = fake_bin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 0\n")  # synthetic running gateway, independent of host state
    pgrep.chmod(0o755)
    env = {**os.environ, "R": str(repo), "W": str(ws), "S": str(SYNC), "A": str(arc),
           "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}  # fmt: skip
    return subprocess.run([_need_bash(), "-c", run], capture_output=True, text=True, env=env,
                          timeout=120)  # fmt: skip


def _host(tmp_path: Path) -> tuple[Path, Path]:
    repo, ws = tmp_path / "repo", tmp_path / "ws"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text(FAKE_PY, encoding="utf-8", newline="\n")
    (repo / ".venv" / "bin" / "python").chmod(0o755)
    (repo / ".env").write_bytes(b"CDI_SECRET_THING=keep\n")
    (repo / "data").mkdir()
    (repo / "data" / "local.txt").write_bytes(b"host data\n")
    (ws / "run").mkdir(parents=True)
    (ws / "logs").mkdir()
    return repo, ws


def _bundle(tmp_path: Path, name: str, files: dict[str, bytes]) -> tuple[Path, str]:
    src = tmp_path / name
    tree = _tree(
        src,
        {
            "infra/runpod/start_apptier.sh": FAKE_APPTIER.encode(),
            "infra/runpod/start_mlserve.sh": b'#!/bin/sh\necho restart >> "$WS/gateway.calls"\n',
            **files,
        },
    )
    out = tmp_path / f"{name}.tar.gz"
    return out, colab_bundle.build(src, tree, out)


def test_sync_applies_exactly_the_bundle_and_keeps_host_state(tmp_path: Path) -> None:
    repo, ws = _host(tmp_path)
    arc1, id1 = _bundle(tmp_path, "v1", {"a.py": b"one\n", "docs/backlog 1.md": b"# v1\n",
                                         "gone.py": b"old\n"})  # fmt: skip
    r = _sync(repo, ws, arc1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"SYNCED: bundle {id1}" in r.stdout and "database 0008 (head)" in r.stdout
    assert (repo / ".bundle_id").read_text().strip() == id1
    assert (repo / "docs" / "backlog 1.md").read_bytes() == b"# v1\n"
    assert "py -m alembic upgrade head" in (ws / "py.calls").read_text()
    assert "apptier start" in (ws / "apptier.calls").read_text()  # web + workers restarted
    assert "-f cdi_adapter.mlserve" in (ws / "pkill.calls").read_text()
    assert "restart" in (ws / "gateway.calls").read_text()

    arc2, id2 = _bundle(tmp_path, "v2", {"a.py": b"two\n", "docs/backlog 1.md": b"# v2\n"})
    r = _sync(repo, ws, arc2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "removed gone.py" in r.stdout and not (repo / "gone.py").exists()
    assert (repo / "a.py").read_bytes() == b"two\n"
    assert (repo / ".bundle_id").read_text().strip() == id2
    # never in a bundle, never touched: the host's .env, its data, its venv
    assert (repo / ".env").read_bytes() == b"CDI_SECRET_THING=keep\n"
    assert (repo / "data" / "local.txt").exists() and (repo / ".venv" / "bin" / "python").exists()


def test_sync_refuses_a_damaged_bundle_and_changes_nothing(tmp_path: Path) -> None:
    repo, ws = _host(tmp_path)
    arc, _ = _bundle(tmp_path, "v1", {"a.py": b"one\n"})
    assert _sync(repo, ws, arc).returncode == 0
    src = tmp_path / "bad"
    _tree(src, {"a.py": b"tampered\n"})
    bad = tmp_path / "bad.tar.gz"
    manifest = f"{hashlib.sha256(b'one' + bytes([10])).hexdigest()}  a.py\n".encode()
    (src / colab_bundle.MANIFEST).write_bytes(manifest)
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(src / "a.py", arcname="a.py")
        tar.add(src / colab_bundle.MANIFEST, arcname=colab_bundle.MANIFEST)
    r = _sync(repo, ws, bad)
    assert r.returncode == 1 and "bundle damaged" in r.stdout
    assert (repo / "a.py").read_bytes() == b"one\n"


def test_sync_refuses_a_host_without_its_setup(tmp_path: Path) -> None:
    arc, _ = _bundle(tmp_path, "v1", {"a.py": b"one\n"})
    (tmp_path / "empty").mkdir()
    r = _sync(tmp_path / "empty", tmp_path / "ws", arc)
    assert r.returncode == 1 and "run the notebook's setup steps first" in r.stdout
