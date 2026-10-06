"""De-identification CLI (backlog E11-S4). Runs where the data is (the pod), never elsewhere.

    python -m cdi_adapter.deid fetch-models                  # once: pinned models into the cache
    python -m cdi_adapter.deid scans IN_DIR OUT_DIR           # scans (+ answer keys) -> de-id
    python -m cdi_adapter.deid text "Name: Anjali Das ..."    # quick look at one text

``scans``: every ``*.png`` / ``*.jpg`` in IN_DIR is OCR'd (RapidOCR, as S3) and de-identified;
a same-named ``*.json`` next to it (an E2-S4 answer key) is de-identified with the same document
key. OUT_DIR gets the redacted page, its OCR text, the JSON, and ``manifest.json`` (entity counts
and detector versions, never a removed value).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def cmd_fetch() -> int:
    import spacy
    from huggingface_hub import snapshot_download

    from cdi_adapter.config import get_settings

    s = get_settings()
    path = snapshot_download(s.deid_ner_model, revision=s.deid_ner_revision,
                             allow_patterns=["config.json", "model.safetensors", "tokenizer.json",
                                             "tokenizer_config.json", "special_tokens_map.json",
                                             "vocab.json", "merges.txt"])  # fmt: skip
    print(f"ner model: {s.deid_ner_model}@{s.deid_ner_revision} -> {path}")
    if not spacy.util.is_package(s.deid_spacy_model):
        print(f"spaCy model {s.deid_spacy_model} is not installed (docs/deid.md)")
        return 1
    print(f"spacy model: {s.deid_spacy_model} installed")
    return 0


def cmd_scans(src: Path, dst: Path) -> int:
    from cdi_adapter.deid.document import Page, deidentify_document, ocr_lines

    dst.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    for img in sorted(p for p in src.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")):
        png = img.read_bytes()
        lines = ocr_lines(png)
        key_file = img.with_suffix(".json")
        payload = json.loads(key_file.read_text(encoding="utf-8")) if key_file.exists() else None
        res = deidentify_document([Page(png, lines)], payload)
        stem = f"deid_{len(manifest):05d}"  # the original file name may itself identify
        (dst / f"{stem}.png").write_bytes(res.pages[0])
        (dst / f"{stem}.txt").write_text(res.ocr_text[0], encoding="utf-8")
        if res.payload is not None:
            (dst / f"{stem}.json").write_text(json.dumps(res.payload, indent=2, ensure_ascii=False),
                                              encoding="utf-8")  # fmt: skip
        manifest[stem] = res.manifest()
        print(
            f"{stem}: {sum(res.counts.values())} identifiers, {res.redacted_lines} lines redacted"
        )
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cdi_adapter.deid", description=__doc__.splitlines()[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-models")
    sc = sub.add_parser("scans")
    sc.add_argument("src", type=Path)
    sc.add_argument("dst", type=Path)
    tx = sub.add_parser("text")
    tx.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "fetch-models":
        return cmd_fetch()
    if args.cmd == "scans":
        return cmd_scans(args.src, args.dst)
    from cdi_adapter.deid.engine import deidentify_text

    clean, spans = deidentify_text(args.text)
    print(clean)
    for s in spans:
        print(f"  {s.entity:<22} {s.score:.2f} {s.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
