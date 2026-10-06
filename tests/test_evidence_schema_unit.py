"""E4-S7 (unit): recognition carries no codes; an observation hashes the pixels it was read from."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw

from cdi_adapter.config import Settings
from cdi_adapter.ocr import rapid, vlm_ocr
from cdi_adapter.ocr.service import _observation_meta, crop_hashes

ROOT = Path(__file__).resolve().parents[1]
RECOGNIZER_SCHEMAS = sorted((ROOT / "schemas" / "recognizer").glob("*.json"))
CODE_FIELDS = {"code", "code_system", "concept", "code_display", "system", "coding"}


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key in ("properties", "patternProperties"):
            for name, sub in (schema.get(key) or {}).items():
                names.add(name)
                names |= _property_names(sub)
        for key in ("items", "additionalProperties", "$defs", "anyOf", "oneOf", "allOf"):
            sub = schema.get(key)
            for s in sub.values() if key == "$defs" and isinstance(sub, dict) else (
                    sub if isinstance(sub, list) else [sub]):  # fmt: skip
                names |= _property_names(s)
    return names


def test_there_is_a_recognizer_output_schema():
    assert [p.name for p in RECOGNIZER_SCHEMAS] == ["ocr_observation.v1.json"]


@pytest.mark.parametrize("path", RECOGNIZER_SCHEMAS, ids=lambda p: p.name)
def test_recognizer_output_schemas_contain_no_code_fields(path: Path):
    """AC: recognizer outputs carry no code_system / code / concept (normalization is separate)."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert not _property_names(schema) & CODE_FIELDS
    assert schema.get("additionalProperties") is False  # nothing can be slipped in


def test_the_engines_line_type_has_no_code_fields():
    assert not {f.name for f in dataclasses.fields(rapid.OcrLine)} & CODE_FIELDS


def _page(w=400, h=200) -> bytes:
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, 180, 40], fill=0)
    d.rectangle([20, 100, 300, 130], fill=90)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_crop_hash_is_the_pixels_at_the_bbox():
    png = _page()
    lines = [rapid.OcrLine(text="a", bbox=[20, 20, 180, 40], conf=0.9),
             rapid.OcrLine(text="b", bbox=[20, 100, 300, 130], conf=0.9),
             rapid.OcrLine(text="c", bbox=[20, 20, 180, 40], conf=0.8),
             rapid.OcrLine(text="off", bbox=[500, 500, 600, 600], conf=0.9)]  # fmt: skip
    a, b, c, off = crop_hashes(png, lines)
    assert a == c and a != b  # same pixels -> same hash, whatever the text or confidence
    assert off is None  # no pixels to point at
    crop = Image.open(io.BytesIO(png)).convert("RGB").crop((20, 20, 180, 40))
    expected = hashlib.sha256(f"{crop.width}x{crop.height}:".encode() + crop.tobytes())
    assert a == expected.hexdigest()


def test_observations_validate_against_the_recognizer_schema():
    schema = json.loads(RECOGNIZER_SCHEMAS[0].read_text(encoding="utf-8"))
    v = Draft202012Validator(schema)
    s = Settings()
    png = _page()
    printed = rapid.OcrLine(text="Tab X", bbox=[20, 20, 180, 40], conf=0.97)
    page = rapid.OcrLine(text="Tab X", bbox=[0, 0, 400, 200], conf=vlm_ocr.VLM_OCR_CONF,
                         block_type="page")  # fmt: skip
    for engine, ln in (("rapidocr", printed), ("vlm", page)):
        meta = _observation_meta(engine, s)
        row = {"engine": engine, "block_type": ln.block_type, "raw_text": ln.text,
               "bbox": ln.bbox, "polygon": ln.polygon, "ocr_conf": ln.conf,
               "reading_order": ln.reading_order, "crop_hash": crop_hashes(png, [ln])[0],
               "field_domain": None, "token_confidences": None, **meta}  # fmt: skip
        assert not list(v.iter_errors(row)), engine
    assert _observation_meta("vlm", s)["prompt_hash"] == hashlib.sha256(
        vlm_ocr.TRANSCRIBE_PROMPT.encode()).hexdigest()  # fmt: skip
    assert _observation_meta("rapidocr", s)["region_kind"] == "printed"
