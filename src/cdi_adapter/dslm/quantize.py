"""AWQ/INT4 quantization of the fine-tuned DSLM (backlog E2-S6, ARCHITECTURE §14 #1d).

Runs in ``.venv-quant`` (``infra/runpod/requirements-quant.lock``) on a GPU host:

    python -m cdi_adapter.dslm.quantize --checkpoint <E2-S5 checkpoint> \\
        --corpus <corpus dir> --out /workspace/models/cdi-dslm-awq

* **Method:** AWQ with llm-compressor 0.7.1.4, scheme ``W4A16_ASYM`` (4-bit weights with a zero
  point, groups of 128; activations stay bf16). The output is a compressed-tensors checkpoint
  (format 0.11.0) that vLLM 0.11.0 loads with no flag: ``CDI_VLM_MODEL_ID=<out dir>``.
* **Scope:** the language decoder only. The vision tower and ``lm_head`` stay bf16. llm-compressor
  0.7.1 has no AWQ mapping for Qwen2.5-VL, and its generic fallback's ``up_proj -> down_proj``
  pattern also matches the vision tower's MLP. The mappings here are anchored to the decoder's
  ``layers.N`` (the vision blocks are ``blocks.N``).
* **Calibration:** the real S4 requests (``cdi_adapter.dslm.corpus``): page image at the serving
  ``max_pixels`` + the ``[bN]`` prompt, so AWQ's scales fit the traffic the model will serve.
  Synthetic or de-identified documents only. For E2-S5 this is the gold set's training split.
* **Record:** ``cdi_quantization.json`` in the output: source, recipe, calibration digest and
  tool versions, so a benchmark row can say exactly what it measured.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdi_adapter.dslm.corpus import corpus_digest, read_corpus

MANIFEST = "cdi_quantization.json"
SUPPORTED_ARCHITECTURES = ("Qwen2_5_VLForConditionalGeneration",)
SCHEME = "W4A16_ASYM"  # AWQ: int4 weights + zero point, group 128; bf16 activations
GROUP_SIZE = 128
TARGETS = ["Linear"]
IGNORE = ["lm_head", "re:visual.*", "re:model.visual.*"]
SEQUENTIAL_TARGETS = ["Qwen2_5_VLDecoderLayer"]
DEFAULT_SAMPLES = 128  # AWQ's usual calibration size; a smaller corpus is used whole
# = Settings.vlm_max_pixels_ocr (a test keeps them equal): .venv-quant has no pydantic-settings
DEFAULT_MAX_PIXELS = 2_000_000
TOOLS = ("llmcompressor", "compressed-tensors", "transformers", "torch")

_L = r"re:.*layers\.\d+\."  # decoder layers only (the vision tower's are blocks.N)
MAPPINGS: list[dict[str, Any]] = [
    {"smooth_layer": _L + r"input_layernorm$",
     "balance_layers": [_L + r"self_attn\.q_proj$", _L + r"self_attn\.k_proj$",
                        _L + r"self_attn\.v_proj$"]},
    {"smooth_layer": _L + r"self_attn\.v_proj$", "balance_layers": [_L + r"self_attn\.o_proj$"]},
    {"smooth_layer": _L + r"post_attention_layernorm$",
     "balance_layers": [_L + r"mlp\.gate_proj$", _L + r"mlp\.up_proj$"]},
    {"smooth_layer": _L + r"mlp\.up_proj$", "balance_layers": [_L + r"mlp\.down_proj$"]},
]  # fmt: skip


def recipe_spec() -> dict[str, Any]:
    """The AWQ recipe as plain data: what the manifest records and ``build_recipe`` builds."""
    return {"modifier": "AWQModifier", "scheme": SCHEME, "group_size": GROUP_SIZE,
            "targets": TARGETS, "ignore": IGNORE, "mappings": MAPPINGS,
            "sequential_targets": SEQUENTIAL_TARGETS}  # fmt: skip


def check_source(config: dict[str, Any]) -> None:
    """Refuse a checkpoint this recipe was not written for, or one that is already quantized."""
    archs = config.get("architectures") or []
    if not any(a in SUPPORTED_ARCHITECTURES for a in archs):
        raise ValueError(f"unsupported architecture {archs}: the AWQ mappings are written for "
                         f"{SUPPORTED_ARCHITECTURES}")  # fmt: skip
    if config.get("quantization_config"):
        raise ValueError("the checkpoint is already quantized: start from the bf16 checkpoint")


def check_output(config: dict[str, Any]) -> None:
    """The saved checkpoint is compressed-tensors int4 AWQ that vLLM detects on its own."""
    qc = config.get("quantization_config") or {}
    if qc.get("quant_method") != "compressed-tensors":
        raise ValueError(f"output is not a compressed-tensors checkpoint: {qc.get('quant_method')}")
    weights = [g.get("weights") or {} for g in (qc.get("config_groups") or {}).values()]
    if not weights or any(w.get("num_bits") != 4 or w.get("group_size") != GROUP_SIZE
                          for w in weights):  # fmt: skip
        raise ValueError(f"output is not int4 / group {GROUP_SIZE}: {weights}")


def calibration_rows(doc: dict[str, Any], samples: int) -> list[dict[str, Any]]:
    """Up to ``samples`` corpus requests, taken round-robin across doc types so every type
    shapes the scales, in a stable order."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(doc["requests"], key=lambda r: r["id"]):
        by_type.setdefault(row["doc_type"], []).append(row)
    out: list[dict[str, Any]] = []
    while len(out) < samples and any(by_type.values()):
        for rows in by_type.values():
            if rows and len(out) < samples:
                out.append(rows.pop(0))
    return out


def calibration_messages(row: dict[str, Any], image: Any) -> list[dict[str, Any]]:
    """The chat message the gateway builds for this request (image first, then the prompt)."""
    return [{"role": "user", "content": [{"type": "image", "image": image},
                                         {"type": "text", "text": row["prompt"]}]}]  # fmt: skip


def _file_sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def build_manifest(
    *,
    source: str,
    corpus_dir: Path,
    rows: list[dict[str, Any]],
    max_pixels: int,
    versions: dict[str, str | None],
    seconds: float | None = None,
) -> dict[str, Any]:
    doc = read_corpus(corpus_dir)
    src = Path(source)
    return {
        "block": "E2-S6",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_model": source,
        # a local checkpoint is pinned by its config + weight index; a hub id by its name only
        "source_config_sha256": _file_sha256(src / "config.json"),
        "source_index_sha256": _file_sha256(src / "model.safetensors.index.json"),
        "method": "AWQ",
        "recipe": recipe_spec(),
        "calibration": {
            "corpus_digest": corpus_digest(corpus_dir),
            "data_class": doc["data_class"],
            "samples": len(rows),
            "doc_types": dict(sorted(Counter(r["doc_type"] for r in rows).items())),
            "max_pixels": max_pixels,
        },
        "tools": versions,
        "seconds": None if seconds is None else round(seconds, 1),
    }


def tool_versions(dists: Sequence[str] = TOOLS) -> dict[str, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str | None] = {}
    for d in dists:
        try:
            out[d] = version(d)
        except PackageNotFoundError:
            out[d] = None
    return out


def build_recipe() -> list[Any]:  # pragma: no cover - needs llmcompressor (.venv-quant)
    from llmcompressor.modifiers.awq import AWQModifier
    from llmcompressor.modifiers.awq.mappings import AWQMapping

    return [AWQModifier(targets=TARGETS, scheme=SCHEME, ignore=IGNORE,
                        mappings=[AWQMapping(m["smooth_layer"], m["balance_layers"])
                                  for m in MAPPINGS])]  # fmt: skip


def check_paths(
    checkpoint: str, corpus_dir: Path, out_dir: Path, *, samples: int, max_pixels: int
) -> None:
    if samples < 1 or max_pixels < 1:
        raise ValueError("samples and max_pixels must be positive")
    target = out_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("quantization output must be an empty new directory")
    for source in (corpus_dir, Path(checkpoint)):
        if source.exists():
            source = source.resolve()
            if target.is_relative_to(source) or source.is_relative_to(target):
                raise ValueError("quantization output must not overlap its checkpoint or corpus")


def quantize(
    checkpoint: str, corpus_dir: Path, out_dir: Path, *, samples: int, max_pixels: int,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:  # pragma: no cover - needs a GPU and .venv-quant  # fmt: skip
    check_paths(checkpoint, corpus_dir, out_dir, samples=samples, max_pixels=max_pixels)
    import time

    import torch
    from datasets import Dataset
    from llmcompressor import oneshot
    from PIL import Image
    from qwen_vl_utils import process_vision_info
    from transformers import AutoConfig, AutoProcessor, Qwen2_5_VLForConditionalGeneration

    check_source(AutoConfig.from_pretrained(checkpoint).to_dict())
    rows = calibration_rows(read_corpus(corpus_dir), samples)
    if not rows:
        raise ValueError("the corpus has no requests")
    log(f"calibrating on {len(rows)} requests from {corpus_dir}")
    t0 = time.perf_counter()
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(checkpoint, torch_dtype="auto")
    processor = AutoProcessor.from_pretrained(checkpoint, max_pixels=max_pixels)

    def encode(example: dict[str, Any]) -> Any:
        messages = calibration_messages(example, Image.open(example["image_path"]).convert("RGB"))
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        images, videos = process_vision_info(messages)
        # no truncation: cutting image tokens would break the image/grid alignment
        return processor(text=[text], images=images, videos=videos, padding=False)

    ds = Dataset.from_list([{"prompt": r["prompt"], "image_path": r["image_path"]} for r in rows])
    ds = ds.map(encode, remove_columns=ds.column_names)
    longest = max(len(ids[0]) for ids in ds["input_ids"])

    def collate(batch: list[dict[str, Any]]) -> dict[str, Any]:
        assert len(batch) == 1
        return {k: torch.tensor(v) for k, v in batch[0].items()}

    oneshot(model=model, processor=processor, dataset=ds, recipe=build_recipe(),
            max_seq_length=longest, num_calibration_samples=len(rows), data_collator=collate,
            sequential_targets=SEQUENTIAL_TARGETS)  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out_dir, save_compressed=True)
    processor.save_pretrained(out_dir)
    check_output(json.loads((out_dir / "config.json").read_text(encoding="utf-8")))
    manifest = build_manifest(source=checkpoint, corpus_dir=corpus_dir, rows=rows,
                              max_pixels=max_pixels, versions=tool_versions(),
                              seconds=time.perf_counter() - t0)  # fmt: skip
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log(f"wrote {out_dir} in {manifest['seconds']} s")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - GPU host CLI
    import os

    ap = argparse.ArgumentParser(description="AWQ/INT4 the fine-tuned DSLM (backlog E2-S6).")
    ap.add_argument("--checkpoint", required=True, help="bf16 checkpoint dir (E2-S5) or hub id")
    ap.add_argument("--corpus", required=True, type=Path, help="cdi_adapter.dslm.corpus output")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--max-pixels", type=int,
                    default=int(os.environ.get("CDI_VLM_MAX_PIXELS_OCR", DEFAULT_MAX_PIXELS)),
                    help="must equal the gateway's CDI_VLM_MAX_PIXELS_OCR")  # fmt: skip
    args = ap.parse_args(argv)
    quantize(args.checkpoint, args.corpus, args.out, samples=args.samples,
             max_pixels=args.max_pixels)  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
