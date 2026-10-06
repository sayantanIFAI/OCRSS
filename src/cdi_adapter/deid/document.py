"""De-identify one document for training / evaluation use (backlog E11-S4; ARCHITECTURE §12).

A document here is what E2-S4 (gold set) and E2-S5 (fine-tuning) consume: its page images, the
OCR lines on each page (text + bbox, S3) and its structured extraction payload (S4) or answer key.

* page images: every OCR line holding an identifier is blacked out, whole line plus a margin
  (conservative: a partial redaction can leave a readable fragment); then the redacted image is
  OCR'd again and anything still identifying is blacked out too (OCR misses and splits lines)
* OCR text: identifiers replaced (engine.replace)
* payload / answer key: every string value de-identified the same way, with the same surrogates
  and date shift; values under identity keys (name, abha, phone, address, dob, ...) are replaced
  outright even when the detector misses them
* one ``DocumentKey`` per document: consistent inside it, unlinkable across documents

The result carries counts per entity type and the versions used, never the removed values.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw

from cdi_adapter.config import get_settings
from cdi_adapter.deid.engine import DocumentKey, Span, detect, replace, surrogate

# payload keys whose values identify the patient whatever the detector says (S4 schemas, §5)
IDENTITY_KEYS = re.compile(
    r"(?i)^(name|patient_?name|full_?name|abha(_?(number|address|id))?|aadhaar|pan|phone|"
    r"mobile|email|address|street|city|pin_?code|dob|date_?of_?birth|birth_?date|mrn|uhid|"
    r"legacy_?mrn|mpi_?id|patient_?id|guardian|father_?name|husband_?name|relative|"
    r"emergency_?contact)$"
)  # fmt: skip
MARGIN = 4  # px around a redacted OCR line


@dataclass
class OcrLine:
    text: str
    bbox: list[int]  # x0, y0, x1, y1 in page pixels


@dataclass
class Page:
    png: bytes
    lines: list[OcrLine]


@dataclass
class Result:
    pages: list[bytes]
    ocr_text: list[str]
    payload: Any
    counts: dict[str, int] = field(default_factory=dict)
    redacted_lines: int = 0

    def manifest(self) -> dict[str, Any]:
        s = get_settings()
        detector = {"spacy": s.deid_spacy_model, "min_score": s.deid_min_score,
                    "ner": f"{s.deid_ner_model}@{s.deid_ner_revision}"}  # fmt: skip
        return {"entities": dict(sorted(self.counts.items())), "pages": len(self.pages),
                "redacted_lines": self.redacted_lines, "detector": detector}  # fmt: skip


def _count(counts: dict[str, int], spans: list[Span]) -> None:
    for s in spans:
        counts[s.entity] = counts.get(s.entity, 0) + 1


def ocr_lines(png: bytes) -> list[OcrLine]:
    """RapidOCR (as S3) with no confidence cut-off: a line S3 would drop as unsure is still
    readable on the image, so de-identification must see it too."""
    from cdi_adapter.ocr.rapid import run_rapidocr

    return [OcrLine(ln.text, ln.bbox) for ln in run_rapidocr(png, min_conf=0.0)]


def _hits(lines: list[OcrLine]) -> tuple[str, list[Span], set[int]]:
    """Analyse the page as one text (names split across words are still found) and map every
    hit back to the OCR lines it touches."""
    joined, starts = "", []
    for ln in lines:
        starts.append(len(joined))
        joined += ln.text + "\n"
    spans = detect(joined)
    hit = {i for s in spans for i, st in enumerate(starts)
           if st < s.end and s.start < st + len(lines[i].text)}  # fmt: skip
    return joined, spans, hit


def _black_out(png: bytes, boxes: list[list[int]]) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN], fill="black")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def redact_page(page: Page, key: DocumentKey, counts: dict[str, int],
                reocr: Callable[[bytes], list[OcrLine]] | None = None,
                passes: int = 3) -> tuple[bytes, str, int]:  # fmt: skip
    """(redacted PNG, de-identified OCR text, lines blacked out).

    With ``reocr``, the redacted image is read again and anything still identifying is blacked
    out too, until a pass finds nothing (at most ``passes``): OCR misses or splits lines, and
    the image, not the OCR text, is what leaves the pod.
    """
    joined, spans, hit = _hits(page.lines)
    _count(counts, spans)
    png = _black_out(page.png, [page.lines[i].bbox for i in sorted(hit)])
    redacted, late = len(hit), []
    for _ in range(passes if reocr else 0):
        again = reocr(png)  # type: ignore[misc]
        _, more, more_hit = _hits(again)
        if not more_hit:
            break
        _count(counts, more)
        late += [again[i].bbox for i in sorted(more_hit)]
        png = _black_out(png, [again[i].bbox for i in sorted(more_hit)])
        redacted += len(more_hit)
    text = replace(joined, spans, key).rstrip("\n")
    if late:  # found only on the image: the text layer must not keep it either
        out = text.split("\n")
        if len(out) == len(page.lines):
            for i, ln in enumerate(page.lines):
                if i not in hit and any(_overlap(ln.bbox, b) for b in late):
                    out[i] = "[REDACTED]"
            text = "\n".join(out)
        else:  # a replacement spanned lines: no safe line mapping, drop the whole text layer
            text = "[REDACTED]"
    return png, text, redacted


def _overlap(a: list[int], b: list[int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def deidentify_value(value: Any, key: DocumentKey, counts: dict[str, int],
                     field_name: str = "") -> Any:  # fmt: skip
    if isinstance(value, dict):
        return {k: deidentify_value(v, key, counts, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [deidentify_value(v, key, counts, field_name) for v in value]
    if not isinstance(value, str) or not value.strip():
        return value
    if IDENTITY_KEYS.match(field_name):
        kind = "PERSON" if re.search(r"(?i)name|guardian|relative", field_name) else (
            "DATE_TIME" if re.search(r"(?i)dob|birth", field_name) else "OTHER_PHI")  # fmt: skip
        counts[kind] = counts.get(kind, 0) + 1
        return surrogate(Span(kind, 0, len(value), 1.0), value, key)
    spans = detect(value)
    _count(counts, spans)
    return replace(value, spans, key)


def deidentify_document(
    pages: list[Page],
    payload: Any = None,
    key: DocumentKey | None = None,
    reocr: Callable[[bytes], list[OcrLine]] | None = ocr_lines,
) -> Result:
    """``reocr=None`` skips the image self-check (tests with synthetic pages only)."""
    key = key or DocumentKey()
    counts: dict[str, int] = {}
    out_pages, texts, redacted = [], [], 0
    for page in pages:
        png, text, n = redact_page(page, key, counts, reocr)
        out_pages.append(png)
        texts.append(text)
        redacted += n
    clean = deidentify_value(payload, key, counts) if payload is not None else None
    return Result(out_pages, texts, clean, counts, redacted)
