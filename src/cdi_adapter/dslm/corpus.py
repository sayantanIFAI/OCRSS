"""The S4 extraction-request corpus (backlog E2-S6).

The benchmark and the AWQ calibration both need the requests the pipeline really sends the
model: the normalized page-1 image, the ``[bN]`` OCR prompt, the inlined schema and the per-type
token budget. ``build_request`` makes one from a source document with the pipeline's own code
(S1 render + normalize, S3 RapidOCR, S2 heuristic classifier, S4 prompt) and no database.

Only synthetic or de-identified documents may go in (ARCHITECTURE §12, E11-S4): the corpus is
copied to GPU hosts and into a checkpoint's calibration record. ``write_corpus`` refuses any
other ``data_class``.

    python -m cdi_adapter.dslm.corpus build --out <dir> --data-class synthetic data/samples/*.pdf
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CORPUS_FILE = "corpus.json"
FORMAT_VERSION = 1
DATA_CLASSES = ("synthetic", "deidentified")


@dataclass(frozen=True)
class Request:
    id: str
    source: str
    source_sha256: str
    doc_type: str
    schema: str
    max_tokens: int
    prompt: str
    json_schema: dict[str, Any]
    image: str  # path of the page-1 PNG, relative to the corpus directory
    blocks: int


def _default_ocr(png: bytes) -> list[dict[str, Any]]:
    from cdi_adapter.ocr.rapid import run_rapidocr

    return [{"id": f"line-{i}", "text": ln.text} for i, ln in enumerate(run_rapidocr(png))]


def build_request(
    raw: bytes,
    filename: str,
    *,
    doc_type: str | None = None,
    ocr: Callable[[bytes], list[dict[str, Any]]] = _default_ocr,
) -> tuple[Request, bytes]:
    """One document -> its S4 request and page-1 PNG, exactly as the pipeline builds them."""
    from cdi_adapter.classify.service import _heuristic_classify
    from cdi_adapter.extract.prompt import (
        SCHEMA_FOR_DOC_TYPE,
        build_extraction_prompt,
        max_tokens_for,
    )
    from cdi_adapter.ingest.pages import normalize_image, render_pages, sniff_mime
    from cdi_adapter.ml.client import bundled_schema

    rendered = render_pages(raw, sniff_mime(raw, filename))
    if not rendered:
        raise ValueError(f"{filename}: document has no pages")
    png, _ = normalize_image(rendered[0])  # what S1 stores and S4 sends (page 1)
    blocks = ocr(png)
    if doc_type is None:
        found = _heuristic_classify("\n".join(b["text"] for b in blocks))
        if found is None:
            raise ValueError(f"{filename}: the heuristic classifier is unsure; label it with "
                             "--label <file>=<doc_type>")  # fmt: skip
        doc_type = found.doc_type
    if doc_type not in SCHEMA_FOR_DOC_TYPE:
        raise ValueError(f"{filename}: doc_type {doc_type!r} has no extraction schema")
    prompt, _ = build_extraction_prompt(doc_type, blocks)
    schema = SCHEMA_FOR_DOC_TYPE[doc_type]
    stem = Path(filename).name.replace(".", "_")
    req = Request(id=stem, source=Path(filename).name,
                  source_sha256=hashlib.sha256(raw).hexdigest(), doc_type=doc_type,
                  schema=schema, max_tokens=max_tokens_for(doc_type), prompt=prompt,
                  json_schema=bundled_schema(schema), image=f"images/{stem}.png",
                  blocks=len(blocks))  # fmt: skip
    return req, png


def write_corpus(out_dir: Path, items: Iterable[tuple[Request, bytes]], *, data_class: str) -> Path:
    if data_class not in DATA_CLASSES:
        raise ValueError(f"data_class must be one of {DATA_CLASSES}: real patient documents are "
                         "de-identified first (E11-S4)")  # fmt: skip
    out_dir = Path(out_dir)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    rows = []
    for req, png in items:
        (out_dir / req.image).write_bytes(png)
        rows.append(asdict(req))
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("two documents map to the same corpus id")
    created = datetime.now(UTC).isoformat(timespec="seconds")
    doc = {"format": FORMAT_VERSION, "data_class": data_class, "created": created, "requests": rows}
    path = out_dir / CORPUS_FILE
    path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return path


def read_corpus(corpus_dir: Path) -> dict[str, Any]:
    """The corpus document; ``requests`` rows gain ``image_path`` (absolute)."""
    corpus_dir = Path(corpus_dir)
    doc: dict[str, Any] = json.loads((corpus_dir / CORPUS_FILE).read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT_VERSION:
        raise ValueError(f"unsupported corpus format {doc.get('format')!r}")
    if doc.get("data_class") not in DATA_CLASSES:
        raise ValueError(f"corpus data_class {doc.get('data_class')!r} is not allowed")
    for row in doc["requests"]:
        row["image_path"] = str(corpus_dir / row["image"])
    return doc


def corpus_digest(corpus_dir: Path) -> str:
    """sha256 over the corpus file and every image: what a checkpoint was calibrated on."""
    corpus_dir = Path(corpus_dir)
    h = hashlib.sha256((corpus_dir / CORPUS_FILE).read_bytes())
    for row in sorted(read_corpus(corpus_dir)["requests"], key=lambda r: r["id"]):
        h.update(Path(row["image_path"]).read_bytes())
    return h.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the S4 request corpus (backlog E2-S6).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="documents -> corpus directory")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--data-class", required=True, choices=DATA_CLASSES)
    b.add_argument(
        "--label",
        action="append",
        default=[],
        help="<file name>=<doc_type> when the heuristic classifier is unsure",
    )
    b.add_argument("documents", nargs="+", type=Path)
    args = ap.parse_args(argv)

    labels = dict(lbl.split("=", 1) for lbl in args.label)
    items = []
    for path in args.documents:
        req, png = build_request(path.read_bytes(), path.name, doc_type=labels.get(path.name))
        print(f"{path.name}: {req.doc_type}, {req.blocks} OCR blocks, max_tokens {req.max_tokens}")
        items.append((req, png))
    out = write_corpus(args.out, items, data_class=args.data_class)
    print(f"wrote {out} ({len(items)} requests, digest {corpus_digest(args.out)[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
