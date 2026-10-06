"""``python -m cdi_adapter.modelops <command>``: the model registry and rollout (backlog E10-S5).

    register VERSION --model-id ID --revision REV --backend hf|vllm|stub [--eval-report FILE]
    versions                        registered versions
    serve VERSION --url URL         start a gateway for VERSION on URL (127.0.0.1:<port>)
    deploy VERSION --url URL        the first primary
    canary VERSION --url URL --percent N     N % of documents to VERSION (1-50)
    set-percent N                   change the canary's share (0 pauses it)
    compare                         the canary vs the primary on live documents
    promote                         the canary becomes the primary (gated by compare)
    rollback                        ONE COMMAND: drop the canary, or return to the previous primary
    status                          live state, gateway health, recent history

Runbook: ``docs/model-rollout.md``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cdi_adapter.modelops import ops
from cdi_adapter.modelops.registry import RolloutError, RolloutState


def _actor(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("CDI_OPERATOR") or getpass.getuser()


def _print(obj: Any) -> None:
    def default(o: Any) -> Any:
        return asdict(o) if isinstance(o, RolloutState) else str(o)

    print(json.dumps(obj, indent=2, default=default))


def _state_line(st: RolloutState) -> str:
    line = f"primary {st.primary_version} @ {st.primary_url}"
    if st.canary_version:
        line += f"; canary {st.canary_version} @ {st.canary_url} = {st.canary_percent} %"
    return line


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.modelops",
                                 description="Model registry + canary/rollback")  # fmt: skip
    ap.add_argument("--actor", help="who is doing this (default: $CDI_OPERATOR or the OS user)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("version")
    r.add_argument("--model-id", required=True)
    r.add_argument("--revision", required=True, help="the pinned model revision (commit sha)")
    r.add_argument("--backend", required=True, choices=("hf", "vllm", "stub"))
    r.add_argument("--quantization")
    r.add_argument("--eval-report", type=Path, help="E2-S7 offline report (JSON)")
    r.add_argument("--notes")
    sub.add_parser("versions")
    for name in ("serve", "deploy", "canary"):
        c = sub.add_parser(name)
        c.add_argument("version")
        c.add_argument("--url", required=True)
        if name == "canary":
            c.add_argument("--percent", type=int, default=10)
        if name != "serve":
            c.add_argument("--reason")
        if name == "deploy":
            c.add_argument("--force", action="store_true")
    s = sub.add_parser("set-percent")
    s.add_argument("percent", type=int)
    s.add_argument("--reason")
    c = sub.add_parser("compare")
    c.add_argument("--json", action="store_true")
    p = sub.add_parser("promote")
    p.add_argument("--force", action="store_true")
    p.add_argument("--reason")
    b = sub.add_parser("rollback")
    b.add_argument("--reason")
    sub.add_parser("status")
    return ap


def _compare_table(rep: dict[str, Any]) -> str:
    keys = ("documents", "partial_rate", "failure_rate", "facts", "reviewed", "rejection_rate",
            "correction_rate", "auto_accept_rate", "mean_confidence", "extract_p50_s",
            "extract_p95_s")  # fmt: skip
    c, p = rep["canary"], rep["primary"]
    lines = [f"since {rep['since']} (canary {rep['canary_percent']} %)",
             f"{'':18} {'canary ' + c['version']:>28} {'primary ' + p['version']:>28}"]  # fmt: skip
    lines += [f"{k:18} {c[k]!s:>28} {p[k]!s:>28}" for k in keys]
    for side in (c, p):
        if side["eval_report"]:
            lines.append(f"E2-S7 {side['version']}: {json.dumps(side['eval_report'])}")
    lines += [f"note: {n}" for n in rep["notes"]]
    lines.append("PROMOTE: allowed" if rep["promote_ok"]
                 else "PROMOTE: blocked - " + "; ".join(rep["blocking"]))  # fmt: skip
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from cdi_adapter.logging import configure

    configure()
    args = build_parser().parse_args(argv)
    actor = _actor(args)
    try:
        if args.cmd == "register":
            report = json.loads(args.eval_report.read_text("utf-8")) if args.eval_report else None
            v = ops.register(actor=actor, version=args.version, model_id=args.model_id,
                             revision=args.revision, backend=args.backend,
                             quantization=args.quantization, eval_report=report,
                             notes=args.notes)  # fmt: skip
            print(f"registered {v['version']}: {v['model_id']}@{v['revision']} ({v['backend']})")
        elif args.cmd == "versions":
            from cdi_adapter.db import session_scope
            from cdi_adapter.modelops import registry

            with session_scope() as conn:
                for v in registry.list_versions(conn):
                    print(f"{v['version']:28} {v['model_id']}@{v['revision'][:12]} "
                          f"{v['backend']} eval={'yes' if v['eval_report'] else 'no'}")  # fmt: skip
        elif args.cmd == "serve":
            v = ops.serve(args.version, args.url)
            print(f"{args.url} serves {v['version']}")
        elif args.cmd == "deploy":
            st = ops.deploy(args.version, args.url, actor=actor, reason=args.reason,
                            force=args.force)  # fmt: skip
            print("deployed:", _state_line(st))
        elif args.cmd == "canary":
            st = ops.canary(args.version, args.url, args.percent, actor=actor, reason=args.reason)
            print("canary:", _state_line(st))
        elif args.cmd == "set-percent":
            st = ops.set_percent(args.percent, actor=actor, reason=args.reason)
            print("canary share:", _state_line(st))
        elif args.cmd == "compare":
            from cdi_adapter.db import session_scope
            from cdi_adapter.modelops import compare

            with session_scope() as conn:
                rep = compare.compare(conn)
            print(json.dumps(rep, indent=2, default=str) if args.json else _compare_table(rep))
        elif args.cmd == "promote":
            st, _ = ops.promote(actor=actor, reason=args.reason, force=args.force)
            print("promoted:", _state_line(st))
        elif args.cmd == "rollback":
            st = ops.rollback(actor=actor, reason=args.reason)
            print("rolled back:", _state_line(st))
        elif args.cmd == "status":
            out = ops.status()
            if out["state"] is None:
                print("no rollout recorded: every document goes to CDI_MLSERVE_URL")
            else:
                print(_state_line(out["state"]))
                for role, g in out["gateways"].items():
                    h = g["health"]
                    print(f"  {role:8} {g['url']:28} "
                          + ("DOWN" if h is None else f"{h.get('version')} {h.get('model')} "
                             f"loaded={h.get('loaded')}"))  # fmt: skip
                for st in out["history"]:
                    print(f"  #{st.id} {st.action:12} {st.actor:12} {_state_line(st)}"
                          + (f"  ({st.reason})" if st.reason else ""))  # fmt: skip
    except RolloutError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
