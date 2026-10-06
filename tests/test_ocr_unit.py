"""E0-S5 unit: reading order, quad->bbox, conf filter, engine choice, RapidOCR, VLM OCR."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from cdi_adapter.config import Settings
from cdi_adapter.ml.client import StubMLClient
from cdi_adapter.ocr import rapid, vlm_ocr
from cdi_adapter.ocr.service import choose_engine
from tests.helpers import png_bytes


def _ln(text, x0, y0, x1, y1):
    return rapid.OcrLine(text=text, bbox=[x0, y0, x1, y1], conf=0.9)


def test_quad_to_bbox():
    assert rapid.quad_to_bbox([[10.2, 20], [110.7, 22], [109, 40.6], [9.8, 38]]) == [9, 20, 111, 41]


def test_order_reading_top_to_bottom_left_to_right():
    lines = [
        _ln("second-line-right", 400, 102, 600, 122),
        _ln("first-line", 50, 50, 300, 70),
        _ln("second-line-left", 50, 100, 300, 120),  # 2 px higher than its neighbour: same band
        _ln("third", 50, 150, 300, 170),
    ]
    out = rapid.order_reading(lines)
    assert [ln.text for ln in out] == ["first-line", "second-line-left", "second-line-right",
                                        "third"]  # fmt: skip
    assert [ln.reading_order for ln in out] == [0, 1, 2, 3]


def test_order_reading_empty():
    assert rapid.order_reading([]) == []


def test_lines_from_result_filters_low_confidence_and_blank():
    quad = [[0, 0], [10, 0], [10, 5], [0, 5]]
    raw = [[quad, "keep", 0.95], [quad, "drop", 0.29], [quad, "edge", 0.30], [quad, "  ", 0.99]]
    out = rapid.lines_from_result(raw, min_conf=0.30)
    assert sorted(ln.text for ln in out) == ["edge", "keep"]
    assert all(ln.polygon == [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]] for ln in out)
    assert rapid.lines_from_result(None, 0.3) == []


def _s(**kw):
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize(
    "force,cls,settings,expected",
    [
        ("rapidocr", {"is_handwritten": True}, _s(), "rapidocr"),  # force wins
        ("vlm", None, _s(), "vlm"),
        (None, {"is_handwritten": True}, _s(), "vlm"),
        (None, {"is_handwritten": True}, _s(handwritten_uses_vlm=False), "rapidocr"),
        (None, {"is_handwritten": False}, _s(), "rapidocr"),
        (None, None, _s(), "rapidocr"),
        (None, None, _s(ocr_engine="vlm"), "vlm"),
    ],
)
def test_choose_engine(force, cls, settings, expected):
    assert choose_engine(force, cls, settings) == expected


def test_choose_engine_rejects_unknown():
    with pytest.raises(ValueError):
        choose_engine("tesseract", None, _s())


def test_real_rapidocr_reads_printed_text_with_boxes():
    im = Image.new("RGB", (1400, 500), "white")
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=56)
    d.text((80, 80), "Tab Metformin 500 mg", fill="black", font=font)
    d.text((80, 260), "HbA1c 7.8 %", fill="black", font=font)
    lines = rapid.run_rapidocr(png_bytes(im), min_conf=0.3)
    text = " ".join(ln.text for ln in lines).lower()
    assert "metformin" in text and "500" in text and "hba1c" in text.replace(" ", "")
    first = next(ln for ln in lines if "metformin" in ln.text.lower())
    assert 60 <= first.bbox[0] <= 120 and 60 <= first.bbox[1] <= 110  # where it was drawn
    assert first.conf >= 0.8
    assert [ln.reading_order for ln in lines] == list(range(len(lines)))
    assert lines[0].bbox[1] < lines[-1].bbox[1]  # top line first


def test_vlm_transcription_uses_page_bbox_and_low_confidence():
    class Echo(StubMLClient):
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
            assert image_png == b"png" and json_schema is None
            return "Rx\n\n  Tab Dolo 650 SOS  \n"

    lines = vlm_ocr.run_vlm_transcription(b"png", 1654, 2339, Echo())
    assert [ln.text for ln in lines] == ["Rx", "Tab Dolo 650 SOS"]
    assert all(ln.bbox == [0, 0, 1654, 2339] and ln.conf == 0.55 for ln in lines)
    assert [ln.reading_order for ln in lines] == [0, 1]


def test_stub_transcription_without_text_returns_nothing():
    assert vlm_ocr.run_vlm_transcription(b"png", 10, 10, StubMLClient()) == []
