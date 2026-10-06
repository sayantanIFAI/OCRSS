"""E2-S3: grammar-locked decoding. Schema-invalid JSON is impossible by construction: the
grammar rejects the token.

The pure checks always run. The proof runs the **real XGrammar 0.1.25** (the version vLLM 0.11.0
pins) with the **real Qwen2.5-VL tokenizer**, token by token, as vLLM does on the pod. It needs
``xgrammar`` (the gateway venv, or ``.tools/xgrammar`` here) and the tokenizer in the Hugging Face
cache; without them it is skipped.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any

import pytest

from cdi_adapter.ml.client import bundled_schema, schema_names, validate_schema
from cdi_adapter.mlserve import grammar as g

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION = [n for n in schema_names() if n != "common.defs"]
TOKENIZER_REVISION = "66285546d2b821cf421d4f5eb2576359d3770cd3"
TOKENIZER = "Qwen/Qwen2.5-VL-3B-Instruct"  # the 7B uses the same vocabulary


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}  # fmt: skip


# ------------------------------------------------------------------ pure (always run)


@pytest.mark.parametrize("name", EXTRACTION)
def test_every_extraction_schema_can_be_locked_exactly(name: str) -> None:
    assert g.unsupported(bundled_schema(name)) == []


def test_every_confidence_becomes_the_exact_placeholder() -> None:
    for name in EXTRACTION:
        schema = g.with_placeholders(bundled_schema(name))
        assert "maximum" not in json.dumps(schema)  # every bounded number was a 0-1 confidence
        assert '"__cdi_unit_interval__"' in json.dumps(schema)


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"type": "number", "minimum": 0, "maximum": 100}, "number range other than 0-1"),
        ({"type": "string", "format": "date"}, "format"),
        ({"type": "array", "items": {"type": "string"}, "uniqueItems": True}, "uniqueItems"),
        ({"type": "integer", "multipleOf": 5}, "multipleOf"),
        ({"type": "string", "pattern": "^[0-9]+$"}, "pattern"),
        ({"$ref": "cdi:common.defs#/$defs/x"}, "$ref"),
        ({"type": "object", "minProperties": 1}, "minProperties"),
    ],
)
def test_features_the_grammar_cannot_enforce_are_named(
    schema: dict[str, Any], problem: str
) -> None:
    found = g.unsupported(_obj({"x": schema}))
    assert found and problem in found[0] and found[0].startswith("#/properties/x")


def test_a_property_named_like_a_keyword_is_not_a_keyword() -> None:
    assert g.unsupported(_obj({"format": {"type": "string"}, "pattern": {"type": "string"}})) == []


# ------------------------------------------------------------------ the real grammar engine


def unavailable(reason: str) -> None:
    if os.environ.get("CDI_TEST_REQUIRE_GRAMMAR") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def xgr() -> Any:
    try:
        import xgrammar
    except ImportError:
        local = ROOT / ".tools" / "xgrammar"
        if not local.exists():
            unavailable("xgrammar not installed (gateway venv, or .tools/xgrammar)")
        sys.path.append(str(local))
        import xgrammar
    return xgrammar


@pytest.fixture(scope="module")
def compiler(xgr: Any) -> tuple[Any, Any]:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        import transformers
    except ImportError:
        unavailable("transformers not installed")
    try:
        tok = transformers.AutoTokenizer.from_pretrained(
            TOKENIZER, revision=TOKENIZER_REVISION, local_files_only=True
        )
    except OSError:
        unavailable(f"{TOKENIZER} tokenizer not in the Hugging Face cache")
    info = xgr.TokenizerInfo.from_huggingface(tok)
    return xgr.GrammarCompiler(info), info


def _finishes(xgr: Any, ctx: Any, text: str) -> bool:
    """True when the model may emit ``text`` and then stop: a complete, allowed output."""
    m = xgr.GrammarMatcher(ctx)
    return bool(m.accept_string(text)) and bool(m.accept_token(m.stop_token_ids[0]))


def _locked(compiler: tuple[Any, Any], schema: dict[str, Any]) -> Any:
    return compiler[0].compile_grammar(g.schema_to_ebnf(schema))


def test_xgrammar_is_the_version_vllm_pins(xgr: Any) -> None:
    from importlib.metadata import version

    lock = (ROOT / "infra" / "runpod" / "requirements-vllm.lock").read_text(encoding="utf-8")
    try:
        installed = version("xgrammar")
    except Exception:  # .tools/xgrammar is a plain --target install: read its dist-info
        (info,) = (ROOT / ".tools" / "xgrammar").glob("xgrammar-*.dist-info")
        installed = info.name.split("-")[1].removesuffix(".dist")
    assert f"xgrammar=={installed} " in lock


def test_the_plain_conversion_lets_confidence_1_8_through(xgr: Any, compiler: Any) -> None:
    """Why the 0-1 rule is replaced: XGrammar's own number range is not exact."""
    schema = _obj({"c": {"type": "number", "minimum": 0, "maximum": 1}})
    plain = compiler[0].compile_json_schema(json.dumps(schema), any_whitespace=False)
    assert _finishes(xgr, plain, '{"c": 1.8}')  # the XGrammar 0.1.25 gap
    exact = _locked(compiler, schema)
    for ok in ("0", "1", "0.5", "0.985", "1.0", "0.000001"):
        assert _finishes(xgr, exact, '{"c": ' + ok + "}"), ok
    for bad in ("1.8", "1.01", "2", "-0.1", "0.1234567", "1e0", '"0.5"'):
        assert not _finishes(xgr, exact, '{"c": ' + bad + "}"), bad


@pytest.mark.parametrize(
    ("label", "prefix"),
    [
        ("unknown key", '{"doc_type": "prescription", "notes"'),
        ("string where a number belongs", '{"doc_type": "prescription", "confidence": "'),
        ("value outside the enum", '{"doc_type": "invoice'),
        ("confidence above 1", '{"doc_type": "prescription", "confidence": 1.5'),
        ("negative confidence", '{"doc_type": "prescription", "confidence": -'),
        ("page 0 (minimum 1)", '{"doc_type": "prescription", "page_spans": [{"start_page": 0'),
        ("prose before the JSON", "Here is the JSON"),
        ("markdown fence", "```json"),
    ],
)
def test_the_grammar_rejects_the_first_invalid_token(
    xgr: Any, compiler: Any, label: str, prefix: str
) -> None:
    ctx = _locked(compiler, bundled_schema("classification.v1"))
    assert not xgr.GrammarMatcher(ctx).accept_string(prefix), label


def test_the_rejection_prefixes_start_from_an_accepted_output(xgr: Any, compiler: Any) -> None:
    """Guards the test above: each bad prefix differs from an accepted one only at the end."""
    ctx = _locked(compiler, bundled_schema("classification.v1"))
    for ok in (
        '{"doc_type": "prescription", "confidence": 0.9',
        '{"doc_type": "prescription", "page_spans": [{"start_page": 1',
        '{"doc_type": "',
        "{",
    ):
        assert xgr.GrammarMatcher(ctx).accept_string(ok), ok


def test_a_missing_required_key_cannot_be_closed(xgr: Any, compiler: Any) -> None:
    ctx = _locked(compiler, bundled_schema("classification.v1"))
    assert not _finishes(xgr, ctx, '{"doc_type": "prescription"}')  # confidence is required
    assert _finishes(xgr, ctx, '{"doc_type": "prescription", "confidence": 0.9}')


def test_properties_come_in_schema_order(xgr: Any, compiler: Any) -> None:
    """The order the prompt asks for; the grammar must not reorder keys (e.g. by sorting)."""
    ctx = _locked(compiler, bundled_schema("classification.v1"))
    assert _finishes(xgr, ctx, '{"doc_type": "lab_report", "is_handwritten": false, '
                               '"confidence": 0.95}')  # fmt: skip
    assert not xgr.GrammarMatcher(ctx).accept_string('{"confidence"')


@pytest.mark.parametrize("name", EXTRACTION)
def test_every_output_the_grammar_allows_is_schema_valid(
    xgr: Any, compiler: Any, name: str
) -> None:
    """Random generation through the token mask, the way vLLM samples: pick any token the grammar
    allows (biased towards closing brackets so outputs finish) until the stop token. Every
    finished output must parse and validate against the real schema, ``cdi:`` refs and all."""
    import numpy as np

    info = compiler[1]
    ctx = _locked(compiler, bundled_schema(name))
    vocab, n_vocab = info.decoded_vocab, info.vocab_size
    closing = np.zeros(n_vocab, dtype=bool)
    closing[: len(vocab)] = [bool(t) and all(c in b'"]}' for c in t) for t in vocab]
    stops = set(info.stop_token_ids)
    mask = xgr.allocate_token_bitmask(1, n_vocab)
    rng = random.Random(name)
    confidences = 0
    for _ in range(15):
        m, ids = xgr.GrammarMatcher(ctx), []
        for _step in range(1500):
            m.fill_next_token_bitmask(mask)
            bits = np.unpackbits(mask.numpy().view(np.uint8), bitorder="little")[:n_vocab]
            allowed = bits.astype(bool)
            idx = np.flatnonzero(allowed)
            stop = [i for i in idx if i in stops]
            if stop and rng.random() < 0.7:
                tid = int(stop[0])
            else:
                close = np.flatnonzero(allowed & closing)
                pool = close if len(close) and rng.random() < 0.5 else idx
                tid = int(pool[rng.randrange(len(pool))])
            assert m.accept_token(tid)
            if m.is_terminated():
                break
            ids.append(tid)
        assert m.is_terminated(), "a walk did not finish (raise the step cap)"
        text = b"".join(vocab[i] for i in ids).decode("utf-8")
        assert validate_schema(json.loads(text), name) == [], text[:300]
        confidences += text.count('"confidence"')
    assert confidences >= 15  # the 0-1 rule was exercised


def test_schema_keyword_property_names_keep_confidence_bounds(xgr: Any, compiler: Any) -> None:
    ctx = _locked(compiler, _obj({"const": g.UNIT_INTERVAL}))
    assert _finishes(xgr, ctx, '{"const": 0.5}')
    assert not _finishes(xgr, ctx, '{"const": 1.8}')
