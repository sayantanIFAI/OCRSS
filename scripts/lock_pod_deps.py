"""Regenerate infra/runpod/requirements.lock (backlog E1-S4).

The pod runs Ubuntu 24.04 / CPython 3.12 / x86_64 (ARCHITECTURE App. B). This resolves
``pyproject.toml``'s runtime dependencies plus the ``ocr`` and ``ml`` extras for that target from
any machine, with the constraints in ``infra/runpod/versions.env`` (torch, torchvision,
transformers), and writes one exact pin per package with the sha256 of every compatible wheel.

The torch family (torch, torchvision, triton, nvidia-*) is left out: the RunPod image supplies it
(``venv --system-site-packages``) and a fallback host installs the pinned cu128 build. It is still
resolved, so everything else in the lock is compatible with it.

    python scripts/lock_pod_deps.py            # rewrite the lock
    python scripts/lock_pod_deps.py --check    # exit 1 if a fresh resolve differs (no write)

Needs network access to PyPI. Wheels only (``--only-binary=:all:``): nothing is built on the pod.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cdi_adapter.podmanifest import TORCH_FAMILY, normalize, parse_versions  # noqa: E402

LOCK = ROOT / "infra" / "runpod" / "requirements.lock"
VLLM_LOCK = ROOT / "infra" / "runpod" / "requirements-vllm.lock"  # backlog E2-S1
QUANT_LOCK = ROOT / "infra" / "runpod" / "requirements-quant.lock"  # backlog E2-S6
VERSIONS = ROOT / "infra" / "runpod" / "versions.env"

PYTHON = (3, 12)
# glibc 2.39 on Ubuntu 24.04: every manylinux level up to it, best first (pip's own ranking)
PLATFORMS = (
    [f"manylinux_2_{v}_x86_64" for v in range(39, 16, -1)]
    + ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"]
)  # fmt: skip

# PEP 508 marker values of the pod. pip evaluates markers against the machine it runs on, not
# against --platform, so a resolve on Windows would otherwise pick up Windows-only dependencies.
LINUX_MARKERS = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.3",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.3",
    "platform_python_implementation": "CPython",
    "python_version": "3.12",
    "sys_platform": "linux",
}

# the venv's own tooling: pinned too, so `pip install --no-build-isolation -e .` is reproducible
TOOLING = ("pip", "setuptools", "wheel")

# run pip in a child interpreter with the pod's marker environment patched in
_PIP_SHIM = """
import json, sys
from pip._vendor.packaging import markers
env = json.loads(sys.argv[1])
markers.default_environment = lambda: dict(env)
from pip._internal.cli.main import main
sys.exit(main(sys.argv[2:]))
"""


def requirements(pyproject: Mapping[str, Any]) -> list[str]:
    project = pyproject["project"]
    extras = project.get("optional-dependencies", {})
    return [*project["dependencies"], *extras.get("ocr", []), *extras.get("ml", []),
            *extras.get("deid", []), *TOOLING]  # fmt: skip


def vllm_requirements(pyproject: Mapping[str, Any]) -> list[str]:
    """The model gateway's own venv (backlog E2-S1): only what ``cdi_adapter.mlserve`` imports,
    plus vLLM. Kept apart because vLLM on torch 2.8 needs transformers < 5 (see versions.env)."""
    extras = pyproject["project"].get("optional-dependencies", {})
    return [*extras.get("vllm", []), *TOOLING]


def quant_requirements(pyproject: Mapping[str, Any]) -> list[str]:
    """The offline quantization venv (backlog E2-S6): llm-compressor on torch 2.8, which caps
    transformers at 4.55.2 (see versions.env)."""
    extras = pyproject["project"].get("optional-dependencies", {})
    return [*extras.get("quant", []), *TOOLING]


def constraints(versions: Mapping[str, str]) -> list[str]:
    pins = (
        ("torch", "TORCH_VERSION"),
        ("torchvision", "TORCHVISION_VERSION"),
        ("transformers", "TRANSFORMERS_VERSION"),
    )
    # PyPI carries no local labels: "2.8.0+cu128" resolves as "2.8.0"
    return [f"{dist}=={versions[key].split('+', 1)[0]}" for dist, key in pins if versions.get(key)]


def resolve(reqs: Sequence[str], cons: Sequence[str]) -> list[dict[str, Any]]:
    """pip's resolution for the pod target: ``[{name, version, url, sha256}]``."""
    with tempfile.TemporaryDirectory() as tmp:
        req_in = Path(tmp) / "req.in"
        con_in = Path(tmp) / "cons.txt"
        report = Path(tmp) / "report.json"
        req_in.write_text("\n".join(reqs) + "\n", encoding="utf-8")
        con_in.write_text("\n".join(cons) + "\n", encoding="utf-8")
        cmd = [sys.executable, "-c", _PIP_SHIM, json.dumps(LINUX_MARKERS),
               "install", "--dry-run", "--ignore-installed", "--quiet", "--report", str(report),
               "--python-version", ".".join(map(str, PYTHON)), "--implementation", "cp",
               "--abi", f"cp{PYTHON[0]}{PYTHON[1]}", "--only-binary=:all:",
               "-r", str(req_in), "-c", str(con_in)]  # fmt: skip
        for plat in PLATFORMS:
            cmd += ["--platform", plat]
        subprocess.run(cmd, check=True)
        data = json.loads(report.read_text(encoding="utf-8"))
    out = []
    for item in data["install"]:
        info = item["download_info"]
        out.append(
            {
                "name": item["metadata"]["name"],
                "version": item["metadata"]["version"],
                "url": info["url"],
                "sha256": info.get("archive_info", {}).get("hashes", {}).get("sha256"),
            }
        )
    return out


def supported_tags() -> set[str]:
    """``py-abi-platform`` triples pip on the pod accepts."""
    from pip._vendor.packaging import tags

    abi = f"cp{PYTHON[0]}{PYTHON[1]}"
    found = {
        str(t) for t in tags.cpython_tags(python_version=PYTHON, abis=[abi], platforms=PLATFORMS)
    }
    found |= {
        str(t)
        for t in tags.compatible_tags(python_version=PYTHON, interpreter=abi, platforms=PLATFORMS)
    }
    return found


def compatible_hashes(files: Iterable[Mapping[str, Any]], accepted: set[str]) -> list[str]:
    """sha256 of every wheel in a PyPI release that the pod could pick, sorted."""
    from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename

    out = set()
    for f in files:
        name = f.get("filename", "")
        if not name.endswith(".whl") or f.get("yanked"):
            continue
        try:
            _, _, _, wheel_tags = parse_wheel_filename(name)
        except InvalidWheelFilename:
            continue
        if any(str(t) in accepted for t in wheel_tags):
            out.add(f["digests"]["sha256"])
    return sorted(out)


def pypi_files(name: str, version: str) -> list[dict[str, Any]]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https host
        urls: list[dict[str, Any]] = json.load(resp)["urls"]
    return urls


def lock_entries(resolved: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, list[str]]]:
    accepted = supported_tags()
    entries = []
    for item in resolved:
        name = normalize(item["name"])
        if TORCH_FAMILY.match(name):
            continue
        hashes = compatible_hashes(pypi_files(name, item["version"]), accepted)
        if item.get("sha256") and item["sha256"] not in hashes:
            hashes = sorted({*hashes, item["sha256"]})
        if not hashes:
            raise SystemExit(f"no compatible wheel hash for {name}=={item['version']}")
        entries.append((name, item["version"], hashes))
    return sorted(entries)


def direct_wheels(versions: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """Wheels not on PyPI, pinned by URL + sha256 in versions.env: (name, url, sha256)."""
    url, sha = versions.get("SPACY_MODEL_WHEEL"), versions.get("SPACY_MODEL_SHA256")
    if not url or not sha:
        return []
    name = normalize(url.rsplit("/", 1)[1].split("-", 1)[0])
    return [(name, url, sha)]


def render(entries: Iterable[tuple[str, str, list[str]]], cons: Sequence[str],
           direct: Iterable[tuple[str, str, str]] = ()) -> str:  # fmt: skip
    lines = [
        "# GENERATED by scripts/lock_pod_deps.py - do not edit by hand (backlog E1-S4).",
        "# Target: Ubuntu 24.04, CPython 3.12, x86_64. Installed by bootstrap_pod.sh with",
        "#   pip install --require-hashes --no-deps -r requirements.lock",
        "# Resolved with: " + ", ".join(cons),
        "# Not in this file: torch / torchvision / triton / nvidia-* (RunPod image or the pinned",
        "# cu128 build; see versions.env).",
        "",
    ]
    for name, version, hashes in entries:
        lines.append(f"{name}=={version} \\")
        for i, h in enumerate(hashes):
            lines.append(f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else ""))
    for name, url, sha in direct:  # not on PyPI: that exact file, by URL + hash (E11-S4)
        lines.append(f"{name} @ {url} \\")
        lines.append(f"    --hash=sha256:{sha}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare only; exit 1 on a difference")
    ap.add_argument(
        "--target",
        choices=("main", "vllm", "quant"),
        default="main",
        help="main: the pipeline venv; vllm: the vLLM gateway venv (E2-S1); "
        "quant: the AWQ quantization venv (E2-S6)",
    )
    args = ap.parse_args(argv)

    versions = parse_versions(VERSIONS.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if args.target == "vllm":
        lock, cmd = VLLM_LOCK, "python scripts/lock_pod_deps.py --target vllm"
        cons = [c for c in constraints(versions) if not c.startswith("transformers==")]
        cons.append(f"transformers=={versions['VLLM_TRANSFORMERS_VERSION']}")
        entries = lock_entries(resolve(vllm_requirements(pyproject), cons))
        text = render(entries, cons)
    elif args.target == "quant":
        lock, cmd = QUANT_LOCK, "python scripts/lock_pod_deps.py --target quant"
        cons = [c for c in constraints(versions) if not c.startswith("transformers==")]
        cons.append(f"transformers=={versions['QUANT_TRANSFORMERS_VERSION']}")
        entries = lock_entries(resolve(quant_requirements(pyproject), cons))
        text = render(entries, cons)
    else:
        lock, cmd = LOCK, "python scripts/lock_pod_deps.py"
        cons = constraints(versions)
        entries = lock_entries(resolve(requirements(pyproject), cons))
        text = render(entries, cons, direct_wheels(versions))

    if args.check:
        current = lock.read_text(encoding="utf-8") if lock.exists() else ""
        if current != text:
            print(f"{lock.relative_to(ROOT)} is stale: run {cmd}")
            return 1
        print(f"{lock.relative_to(ROOT)} is current")
        return 0
    lock.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {lock.relative_to(ROOT)} ({len(entries)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
