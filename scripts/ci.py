"""CI gate: lint + format + type check + unit tests (E1-S1) and the integration suite (E1-S2).

The same entry point runs locally and in `.github/workflows/ci.yml`, so a green local run means a
green CI run. Every stage runs even after a failure, so one run reports every red stage.

    python scripts/ci.py                        # the E1-S1 gate (suite "unit")
    python scripts/ci.py --suite integration    # E1-S2: needs Postgres + real MinIO, stub VLM
    python scripts/ci.py --only unit            # one stage
    python scripts/ci.py --list                 # show the stages
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


@dataclass(frozen=True)
class Stage:
    name: str
    cmd: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


# Tools run as `python -m <tool>`: Windows Smart App Control blocks pip's unsigned .exe launchers.
STAGES: tuple[Stage, ...] = (
    Stage("lint", (PY, "-m", "ruff", "check", "src", "tests", "db", "scripts")),
    Stage("format", (PY, "-m", "ruff", "format", "--check", "src", "tests", "db", "scripts")),
    Stage("types", (PY, "-m", "mypy", "src")),
    Stage("secrets", (PY, "scripts/secret_scan.py")),  # E1-S5: no plaintext credential
    Stage("unit", (PY, "-m", "pytest", "-m", "not integration", "-q", "-p", "no:cacheprovider")),
)

# E1-S2. Missing infra fails instead of skipping (CDI_TEST_REQUIRE_INFRA); stub VLM, no GPU.
INTEGRATION_STAGES: tuple[Stage, ...] = (
    Stage(
        "integration",
        (PY, "-m", "pytest", "-m", "integration", "-q", "-p", "no:cacheprovider"),
        (("CDI_TEST_REQUIRE_INFRA", "1"), ("CDI_MLSERVE_BACKEND", "stub")),
    ),
    Stage(
        "pipeline-audit",
        (PY, "scripts/ci_pipeline_audit.py"),
        (("CDI_MLSERVE_BACKEND", "stub"),),
    ),
    # E1-S6: the §8.2 golden path, S1 -> S9 on a fresh database, against the stub baseline
    Stage(
        "golden",
        (
            PY,
            "scripts/golden_path.py",
            "--mode",
            "full",
            "--baseline",
            "stub",
            "--fresh-db",
            "cdi_golden",
        ),
        (("CDI_MLSERVE_BACKEND", "stub"),),
    ),  # fmt: skip
)
SUITES = {"unit": STAGES, "integration": INTEGRATION_STAGES}


def run_stages(stages: Sequence[Stage], cwd: Path = ROOT) -> int:
    """Run every stage; return 0 only if all of them exit 0."""
    failed: list[str] = []
    for stage in stages:
        print(f"::group::{stage.name}", flush=True)
        t0 = time.monotonic()
        rc = subprocess.run(stage.cmd, cwd=cwd, env={**os.environ, **dict(stage.env)}).returncode
        print("::endgroup::", flush=True)
        verdict = "ok" if rc == 0 else f"FAILED (exit {rc})"
        print(f"[ci] {stage.name}: {verdict} in {time.monotonic() - t0:.1f}s", flush=True)
        if rc != 0:
            failed.append(stage.name)
    if failed:
        print(f"[ci] RED - failed stages: {', '.join(failed)}", flush=True)
        return 1
    print("[ci] GREEN - all stages passed", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--suite", choices=list(SUITES), default="unit")
    ap.add_argument("--only", action="append", choices=[s.name for v in SUITES.values() for s in v])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    suite = SUITES[args.suite]
    if args.list:
        for s in suite:
            print(f"{s.name}: {' '.join(s.cmd[1:])}")
        return 0
    stages = [s for s in suite if not args.only or s.name in args.only]
    if not stages:
        ap.error(f"--only matches no stage of suite {args.suite!r}")
    return run_stages(stages)


if __name__ == "__main__":
    sys.exit(main())
