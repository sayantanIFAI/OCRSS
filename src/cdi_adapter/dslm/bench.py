"""Serving benchmark: bf16 baseline vs the AWQ/INT4 DSLM (backlog E2-S6).

Two commands, each in the venv it needs:

    # .venv-vllm on the GPU host, one process per variant (the GPU is fully freed between them)
    python -m cdi_adapter.dslm.bench run --name bf16 --model Qwen/Qwen2.5-VL-7B-Instruct \\
        --corpus <dir> --out bf16.json
    python -m cdi_adapter.dslm.bench run --name awq --model /workspace/models/cdi-dslm-awq ...
    # pipeline venv (has the schema validator): the comparison + the Sizing & Capacity row
    python -m cdi_adapter.dslm.bench compare bf16.json awq.json --archive docs/bench/e2-s6

``run`` replays the corpus through the production ``VLLMBackend`` (same engine settings, chat
prompt, image handling and XGrammar lock as the gateway), with the 7B->3B fallback switched off so
a variant that does not fit fails instead of quietly measuring another model. It records:

* **extract-seconds/doc**: each request alone, after one warm-up request (median and p95)
* **docs/minute** at each concurrency level (continuous batching, E2-S1)
* **VRAM**: device memory in use once the engine is up (weights + the KV cache vLLM reserves)
* **KV cache** in tokens (vLLM's own sizing), and **concurrent slots** = KV tokens / the p95
  request footprint (prompt incl. image tokens + generated tokens); ``slots_worst`` uses the
  full ``max_tokens`` budget instead of the generated count

Output agreement with the baseline is a regression signal only; accuracy is E2-S7's harness on
the E2-S4 gold set (DoD-EVAL). A row is tagged ``MEASURED`` only when run on the 32 GB target
GPU (ARCHITECTURE §1: RTX PRO 4500 Blackwell 32 GB).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdi_adapter.dslm.corpus import corpus_digest, read_corpus

TARGET_GPU = "RTX PRO 4500"  # ARCHITECTURE §1: the pod's Blackwell 32 GB
TARGET_MIN_MIB = 30 * 1024
DEFAULT_LEVELS = (1, 2, 4)
QUANT_MANIFEST = "cdi_quantization.json"


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) of a non-empty sample."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)])


def kv_slots(kv_tokens: int, footprints: Sequence[int]) -> int:
    """Requests of the p95 footprint the KV cache holds at once."""
    need = percentile(footprints, 95)
    return int(kv_tokens // need) if need > 0 else 0


def _gpu_info() -> dict[str, Any]:  # pragma: no cover - needs CUDA
    import torch

    free, total = torch.cuda.mem_get_info()
    return {"name": torch.cuda.get_device_name(0), "total_mib": total // 2**20,
            "used_mib": (total - free) // 2**20}  # fmt: skip


def _default_backend(settings: Any) -> Any:  # pragma: no cover - needs vllm (.venv-vllm)
    from cdi_adapter.mlserve.backends import VLLMBackend

    return VLLMBackend(settings)


def _quant_manifest(model: str) -> dict[str, Any] | None:
    path = Path(model) / QUANT_MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def run_variant(
    name: str,
    model: str,
    corpus_dir: Path,
    *,
    settings: Any,
    levels: Sequence[int] = DEFAULT_LEVELS,
    backend_factory: Callable[[Any], Any] = _default_backend,
    gpu: Callable[[], dict[str, Any]] = _gpu_info,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    if not levels or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in levels):
        raise ValueError("concurrency levels must be positive integers")
    if len(set(levels)) != len(levels):
        raise ValueError("concurrency levels must be unique")
    doc = read_corpus(corpus_dir)
    rows = doc["requests"]
    if not rows:
        raise ValueError("the corpus has no requests")
    # no silent 7B->3B fallback: a variant that does not fit must fail, not measure another model
    s = settings.model_copy(update={"mlserve_backend": "vllm", "vlm_model_id": model,
                                    "vlm_fallback_model_id": model})  # fmt: skip
    before = gpu()
    be = backend_factory(s)
    t0 = clock()
    kv_tokens = be.kv_cache_tokens()  # starts the engine
    load_s = clock() - t0
    if be.model != model:
        raise RuntimeError(f"engine loaded {be.model!r}, not {model!r}")
    after = gpu()
    images = {r["id"]: Path(r["image_path"]).read_bytes() for r in rows}

    def call(row: dict[str, Any]) -> dict[str, Any]:
        t = clock()
        text, p_tok, o_tok = be.generate_with_usage(row["prompt"], images[row["id"]],
                                                    max_tokens=row["max_tokens"],
                                                    json_schema=row["json_schema"])  # fmt: skip
        return {"id": row["id"], "doc_type": row["doc_type"], "schema": row["schema"],
                "max_tokens": row["max_tokens"], "seconds": clock() - t,
                "prompt_tokens": p_tok, "output_tokens": o_tok, "text": text}  # fmt: skip

    call(rows[0])  # warm-up: CUDA graphs, grammar compile, first image; not measured
    serial = [call(r) for r in rows]
    secs = [r["seconds"] for r in serial]
    footprint = [r["prompt_tokens"] + r["output_tokens"] for r in serial]
    budget = [r["prompt_tokens"] + r["max_tokens"] for r in serial]

    concurrency = []
    for k in levels:
        batch = [rows[i % len(rows)] for i in range(max(len(rows), 2 * k))]
        with ThreadPoolExecutor(max_workers=k) as pool:
            t = clock()
            done = list(pool.map(call, batch))
            wall = clock() - t
        concurrency.append(
            {
                "concurrency": k,
                "requests": len(done),
                "wall_s": round(wall, 3),
                "docs_per_min": round(len(done) / wall * 60, 2) if wall else None,
                "p95_s": round(percentile([d["seconds"] for d in done], 95), 3),
            }
        )

    return {
        "block": "E2-S6",
        "variant": name,
        "model": model,
        "quantization": _quant_manifest(model),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": {
            "digest": corpus_digest(corpus_dir),
            "data_class": doc["data_class"],
            "requests": len(rows),
        },
        "engine": {
            "gpu_memory_utilization": s.vllm_gpu_memory_utilization,
            "max_model_len": s.vllm_max_model_len,
            "max_num_seqs": s.vllm_max_num_seqs,
            "max_pixels": s.vlm_max_pixels_ocr,
            "guided": s.vllm_guided,
        },
        "gpu": {
            "name": after["name"],
            "total_mib": after["total_mib"],
            "used_before_mib": before["used_mib"],
            "used_mib": after["used_mib"],
        },
        "load_s": round(load_s, 1),
        "kv_cache_tokens": kv_tokens,
        "extract_s_per_doc": {
            "median": round(statistics.median(secs), 3),
            "p95": round(percentile(secs, 95), 3),
            "mean": round(statistics.fmean(secs), 3),
        },
        "tokens": {
            "prompt_p95": int(percentile([r["prompt_tokens"] for r in serial], 95)),
            "output_p95": int(percentile([r["output_tokens"] for r in serial], 95)),
            "footprint_p95": int(percentile(footprint, 95)),
        },
        "slots": kv_slots(kv_tokens, footprint),
        "slots_worst": kv_slots(kv_tokens, budget),
        "concurrency": concurrency,
        "serial": serial,
    }


# ------------------------------------------------------------------ compare (pipeline venv)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf paths of a JSON value, e.g. ``medications[0].drug_name``. ``confidence`` leaves are
    left out: they are the model's own estimate, not a field of the page."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "confidence":
                out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


def agreement(baseline: Any, other: Any) -> float | None:
    """Share of the baseline's leaves the other output reproduces exactly (None: no leaves)."""
    base, got = flatten(baseline), flatten(other)
    if not base:
        return None
    return sum(1 for k, v in base.items() if k in got and got[k] == v) / len(base)


def _parse(text: str, schema: str) -> tuple[Any, bool]:
    from cdi_adapter.ml.client import validate_schema
    from cdi_adapter.mlserve.grammar import strict_json

    try:
        payload = strict_json(text)
    except ValueError:
        return None, False
    return payload, not validate_schema(payload, schema)


def is_target(gpu: dict[str, Any], target: str = TARGET_GPU) -> bool:
    return target.lower() in str(gpu.get("name", "")).lower() and gpu["total_mib"] >= TARGET_MIN_MIB


def _gib(mib: int) -> float:
    return round(mib / 1024, 1)


def _check_comparable(results: Sequence[dict[str, Any]]) -> None:
    base = results[0]
    names = [r["variant"] for r in results]
    if len(set(names)) != len(names):
        raise ValueError("benchmark variant names must be unique")
    expected = {s["id"]: s["schema"] for s in base["serial"]}
    for r in results:
        serial = r["serial"]
        if not serial or len({s["id"] for s in serial}) != len(serial):
            raise ValueError("benchmark requests must be nonempty and have unique ids")
        if {s["id"]: s["schema"] for s in serial} != expected:
            raise ValueError("benchmark request ids and schemas must match")
        if r["corpus"] != base["corpus"] or r["corpus"]["requests"] != len(serial):
            raise ValueError("benchmark corpus digests, classifications and counts must match")
        if r["engine"] != base["engine"]:
            raise ValueError("benchmark engine settings must match")
        if any(r["gpu"][k] != base["gpu"][k] for k in ("name", "total_mib")):
            raise ValueError("benchmark hardware must match")
        if not r["concurrency"]:
            raise ValueError("benchmark requires concurrency measurements")
        levels = [c["concurrency"] for c in r["concurrency"]]
        if len(set(levels)) != len(levels) or sorted(levels) != sorted(
            c["concurrency"] for c in base["concurrency"]
        ):
            raise ValueError("benchmark concurrency levels must match and be unique")
        values = [
            r["extract_s_per_doc"]["median"],
            r["extract_s_per_doc"]["p95"],
            r["engine"]["max_num_seqs"],
            *levels,
            *(c["docs_per_min"] for c in r["concurrency"]),
        ]
        if any(
            isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0
            for v in values
        ):
            raise ValueError("benchmark measurements must be finite and positive")


def compare(results: Sequence[dict[str, Any]], *, target: str = TARGET_GPU) -> dict[str, Any]:
    """The first result is the baseline (the bf16 7B)."""
    if len(results) < 2:
        raise ValueError("compare needs the baseline and at least one variant")
    _check_comparable(results)
    base = results[0]
    parsed = {r["variant"]: {s["id"]: _parse(s["text"], s["schema"]) for s in r["serial"]}
              for r in results}  # fmt: skip
    rows = []
    for r in results:
        valid = [ok for _, ok in parsed[r["variant"]].values()]
        agree = [
            a
            for i, (payload, _) in parsed[r["variant"]].items()
            if (a := agreement(parsed[base["variant"]][i][0], payload)) is not None
        ]
        best = max(r["concurrency"], key=lambda c: c["docs_per_min"] or 0)
        tag = ("MEASURED" if is_target(r["gpu"], target)
               else f"MEASURED on {r['gpu']['name']} (not the 32 GB target)")  # fmt: skip
        rows.append({
            "variant": r["variant"], "model": r["model"],
            "quantization": "AWQ W4A16" if r.get("quantization") else "bf16",
            "source_model": (r.get("quantization") or {}).get("source_model", r["model"]),
            "gpu": r["gpu"]["name"], "gpu_total_gib": _gib(r["gpu"]["total_mib"]),
            "extract_s_per_doc": r["extract_s_per_doc"]["median"],
            "extract_s_per_doc_p95": r["extract_s_per_doc"]["p95"],
            "speedup_vs_baseline": round(base["extract_s_per_doc"]["median"]
                                         / r["extract_s_per_doc"]["median"], 2),
            "docs_per_min": best["docs_per_min"], "at_concurrency": best["concurrency"],
            "vram_used_gib": _gib(r["gpu"]["used_mib"]),
            "kv_cache_tokens": r["kv_cache_tokens"],
            "concurrent_slots": min(r["slots"], r["engine"]["max_num_seqs"]),
            "concurrent_slots_worst": min(r["slots_worst"], r["engine"]["max_num_seqs"]),
            "kv_slot_estimate": r["slots"],
            "max_num_seqs": r["engine"]["max_num_seqs"],
            "schema_valid": f"{sum(valid)}/{len(valid)}",
            "agreement_with_baseline": round(statistics.fmean(agree), 3) if agree else None,
            "tag": tag,
        })  # fmt: skip
    return {"block": "E2-S6", "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "baseline": base["variant"],
            "corpus": base["corpus"], "rows": rows,
            "note": "agreement_with_baseline is exact leaf agreement with the bf16 output, a "
                    "regression signal only; accuracy is E2-S7 on the E2-S4 gold set. "
                    "Slots are KV estimates capped by max_num_seqs; safe capacity needs a load run."
    }  # fmt: skip


_COLUMNS = (
    ("variant", "Variant"), ("quantization", "Weights"), ("gpu", "GPU"),
    ("extract_s_per_doc", "s/doc (median)"), ("extract_s_per_doc_p95", "s/doc (p95)"),
    ("speedup_vs_baseline", "Speed-up"), ("docs_per_min", "docs/min"),
    ("at_concurrency", "at k"), ("vram_used_gib", "VRAM GiB"),
    ("kv_cache_tokens", "KV tokens"), ("concurrent_slots", "Slots"),
    ("concurrent_slots_worst", "Slots (worst)"), ("schema_valid", "Schema-valid"),
    ("agreement_with_baseline", "Agreement"), ("tag", "Tag"),
)  # fmt: skip


def render_markdown(summary: dict[str, Any]) -> str:
    c = summary["corpus"]
    lines = [
        "# E2-S6 benchmark: bf16 baseline vs AWQ/INT4",
        "",
        f"Created {summary['created']}. Corpus `{c['digest'][:12]}` ({c['requests']} requests, "
        f"{c['data_class']}). Baseline: `{summary['baseline']}`.",
        "",
        "| " + " | ".join(h for _, h in _COLUMNS) + " |",
        "|" + "---|" * len(_COLUMNS),
    ]
    for row in summary["rows"]:
        lines.append("| " + " | ".join(str(row[k]) for k, _ in _COLUMNS) + " |")
    lines += ["", f"Note: {summary['note']}", ""]
    for row in summary["rows"]:
        lines.append(f"- `{row['variant']}`: `{row['model']}` (source `{row['source_model']}`)")
    return "\n".join(lines) + "\n"


def archive(summary: dict[str, Any], results: Sequence[dict[str, Any]], out_dir: Path) -> Path:
    """``<UTC stamp>-<gpu>.json`` (summary + every raw result) and ``.md`` in ``out_dir``."""
    stamp = datetime.fromisoformat(summary["created"]).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    gpu = re.sub(r"[^a-z0-9]+", "-", summary["rows"][0]["gpu"].lower()).strip("-")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{stamp}-{gpu}"
    base.with_suffix(".json").write_text(
        json.dumps({"summary": summary, "results": list(results)}, indent=1), encoding="utf-8"
    )
    base.with_suffix(".md").write_text(render_markdown(summary), encoding="utf-8")
    return base.with_suffix(".md")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DSLM serving benchmark (backlog E2-S6).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="benchmark one variant (.venv-vllm, GPU host)")
    r.add_argument("--name", required=True)
    r.add_argument("--model", required=True, help="hub id or local checkpoint dir")
    r.add_argument("--corpus", required=True, type=Path)
    r.add_argument("--out", required=True, type=Path)
    r.add_argument(
        "--levels",
        default=",".join(map(str, DEFAULT_LEVELS)),
        help="concurrency levels, comma separated",
    )
    c = sub.add_parser("compare", help="baseline first; writes the comparison (pipeline venv)")
    c.add_argument("results", nargs="+", type=Path)
    c.add_argument("--archive", type=Path, help="directory for the dated .json + .md")
    c.add_argument("--target-gpu", default=TARGET_GPU)
    args = ap.parse_args(argv)

    if args.cmd == "run":  # pragma: no cover - needs the GPU host
        from cdi_adapter.config import get_settings

        result = run_variant(args.name, args.model, args.corpus, settings=get_settings(),
                             levels=[int(k) for k in args.levels.split(",")])  # fmt: skip
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(
            f"{args.name}: {result['extract_s_per_doc']['median']} s/doc, "
            f"slots {result['slots']}, wrote {args.out}"
        )
        return 0
    results = [json.loads(p.read_text(encoding="utf-8")) for p in args.results]
    summary = compare(results, target=args.target_gpu)
    if args.archive:
        print(f"wrote {archive(summary, results, args.archive)}")
    print(render_markdown(summary))
    return (
        0
        if all(
            row["schema_valid"].split("/")[0] == row["schema_valid"].split("/")[1]
            for row in summary["rows"]
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
