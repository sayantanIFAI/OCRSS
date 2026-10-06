"""Printed-text OCR with RapidOCR on ONNX Runtime, CPU (ARCHITECTURE §5 S3).

RapidOCR returns ``[quad, text, score]`` per line; a line is kept if ``score >= CDI_OCR_MIN_CONF``
(0.30); the quad becomes ``bbox [x0, y0, x1, y1]`` and the polygon is retained.
``order_reading`` sorts lines into natural top-to-bottom, left-to-right order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from statistics import median
from typing import Any

from cdi_adapter import _cpu
from cdi_adapter.config import get_settings

ENGINE = "rapidocr"


@dataclass
class OcrLine:
    text: str
    bbox: list[int]
    conf: float
    polygon: list[list[float]] | None = None
    reading_order: int = 0
    block_type: str = "line"
    extra: dict[str, Any] = field(default_factory=dict)


def quad_to_bbox(quad) -> list[int]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return [int(min(xs)), int(min(ys)), int(round(max(xs))), int(round(max(ys)))]


def order_reading(lines: list[OcrLine]) -> list[OcrLine]:
    """Sort by horizontal band ``round(y_center / (0.7 * median_height))`` then ``x0``."""
    if not lines:
        return []
    heights = [max(1, ln.bbox[3] - ln.bbox[1]) for ln in lines]
    band_h = 0.7 * median(heights)

    def key(ln: OcrLine) -> tuple[int, int]:
        y_center = (ln.bbox[1] + ln.bbox[3]) / 2
        return (round(y_center / band_h), ln.bbox[0])

    ordered = sorted(lines, key=key)
    for i, ln in enumerate(ordered):
        ln.reading_order = i
    return ordered


@lru_cache
def get_engine():
    from rapidocr_onnxruntime import RapidOCR

    threads = _cpu.per_worker_threads(_cpu.cpu_budget(), get_settings().job_max_workers)
    try:
        return RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1)
    except TypeError:  # older RapidOCR without the thread kwargs
        return RapidOCR()


def lines_from_result(result, min_conf: float) -> list[OcrLine]:
    """Convert raw RapidOCR output (``[[quad, text, score], ...]`` or None) to kept lines."""
    out: list[OcrLine] = []
    for quad, text, score in result or []:
        score = float(score)
        if score < min_conf or not str(text).strip():
            continue
        out.append(
            OcrLine(
                text=str(text).strip(),
                bbox=quad_to_bbox(quad),
                conf=round(score, 4),
                polygon=[[float(x), float(y)] for x, y in quad],
            )
        )
    return order_reading(out)


def run_rapidocr(png: bytes, *, min_conf: float | None = None) -> list[OcrLine]:
    result, _elapsed = get_engine()(png)
    return lines_from_result(result, get_settings().ocr_min_conf if min_conf is None else min_conf)
