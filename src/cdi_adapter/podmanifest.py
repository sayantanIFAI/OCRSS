"""Version manifest + drift detection for the pod bootstrap (backlog E1-S4; ARCHITECTURE §3.3).

``bootstrap_pod.sh`` records what one boot actually installed (apt packages, Python distributions,
the Go toolchain, the MinIO binary, the cached model revisions), then checks it against the pins in
``infra/runpod/versions.env`` and ``infra/runpod/requirements.lock``. A pin that does not hold is
drift: the bootstrap stops before the web app starts. Differences from the previous boot that no
pin covers (e.g. a base-image package RunPod updated) are logged as warnings, not failures.

    python -m cdi_adapter.podmanifest write --dir /workspace/logs/manifests \\
        --versions infra/runpod/versions.env --lock infra/runpod/requirements.lock
    python -m cdi_adapter.podmanifest compare boot1.json boot2.json   # 0 = identical versions

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

# keys that differ between two boots by nature, never compared
VOLATILE = ("generated_at", "host")

# our own distribution, installed editable from the checkout; not in the lock by design
OWN_DISTS = frozenset({"cdi-adapter"})

# supplied by the RunPod image (App. B) or the pinned cu128 install, never by the lock
TORCH_FAMILY = re.compile(r"^(torch|torchvision|torchaudio|triton|nvidia-.*)$")

_HF_MODEL_KEYS = ("CDI_VLM_MODEL_ID", "CDI_VLM_FALLBACK_MODEL_ID")


def normalize(name: str) -> str:
    """PEP 503 name normalisation, so ``PyYAML`` and ``pyyaml`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def base_version(version: str) -> str:
    """Drop a PEP 440 local label: ``2.8.0+cu128`` -> ``2.8.0``."""
    return version.split("+", 1)[0]


# ---------------------------------------------------------------- pin files


def parse_versions(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines of versions.env (comments and blanks ignored, quotes stripped)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"not a KEY=VALUE line: {raw!r}")
        # as bash reads it: "#" starts a comment only at the start of a word (FHIR_IG=id#version)
        lex = shlex.shlex(value, posix=True)
        lex.whitespace_split, lex.commenters = True, ""
        parts = []
        for token in lex:
            if token.startswith("#"):
                break
            parts.append(token)
        out[key] = " ".join(parts)
    return out


def apt_pins(versions: Mapping[str, str]) -> dict[str, str]:
    """``APT_PINS="pkg=ver ..."`` -> ``{pkg: ver}``."""
    pins: dict[str, str] = {}
    for item in versions.get("APT_PINS", "").split():
        pkg, sep, ver = item.partition("=")
        if not sep or not ver:
            raise ValueError(f"APT_PINS entry without a version: {item!r}")
        pins[pkg] = ver
    return pins


def parse_lock(text: str) -> dict[str, str]:
    """``name==version`` requirements of a pip hash lock -> ``{normalized name: version}``."""
    pins: dict[str, str] = {}
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if m:
            pins[normalize(m.group(1))] = m.group(2)
            continue
        # a wheel pinned by URL (+ hash), not on PyPI: its version is the wheel file's own
        u = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*) @ \S+/[A-Za-z0-9_.]+?-([^-/\s]+)-\S+\.whl",
                     line)  # fmt: skip
        if not u:
            raise ValueError(f"lock line is not an exact pin: {raw!r}")
        pins[normalize(u.group(1))] = u.group(2)
    return pins


# ---------------------------------------------------------------- collection


def _run(cmd: Sequence[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    r = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else None


def collect_apt() -> dict[str, str] | None:
    """Every installed dpkg package and version; None where there is no dpkg (not Debian)."""
    out = _run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}\t${Package}\t${Version}\n"])
    if out is None:
        return None
    pkgs: dict[str, str] = {}
    for line in out.splitlines():
        status, _, rest = line.partition("\t")
        name, _, version = rest.partition("\t")
        if status.startswith("ii") and name:
            pkgs[name] = version
    return pkgs


def collect_python(prefix: str | None = None) -> dict[str, dict[str, str]]:
    """Distributions visible to this interpreter, split into the venv's own and inherited ones."""
    root = Path(prefix or sys.prefix).resolve()
    venv: dict[str, str] = {}
    system: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        where = Path(str(dist.locate_file(""))).resolve()
        bucket = venv if where.is_relative_to(root) else system
        bucket.setdefault(normalize(name), dist.version)  # first on sys.path wins, as at import
    return {"venv": dict(sorted(venv.items())), "system": dict(sorted(system.items()))}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_go(ws: Path) -> str | None:
    """``go1.27.1`` from the toolchain's VERSION file (no exec needed)."""
    vfile = ws / "tools" / "go" / "VERSION"
    if not vfile.is_file():
        return None
    return vfile.read_text(encoding="utf-8").splitlines()[0].strip()


def collect_minio(ws: Path, tag: str) -> dict[str, str] | None:
    binary = ws / "tools" / f"minio-{tag}"
    if not binary.is_file():
        return None
    return {"tag": tag, "sha256": sha256_file(binary)}


def collect_models(hf_home: Path, model_ids: Iterable[str]) -> dict[str, str]:
    """The cached revision each configured model id resolves to (hub cache ``refs/main``)."""
    out: dict[str, str] = {}
    for model_id in model_ids:
        ref = hf_home / "hub" / f"models--{model_id.replace('/', '--')}" / "refs" / "main"
        if ref.is_file():
            out[model_id] = ref.read_text(encoding="utf-8").strip()
    return out


def collect_os() -> dict[str, str]:
    info: dict[str, str] = {"kernel": platform.release()}
    osr = Path("/etc/os-release")
    if osr.is_file():
        for line in osr.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key in ("ID", "VERSION_ID", "VERSION_CODENAME"):
                info[key.lower()] = value.strip().strip('"')
    return info


def collect(
    ws: Path,
    versions: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    hf_home = Path(env.get("HF_HOME") or ws / "hf-cache")
    model_ids = [env[k] for k in _HF_MODEL_KEYS if env.get(k)]
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "host": {
            "kind": "runpod" if env.get("RUNPOD_POD_ID") else "fallback",
            "id": env.get("RUNPOD_POD_ID") or platform.node(),
        },
        "os": collect_os(),
        "python": platform.python_version(),
        "apt": collect_apt(),
        "pip": collect_python(),
        "go": collect_go(ws),
        "minio": collect_minio(ws, versions.get("MINIO_TAG", "")),
        "models": collect_models(hf_home, model_ids),
    }


# ---------------------------------------------------------------- checks


def check_pins(
    manifest: Mapping[str, Any],
    versions: Mapping[str, str],
    lock: Mapping[str, str],
    *,
    torch_required: bool = False,
) -> list[str]:
    """Every pin that does not hold on this boot, as one human-readable line each."""
    drift: list[str] = []

    series = versions.get("PYTHON_SERIES")
    py = str(manifest.get("python", ""))
    if series and not (py == series or py.startswith(series + ".")):
        drift.append(f"python: {py} is not the pinned {series} series")

    apt = manifest.get("apt")
    pins = apt_pins(versions)
    if pins and apt is None:
        drift.append("apt: dpkg is not available; the pinned packages cannot be verified")
    elif apt is not None:
        for pkg, want in pins.items():
            have = apt.get(pkg)
            if have != want:
                drift.append(f"apt: {pkg} is {have or 'not installed'}, pinned {want}")

    pip = manifest.get("pip") or {}
    venv: Mapping[str, str] = pip.get("venv", {})
    system: Mapping[str, str] = pip.get("system", {})
    for name, want in lock.items():
        have = venv.get(name)
        if have != want:
            drift.append(f"pip: {name} is {have or 'not installed'} in the venv, locked {want}")
    for name, have in venv.items():
        if name not in lock and name not in OWN_DISTS and not TORCH_FAMILY.match(name):
            drift.append(f"pip: {name}=={have} is in the venv but not in requirements.lock")

    for key, dist in (("TORCH_VERSION", "torch"), ("TORCHVISION_VERSION", "torchvision")):
        pinned = versions.get(key)
        found = venv.get(dist) or system.get(dist)
        if not pinned:
            continue
        if found is None:
            if torch_required:
                drift.append(f"pip: {dist} is not installed, pinned {pinned} (hf backend)")
        elif base_version(found) != base_version(pinned):
            drift.append(f"pip: {dist} is {found}, pinned {pinned}")

    go_want = versions.get("GO_VERSION")
    go_have = manifest.get("go")
    if go_want and go_have is not None and go_have != f"go{go_want}":
        drift.append(f"go: toolchain is {go_have}, pinned go{go_want}")

    minio = manifest.get("minio")
    tag = versions.get("MINIO_TAG")
    if tag and minio is None:
        drift.append(f"minio: no binary built for the pinned {tag}")

    return drift


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, Mapping):
        flat: dict[str, Any] = {}
        for key, value in obj.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: obj}


def diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """Version differences between two manifests (volatile keys ignored), one line each."""
    fa = {k: v for k, v in _flatten(a).items() if k.split(".", 1)[0] not in VOLATILE}
    fb = {k: v for k, v in _flatten(b).items() if k.split(".", 1)[0] not in VOLATILE}
    lines: list[str] = []
    for key in sorted(fa.keys() | fb.keys()):
        if key not in fb:
            lines.append(f"- {key}: {fa[key]}")
        elif key not in fa:
            lines.append(f"+ {key}: {fb[key]}")
        elif fa[key] != fb[key]:
            lines.append(f"~ {key}: {fa[key]} -> {fb[key]}")
    return lines


# ---------------------------------------------------------------- CLI


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _dump(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_write(args: argparse.Namespace) -> int:
    versions = parse_versions(Path(args.versions).read_text(encoding="utf-8"))
    lock = parse_lock(Path(args.lock).read_text(encoding="utf-8"))
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)

    manifest = collect(Path(args.ws), versions)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"manifest.{stamp}.json"
    _dump(path, manifest)
    print(f"manifest: {path}")

    last = out / "last.json"
    if last.is_file():
        changes = diff(_load(last), manifest)
        for line in changes:
            print(f"WARNING: changed since the previous boot: {line}")
        if not changes:
            print("manifest: identical versions to the previous boot")
    _dump(last, manifest)

    backend = os.environ.get("CDI_MLSERVE_BACKEND", "stub")
    drift = check_pins(manifest, versions, lock, torch_required=backend == "hf")
    for line in drift:
        print(f"DRIFT: {line}")
    if drift:
        with (out / "drift.log").open("a", encoding="utf-8") as fh:
            for line in drift:
                fh.write(f"{stamp} {line}\n")
        return 1
    print("manifest: every pin holds")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    changes = diff(_load(Path(args.a)), _load(Path(args.b)))
    for line in changes:
        print(line)
    print("identical versions" if not changes else f"{len(changes)} difference(s)")
    return 0 if not changes else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.podmanifest", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="record this boot's versions and check the pins")
    w.add_argument("--dir", required=True, help="manifest directory (keeps last.json + drift.log)")
    w.add_argument("--versions", required=True, help="infra/runpod/versions.env")
    w.add_argument("--lock", required=True, help="infra/runpod/requirements.lock")
    w.add_argument("--ws", default=os.environ.get("WS", "/workspace"), help="workspace root")
    w.set_defaults(func=cmd_write)
    c = sub.add_parser("compare", help="exit 0 iff two manifests carry identical versions")
    c.add_argument("a")
    c.add_argument("b")
    c.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
