"""Pinned ABDM IG + HL7 validator package cache: seed, stage, promote (backlog E6-S1, E6-S5).

    # every boot / new host (E6-S1): exactly the pinned package set, then prove it
    python scripts/fhir_ig.py seed --home /workspace/tools/fhir-home

    # IG upgrade (E6-S5, runbook docs/ig-upgrade.md)
    python scripts/fhir_ig.py stage   --ig ndhm.in#6.6.0 --home /workspace/tools/fhir-staging
    python scripts/fhir_ig.py promote --ig ndhm.in#6.6.0 --home /workspace/tools/fhir-staging

``seed`` downloads each ``id#version`` in ``infra/fhir/packages.lock`` from the FHIR package
registry into ``<home>/.fhir/packages`` and checks every digest: no resolver, no "latest".

``stage`` builds a separate cache for a candidate IG: it lets the validator (online, this once)
pull whatever the candidate needs, then validates the five E6-S4 golden bundles against it,
offline, and writes ``<home>/stage-report.json``: it passes when no golden gains an error beyond
the pinned IG's baseline (tests/golden/fhir/validation-baseline.json). ``promote`` refuses unless
that report passed
for exactly this IG and the staged cache is unchanged since; then it rewrites the pins
(``infra/fhir/packages.lock``, ``FHIR_IG``/``FHIR_IG_SHA256`` in ``infra/runpod/versions.env``,
``CDI_IG_PACKAGE`` in ``.env.runpod`` / ``.env.example``). Production changes only when those pins
are deployed and the pod boots with them.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cdi_adapter.fhir import package_cache as pc  # noqa: E402
from cdi_adapter.fhir.validate_abdm import hapi_command, ig_tarball  # noqa: E402

LOCK = ROOT / "infra" / "fhir" / "packages.lock"
VERSIONS = ROOT / "infra" / "runpod" / "versions.env"
ENV_FILES = (ROOT / ".env.runpod", ROOT / ".env.example")
REGISTRY = "https://packages.fhir.org"


# ---------------------------------------------------------------- registry + cache install


def fetch(pkg: str, version: str) -> bytes:
    url = f"{REGISTRY}/{pkg}/{version}"
    with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310 - fixed https registry
        data: bytes = resp.read()
    return data


def install(tgz: bytes, home: Path, name: str) -> Path:
    """Unpack a package tarball as ``<home>/.fhir/packages/<id#version>/package/...``."""
    base = pc.packages_dir(home)
    base.mkdir(parents=True, exist_ok=True)
    ini = base / "packages.ini"
    if not ini.exists():
        ini.write_text("[cache]\nversion = 4\n", encoding="utf-8")
    dest, tmp = base / name, base / f".{name}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            parts = Path(m.name).parts
            if ".." in parts or Path(m.name).is_absolute():
                raise ValueError(f"unsafe path in {name}: {m.name}")
            target = tmp.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None
            target.write_bytes(src.read())
    if not (tmp / "package" / "package.json").is_file():
        raise ValueError(f"{name}: not a FHIR package (no package/package.json)")
    shutil.rmtree(dest, ignore_errors=True)
    for attempt in range(30):  # Windows: a virus scanner may still hold the new files open
        try:
            tmp.rename(dest)
            break
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(1)
    return dest


def seed(home: Path, lock: Path = LOCK) -> list[str]:
    """Install exactly the locked set, then return the drift (empty = exactly pinned)."""
    pinned = pc.read_lock(lock)
    have = pc.manifest(home)
    for name, digest in sorted(pinned.items()):
        if have.get(name) == digest:
            continue
        pkg, _, version = name.partition("#")
        print(f"fhir: installing {name}")
        install(fetch(pkg, version), home, name)
    return pc.check(pc.manifest(home), pinned)


def put_ig(ig: str, home: Path, sha256: str | None = None) -> Path:
    """The IG tarball at ``<home>/ig/<id>-<version>.tgz``, checksum-verified when pinned."""
    path = ig_tarball(ig, home)
    if not path.is_file() or (sha256 and sha256_bytes(path.read_bytes()) != sha256):
        pkg, _, version = ig.partition("#")
        data = fetch(pkg, version)
        if sha256 and sha256_bytes(data) != sha256:
            raise SystemExit(f"DRIFT: {ig} tarball sha256 {sha256_bytes(data)}, pinned {sha256}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- pins in the repo


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"')
    return out


def set_env(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
    if n != 1:
        raise SystemExit(f"{path.name}: expected exactly one {key}= line, found {n}")
    path.write_text(new, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- stage / promote (E6-S5)


def run_validator(files: list[Path], home: Path, ig: str, *, java: str, jar: str,
                  online: bool) -> dict[str, Any]:  # fmt: skip
    """The real validator. ``online`` only while staging a candidate (to discover its packages)."""
    from cdi_adapter.fhir.validate_abdm import parse_outcomes

    with tempfile.TemporaryDirectory() as tmp:
        oo = Path(tmp) / "outcome.json"
        cmd = hapi_command(files, oo, ig=ig, package_home=home, java=java, jar=jar)
        if online:
            cmd = [c for c in cmd if not c.startswith(("-Dhttp.proxy", "-Dhttps.proxy"))
                   and c != "-no-http-access"]  # fmt: skip
        run = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if not oo.is_file():
            raise SystemExit(f"validator produced no output (exit {run.returncode}):\n"
                             f"{run.stdout[-1500:]}{run.stderr[-800:]}")  # fmt: skip
        return parse_outcomes(json.loads(oo.read_text(encoding="utf-8")), files)


def stage(ig: str, home: Path, *, java: str, jar: str) -> dict[str, Any]:
    import fhir_golden

    tarball = put_ig(ig, home)
    files = [fhir_golden.path_for(a) for a in fhir_golden.ARTIFACTS]
    # 1. online, once: the validator installs whatever this IG version needs into the staging cache
    run_validator(files[:1], home, ig, java=java, jar=jar, online=True)
    # 1b. ...from wherever it likes (FHIR core came from the build server, not the registry), so
    #     every discovered package is reinstalled from the registry: the staged set is then exactly
    #     what `seed` reproduces on a pod from the lock
    for name in pc.manifest(home):
        pkg, _, version = name.partition("#")
        install(fetch(pkg, version), home, name)
    # 2. offline: the verdict, exactly as production will run it
    per_file = run_validator(files, home, ig, java=java, jar=jar, online=False)
    # the gate: no golden may gain an error the pinned IG's baseline does not already carry
    found = fhir_golden.error_signatures(per_file)
    new, fixed = fhir_golden.compare(found, fhir_golden.read_baseline()["errors"])
    results = {a: {"errors": len(found[a]), "new_errors": new.get(a, []),
                   "fixed_errors": fixed.get(a, [])} for a in fhir_golden.ARTIFACTS}  # fmt: skip
    report = {
        "ig": ig,
        "ig_sha256": sha256_bytes(tarball.read_bytes()),
        "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "validator_jar_sha256": sha256_bytes(Path(jar).read_bytes()),
        "goldens_match_builders": not fhir_golden.check(),
        "results": results,
        "packages": pc.manifest(home),
    }
    report["passed"] = report["goldens_match_builders"] and not new
    (home / "stage-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def promote(ig: str, home: Path, *, validator: str, dry_run: bool = False) -> list[str]:
    """Rewrite every pin to the staged IG, or refuse. Returns the changes (dry run: only those)."""
    report_path = home / "stage-report.json"
    if not report_path.is_file():
        raise SystemExit(f"refused: no stage report in {home}; run `stage --ig {ig}` first")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("ig") != ig:
        raise SystemExit(f"refused: the staged IG is {report.get('ig')}, not {ig}")
    if not report.get("passed"):
        failing = [a for a, r in report["results"].items() if r["new_errors"]]
        why = f"new errors in {failing}" if failing else "goldens no longer match the builders"
        raise SystemExit(f"refused: {ig} failed staging ({why})")
    drift = pc.check(pc.manifest(home), report["packages"])
    if drift:
        raise SystemExit(f"refused: the staging cache changed after staging: {drift}")
    pinned = pc.read_lock(LOCK)
    changes = [f"packages.lock: {line}" for line in pc.check(report["packages"], pinned)]
    changes += [
        f"versions.env: FHIR_IG={ig}",
        f"versions.env: FHIR_IG_SHA256={report['ig_sha256']}",
    ]
    changes += [f"{env.name}: CDI_IG_PACKAGE={ig}" for env in ENV_FILES]
    if dry_run:
        return changes
    pc.write_lock(LOCK, report["packages"], ig=ig, validator=validator)
    set_env(VERSIONS, "FHIR_IG", ig)
    set_env(VERSIONS, "FHIR_IG_SHA256", report["ig_sha256"])
    for env in ENV_FILES:
        set_env(env, "CDI_IG_PACKAGE", ig)
    return changes


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="install exactly the pinned packages and prove it")
    s.add_argument("--home", type=Path, required=True)
    for name in ("stage", "promote"):
        p = sub.add_parser(name)
        p.add_argument("--ig", required=True, help="candidate IG as id#version")
        p.add_argument("--home", type=Path, required=True, help="a staging package home")
        if name == "promote":
            p.add_argument("--dry-run", action="store_true", help="check + list changes only")
        if name == "stage":
            p.add_argument("--java", default="java")
            p.add_argument("--jar", required=True, help="the pinned validator_cli.jar")
    args = ap.parse_args(argv)
    pins = read_env(VERSIONS)

    if args.cmd == "seed":
        put_ig(pins["FHIR_IG"], args.home, pins["FHIR_IG_SHA256"])
        drift = seed(args.home)
        for line in drift:
            print(f"DRIFT: fhir package {line}")
        print("fhir packages: exactly the pinned set" if not drift else "fhir packages: drift")
        return 1 if drift else 0
    if args.cmd == "stage":
        from cdi_adapter.config import get_settings

        if Path(args.home).resolve() == Path(get_settings().fhir_package_home).resolve():
            raise SystemExit("refused: stage into a separate staging home, never production")
        report = stage(args.ig, args.home, java=args.java, jar=args.jar)
        for a, r in report["results"].items():
            print(
                f"{'FAIL' if r['new_errors'] else 'ok  '} {a}: {r['errors']} error(s), "
                f"{len(r['new_errors'])} new, {len(r['fixed_errors'])} fixed vs the baseline"
            )
        print(
            f"staged {args.ig}: {'PASSED' if report['passed'] else 'FAILED'} "
            f"({len(report['packages'])} packages) -> {args.home / 'stage-report.json'}"
        )
        return 0 if report["passed"] else 1
    changes = promote(
        args.ig, args.home, validator=pins["FHIR_VALIDATOR_VERSION"], dry_run=args.dry_run
    )
    if args.dry_run:
        print(f"dry run: {args.ig} may be promoted; nothing written. It would change:")
        for line in changes:
            print(f"  {line}")
        return 0
    print(f"promoted {args.ig}: infra/fhir/packages.lock, versions.env (FHIR_IG, FHIR_IG_SHA256),")
    print("  .env.runpod + .env.example (CDI_IG_PACKAGE) rewritten. Next: update the")
    print("  config.py default + ARCHITECTURE §9, run scripts/ci.py, deploy, reboot the pod.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
