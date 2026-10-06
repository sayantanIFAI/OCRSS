"""Canary + rollback drill with real models on a GPU host (backlog E10-S5 "Tests").

Two registered versions, each in its own gateway process on the GPU, take real extractions of
synthetic documents (no patient data) through the web app's router:

  deploy A -> canary B at 50 % -> documents split by id -> compare -> ROLLBACK (canary dropped)
  -> canary B again -> promote -> ROLLBACK (back to A) -> promote again, stop A's gateway
  -> ROLLBACK (A's gateway started again, then switched)

Two different 7B models do not fit one 24 GB L4 together, so the drill registers the same 3B
weights (at their pinned revision) as two versions: what is drilled is the routing, the record of
which version answered, and the rollback, not a model difference. Needs PostgreSQL + MinIO and the
GPU free of other gateways.

    python scripts/model_rollout_drill.py --docs 8
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _samples() -> Any:
    path = ROOT / "scripts" / "make_sample_docs.py"
    spec = importlib.util.spec_from_file_location("make_sample_docs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def revision_of(model_id: str) -> str:  # pragma: no cover - needs the hub or its cache
    """The model's current commit on the hub, pinned for the drill."""
    from huggingface_hub import HfApi

    return str(HfApi().model_info(model_id).sha)


def prescription(i: int) -> list[str]:
    name = "Drill " + "".join(chr(97 + (i * 7 + k * 3) % 26) for k in range(6)).title()
    lines = list(_samples().DOCS["prescription_00"])
    return [ln.replace("Anjali Das", name) for ln in lines]


def one_document(i: int, tag: str) -> dict[str, Any]:  # pragma: no cover - GPU host
    from sqlalchemy import text

    from cdi_adapter.classify.service import classify_document
    from cdi_adapter.db import session_scope
    from cdi_adapter.extract.service import extract_document
    from cdi_adapter.ingest.service import ingest_bytes
    from cdi_adapter.modelops import router
    from cdi_adapter.ocr.service import ocr_document

    pdf = _samples().text_pdf(prescription(i) + [f"Ref: {tag}-{i}"])
    doc = str(ingest_bytes(pdf, f"drill-{tag}-{i}.pdf", source_channel="drill").document_id)
    ocr_document(doc, force_engine="rapidocr")
    classify_document(doc)
    role = router.route(doc).role
    t0 = time.perf_counter()
    out = extract_document(doc)
    secs = round(time.perf_counter() - t0, 1)
    with session_scope() as c:
        v, m = c.execute(text("SELECT model_version, model_name FROM extraction "
                              "WHERE document_id = :d ORDER BY created_at DESC LIMIT 1"),
                         {"d": doc}).one()  # fmt: skip
    return {"doc": doc[:8], "routed": role, "version": v, "model": m, "facts": out.get("facts"),
            "partial": out.get("partial"), "s": secs}  # fmt: skip


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - GPU host
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model-id", default="Qwen/Qwen2.5-VL-3B-Instruct")
    ap.add_argument("--backend", default="hf", choices=("hf", "vllm"))
    ap.add_argument("--primary-url", default="http://127.0.0.1:8079")
    ap.add_argument("--canary-url", default="http://127.0.0.1:8078")
    ap.add_argument("--docs", type=int, default=8)
    ap.add_argument("--restore", metavar="VERSION",
                    help="afterwards, make this version the primary again")  # fmt: skip
    ap.add_argument("--restore-url", default="http://127.0.0.1:8077")
    args = ap.parse_args(argv)
    run = time.strftime("%H%M%S")

    from cdi_adapter.db import session_scope
    from cdi_adapter.modelops import compare, ops, registry, router
    from cdi_adapter.modelops.__main__ import _compare_table

    checks: list[tuple[str, bool]] = []
    procs: list[subprocess.Popen[bytes]] = []

    def launch(version: dict[str, Any], url: str) -> None:
        procs.append(ops.launch_gateway(version, url))

    def batch(n: int, tag: str) -> list[dict[str, Any]]:
        rows = [one_document(i, f"{run}{tag}") for i in range(n)]
        for r in rows:
            print("   ", r)
        return rows

    rev = revision_of(args.model_id)
    a, b = "drill-3b-a", "drill-3b-b"
    for v in (a, b):
        ops.register(actor="drill", version=v, model_id=args.model_id, revision=rev,
                     backend=args.backend, notes="E10-S5 drill: same weights")  # fmt: skip
    print(f"registered {a}, {b}: {args.model_id}@{rev}")
    try:
        ops.serve(a, args.primary_url, launch=launch)
        ops.serve(b, args.canary_url, launch=launch)
        with session_scope() as c:
            live = registry.current(c)
        ops.deploy(a, args.primary_url, actor="drill", force=live is not None,
                   reason="E10-S5 drill" if live is not None else None)  # fmt: skip
        t0 = time.perf_counter()
        ops.canary(b, args.canary_url, 50, actor="drill", reason="E10-S5 drill")
        print(f"1. canary {b} at 50 % ({time.perf_counter() - t0:.2f} s)")
        rows = batch(args.docs, "c1")
        right = all(r["version"] == (b if r["routed"] == "canary" else a) for r in rows)
        both = {r["routed"] for r in rows} == {"primary", "canary"}
        checks += [("each document answered by the version it was routed to", right),
                   ("both sides took documents", both)]  # fmt: skip
        with session_scope() as c:
            print(_compare_table(compare.compare(c)))

        t0 = time.perf_counter()
        st = ops.rollback(actor="drill", reason="E10-S5 drill: canary rollback")
        print(f"2. ROLLBACK (canary) in {time.perf_counter() - t0:.2f} s -> primary "
              f"{st.primary_version}, canary {st.canary_version}")  # fmt: skip
        rows = batch(max(2, args.docs // 2), "r1")
        checks.append(("after the canary rollback every document is on A",
                       all(r["version"] == a for r in rows)))  # fmt: skip

        ops.canary(b, args.canary_url, 50, actor="drill")
        batch(max(2, args.docs // 2), "c2")
        st, rep = ops.promote(actor="drill", force=True, reason="E10-S5 drill (few documents)")
        print(f"3. promoted {st.primary_version} (gate: {rep['blocking'] or 'ok'})")
        rows = batch(2, "p1")
        checks.append(("after promotion every document is on B",
                       all(r["version"] == b for r in rows)))  # fmt: skip
        t0 = time.perf_counter()
        st = ops.rollback(actor="drill", reason="E10-S5 drill: promotion rollback")
        took = time.perf_counter() - t0
        print(f"4. ROLLBACK (promotion) in {took:.2f} s -> {st.primary_version}")
        rows = batch(2, "r2")
        checks.append(("after the promotion rollback every document is on A",
                       all(r["version"] == a for r in rows)))  # fmt: skip

        ops.canary(b, args.canary_url, 50, actor="drill")
        batch(2, "c3")
        ops.promote(actor="drill", force=True, reason="E10-S5 drill (few documents)")
        procs[0].terminate()  # A's gateway goes away
        procs[0].wait(60)
        down = ops.gateway_health(args.primary_url) is None
        n0 = len(procs)
        t0 = time.perf_counter()
        st = ops.rollback(actor="drill", reason="E10-S5 drill: restart rollback", launch=launch)
        print(f"5. ROLLBACK with A's gateway down in {time.perf_counter() - t0:.1f} s -> "
              f"{st.primary_version}")  # fmt: skip
        rows = batch(2, "r3")
        restarted = down and len(procs) == n0 + 1
        checks.append(("rollback started A's gateway again and switched to it",
                       restarted and all(r["version"] == a for r in rows)))  # fmt: skip
        with session_scope() as c:
            hist = [f"{s.action}:{s.primary_version}/{s.canary_version or '-'}"
                    for s in reversed(registry.history(c, 11))]  # fmt: skip
        print("rollout log:", " ".join(hist))
    finally:
        router.reset()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
    if args.restore:  # the drill gateways are gone: put the site's own model back
        st = ops.deploy(args.restore, args.restore_url, actor="drill", force=True,
                        reason="E10-S5 drill finished: restore")  # fmt: skip
        print(f"restored: primary {st.primary_version} @ {st.primary_url}")
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    ok = bool(checks) and all(ok for _, ok in checks)
    print("MODEL ROLLOUT DRILL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
