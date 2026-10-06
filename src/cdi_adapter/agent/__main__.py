"""``python -m cdi_adapter.agent run <document id>``: run the medication agent on one document."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from cdi_adapter.agent.medication import run_medication_agent


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the medication agent on one document")
    run.add_argument("document_id")
    a = ap.parse_args(argv)
    json.dump(run_medication_agent(a.document_id, actor="cli"), sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
