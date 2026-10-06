"""``python -m cdi_adapter.orchestrator steps DOC`` | ``replay DOC [--from-stage S] [--dry-run]``
| ``tools`` (backlog E12-S1) | ``trace DOC`` (E12-S5: the document's agent decisions). Prints
JSON; exit 0 (1 when a replay fails, or when a document's agent trace has gaps)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from cdi_adapter.db import session_scope
from cdi_adapter.orchestrator import trace, workflow
from cdi_adapter.orchestrator.tools import default_registry


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("steps", help="a document's recorded transitions").add_argument("document")
    rp = sub.add_parser("replay", help="re-run a document's recorded steps")
    rp.add_argument("document")
    rp.add_argument("--from-stage", choices=workflow.STAGES, default=None)
    rp.add_argument("--dry-run", action="store_true", help="print the plan only")
    sub.add_parser("tools", help="the allow-listed agent tools")
    sub.add_parser("trace", help="a document's agent decisions (agent_trace)").add_argument(
        "document"
    )
    args = ap.parse_args(argv)
    rc = 0
    if args.cmd == "steps":
        with session_scope() as c:
            out: dict = {"steps": workflow.steps(c, args.document)}
    elif args.cmd == "replay" and args.dry_run:
        with session_scope() as c:
            out = {"plan": workflow.replay_plan(c, args.document, args.from_stage)}
    elif args.cmd == "replay":
        out = workflow.replay(args.document, from_stage=args.from_stage, actor="cli")
        rc = 0 if out["status"] == "ok" else 1
    elif args.cmd == "trace":
        with session_scope() as c:
            out = trace.decisions(c, args.document)
        rc = 0 if out["complete"] else 1
    else:
        reg = default_registry()
        out = {"tools": [{"name": t.name, "description": t.description, "timeout_s": t.timeout_s,
                          "agents": sorted(a for a, ts in reg.allowed.items() if t.name in ts)}
                         for t in reg.tools.values()]}  # fmt: skip
    print(json.dumps(out, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
