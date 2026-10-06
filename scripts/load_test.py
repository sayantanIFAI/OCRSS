"""Run the E1-S7 load test against any environment (ADR-0002: 30 patients x 5-10 documents).

    python scripts/load_test.py --host https://emr.example.in                 # 30 patients
    python scripts/load_test.py --host http://127.0.0.1:8888 --patients 3 --docs-max 5
    python scripts/load_test.py --host ... --enforce-sla      # exit 1 if p95 misses the SLA

All patients are spawned at once (the design burst), each uploads all of its documents in one job,
and the run stops by itself when the last job is done. The report (exact p50/p95/p99, histogram,
SLA verdict, every sample) goes to --report(.json/.txt). Extra arguments are passed to Locust.

Behind the E1-S9 ingress the UI is clinic-only: run it from an allowlisted network. Every patient
signs in as ``--user`` (an intake account, E11-S1) with the password in ``CDI_LOAD_PASSWORD``.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCUSTFILE = HERE / "loadtest" / "locustfile.py"


def command(args: argparse.Namespace, extra: Sequence[str] = ()) -> list[str]:
    # Locust's own run-time limit is only a backstop: the test quits when every job is done
    backstop = int(args.job_timeout_s + 300)
    return [sys.executable, "-m", "locust", "-f", str(LOCUSTFILE), "--headless", "--only-summary",
            "--host", args.host, "-u", str(args.patients), "-r", str(args.patients),
            "--run-time", f"{backstop}s", "--stop-timeout", "10",
            "--cdi-docs-min", str(args.docs_min), "--cdi-docs-max", str(args.docs_max),
            "--cdi-seed", str(args.seed), "--cdi-sla-s", str(args.sla_s),
            "--cdi-poll-s", str(args.poll_s), "--cdi-job-timeout-s", str(args.job_timeout_s),
            "--cdi-report", args.report,
            *(["--cdi-enforce-sla"] if args.enforce_sla else []),
            *(["--cdi-user", args.user] if getattr(args, "user", "") else []), *extra]  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True, help="environment base URL")
    ap.add_argument("--patients", type=int, default=30, help="concurrent patients (default 30)")
    ap.add_argument("--docs-min", type=int, default=5)
    ap.add_argument("--docs-max", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sla-s", type=float, default=300.0, help="SLA in seconds (ADR-0002: 300)")
    ap.add_argument("--poll-s", type=float, default=2.0)
    ap.add_argument("--job-timeout-s", type=float, default=1800.0)
    ap.add_argument("--report", default="load_report", help="report path prefix")
    ap.add_argument("--enforce-sla", action="store_true")
    ap.add_argument(
        "--user",
        default="",
        help="intake account to sign in as (E11-S1); the "
        "password is read from the CDI_LOAD_PASSWORD environment variable only",
    )
    args, extra = ap.parse_known_args(argv)
    if args.patients < 1:
        ap.error("--patients must be at least 1")
    if not 1 <= args.docs_min <= args.docs_max <= 10:
        ap.error("document counts must satisfy 1 <= docs-min <= docs-max <= 10")
    for field in ("sla_s", "poll_s", "job_timeout_s"):
        value = getattr(args, field)
        if not math.isfinite(value) or value <= 0:
            ap.error(f"--{field.replace('_', '-')} must be finite and positive")
    return subprocess.run(command(args, extra), check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
