"""E15-S2: clinical field grammars, as constrained decoding and as a validator.

The acceptance has two halves and this file covers both:

* **an out-of-grammar numeric reading can't be emitted by the constrained decoder** - the field's
  rule is compiled into the document grammar by the real XGrammar 0.1.25 (the version vLLM 0.11.0
  pins) and the reading is checked against it by the engine itself, so "cannot be emitted" is
  measured, not asserted;
* **and is flagged when produced by an unconstrained engine** - the same grammar, as the S6 rule
  ``check_field_grammars``.

``1-0-1`` vs ``1-7-1`` is the fixture the story names, and it runs in both halves.

The pure checks always run. The XGrammar half needs ``xgrammar`` (the gateway venv, or
``.tools/xgrammar`` here); without it those tests are skipped, never passed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from cdi_adapter import fieldgrammar as fg
from cdi_adapter.mlserve import grammar as mg
from cdi_adapter.validate import rules

ROOT = Path(__file__).resolve().parents[1]

# (domain, reading, in grammar?) - the whole corpus, used by the pure checks AND by the
# XGrammar agreement test, so the decoder and the validator are judged on identical cases.
CORPUS: tuple[tuple[str, str, bool], ...] = (
    # the story's fixture
    ("dose", "1-0-1", True),
    ("dose", "1-7-1", False),
    ("dose", "0-0-1", True),
    ("dose", "1-½-1", True),
    ("dose", "1/2-0-1", True),
    ("dose", "0.5-0-0.5", True),
    ("dose", "1½-0-1", True),
    ("dose", "2-2-2", True),
    ("dose", "4-0-4", True),
    ("dose", "5-0-5", False),  # above the slot ceiling: a count, not any number
    ("dose", "10-0-1", False),
    ("dose", "1-0", False),
    ("dose", "1-0-1-1", False),
    ("dose", "1-O-1", False),  # the letter O, the classic OCR slip
    ("dose", "1 - 0 - 1", False),
    # strength: <number><unit>
    ("strength", "500mg", True),
    ("strength", "500 mg", True),
    ("strength", "2.5 mL", True),
    ("strength", "40mcg", True),
    ("strength", "5%", True),
    ("strength", "500", False),  # no unit
    ("strength", "mg", False),  # no number
    ("strength", "500 mgx", False),
    ("strength", "0.500mg", False),  # three decimals is a reading artefact
    ("strength", "500mg bd", False),
    # duration
    ("duration", "x5d", True),
    ("duration", "x5 days", True),
    ("duration", "x2wk", True),
    ("duration", "14/52", True),
    ("duration", "x14/52", True),
    ("duration", "5 dayz", False),
    ("duration", "x5 months", False),
    ("duration", "x1000d", False),
    # frequency: the closed set, and the spellings the project's own parser knows
    ("frequency", "OD", True),
    ("frequency", "BD", True),
    ("frequency", "TDS", True),
    ("frequency", "QID", True),
    ("frequency", "HS", True),
    ("frequency", "SOS", True),
    ("frequency", "PRN", True),
    ("frequency", "twice daily", True),
    ("frequency", "q8h", True),
    ("frequency", "ODX", False),
    # a prescription here writes the frequency AS a dose pattern, and the extractor stores it
    # that way (parse.freq_per_day reads 1-0-1 as 2/day), so the dose grammar judges it
    ("frequency", "1-0-1", True),
    ("frequency", "0-0-1", True),
    ("frequency", "1-7-1", False),
    ("frequency", "", False),  # absent is not judged here, and never "in grammar"
)


# ------------------------------------------------------------------ the grammars themselves


@pytest.mark.parametrize(("domain", "reading", "ok"), CORPUS)
def test_the_grammar_accepts_exactly_what_it_should(domain: str, reading: str, ok: bool) -> None:
    assert fg.accepts(domain, reading) is ok, f"{domain} {reading!r}"


def test_the_story_fixture() -> None:
    """1-0-1 is a prescription; 1-7-1 is a misread, and the grammar says so."""
    assert fg.accepts("dose", "1-0-1")
    assert not fg.accepts("dose", "1-7-1")
    why = fg.out_of_grammar("dose", "1-7-1")
    assert why and "1-7-1" in why and "d-d-d" in why


def test_the_four_domains_the_story_names() -> None:
    assert fg.DOMAINS == ("strength", "dose", "duration", "frequency")
    assert set(fg.STORY_FREQUENCY_CODES) <= set(fg.FREQUENCY_CODES)


def test_strength_and_dose_are_the_strict_domains() -> None:
    """E4-S3 calls the numeric fields the strict ones, so a violation there blocks auto-accept
    while an unknown frequency or duration asks for a human."""
    assert fg.severity("strength") == fg.severity("dose") == "blocker"
    assert fg.severity("frequency") == fg.severity("duration") == "warn"


def test_a_frequency_written_as_a_broken_dose_is_a_blocker() -> None:
    """``frequency: "1-7-1"`` is a misread number wherever it is written, so it is reported as
    the dose it meant, at dose severity - not as a merely unknown frequency word."""
    assert fg.severity("frequency", "1-7-1") == "blocker"
    assert fg.severity("frequency", "ODX") == "warn"
    why = fg.out_of_grammar("frequency", "1-7-1")
    assert why and why.startswith("dose ")
    assert fg.looks_like_dose("1-7-1") and not fg.looks_like_dose("OD")


def test_a_reading_is_judged_not_rewritten() -> None:
    """E4-S7: the image stays authoritative. Normalisation may only trim and collapse spaces."""
    assert fg.normalise("  1-0-1 ") == "1-0-1"
    assert fg.normalise("x5  days") == "x5 days"
    assert fg.normalise(None) == ""
    assert fg.out_of_grammar("dose", None) is None  # absent is not a violation
    assert fg.out_of_grammar("dose", "   ") is None


# ------------------------------------------------------------------ the validator half


def _med(**kw: Any) -> dict[str, Any]:
    return {"fact_type": "medication", "drug_text": "Amoxicillin", **kw}


def test_an_out_of_grammar_reading_is_flagged() -> None:
    found = rules.check_field_grammars(_med(), {"dose": "1-7-1"})
    assert [(f.severity, f.code) for f in found] == [("blocker", "field-out-of-grammar")]
    assert "1-7-1" in found[0].message


def test_an_in_grammar_reading_is_not_flagged() -> None:
    assert rules.check_field_grammars(_med(), {"dose": "1-0-1", "strength": "500mg"}) == []


def test_the_stored_frequency_reading_is_judged_too() -> None:
    """``medication_detail.frequency_code`` keeps the engine's reading verbatim, so it is judged
    even when no payload reading is passed."""
    assert rules.check_field_grammars(_med(frequency_code="OD")) == []
    found = rules.check_field_grammars(_med(frequency_code="0D"))  # zero, not the letter O
    assert [(f.severity, f.code) for f in found] == [("warn", "field-out-of-grammar")]


def test_every_bad_field_is_reported_once(_pytest: Any = None) -> None:
    found = rules.check_field_grammars(_med(), {"dose": "1-7-1", "strength": "500",
                                                "duration": "5 dayz"})  # fmt: skip
    assert len(found) == 3
    assert {f.severity for f in found} == {"blocker", "warn"}
    assert all(f.code == "field-out-of-grammar" for f in found)


def test_only_medications_are_judged() -> None:
    assert rules.check_field_grammars({"fact_type": "lab_result"}, {"dose": "1-7-1"}) == []


def test_a_structured_quantity_is_left_to_the_numeric_rules() -> None:
    """Only the engine's own string is judged: a parsed ``{value, unit}`` has already been read,
    and re-rendering it would judge our formatting, not the reading."""
    assert fg.readings({"strength": {"value": 500, "unit": "mg"}}) == {}
    assert fg.readings({"strength": "500mg", "dose": "1-0-1"}) == {"strength": "500mg",
                                                                   "dose": "1-0-1"}  # fmt: skip


def test_the_payload_readings_are_keyed_by_drug_name() -> None:
    payload = {"medications": [{"drug_name": " Amoxicillin ", "dose": "1-7-1"},
                               {"drug_name": "Paracetamol", "frequency": "SOS"},
                               "not a medication"]}  # fmt: skip
    out = fg.readings_by_drug(payload)
    assert out["amoxicillin"] == {"dose": "1-7-1"}
    assert out["paracetamol"] == {"frequency": "SOS"}
    assert fg.readings_by_drug(None) == {} and fg.readings_by_drug({}) == {}


# ------------------------------------------------------------------ the decoder half


@pytest.fixture(scope="module")
def xgr() -> Any:
    try:
        import xgrammar
    except ImportError:
        local = ROOT / ".tools" / "xgrammar"
        if not local.exists():
            pytest.skip("xgrammar not installed (gateway venv, or .tools/xgrammar)")
        sys.path.append(str(local))
        import xgrammar
    return xgrammar


def _accepts_by_engine(xgr: Any, domain: str, reading: str) -> bool:
    """Does the real XGrammar engine accept this reading as a whole field?"""
    compiled = xgr.GrammarCompiler(xgr.TokenizerInfo([])).compile_grammar(
        xgr.Grammar.from_ebnf(fg.ebnf(domain, terminated=True))
    )
    matcher = xgr.GrammarMatcher(compiled)
    return bool(matcher.accept_string(fg.normalise(reading) + fg.END))


@pytest.mark.parametrize(("domain", "reading", "ok"), CORPUS)
def test_the_decoder_and_the_validator_agree(xgr: Any, domain: str, reading: str,
                                             ok: bool) -> None:  # fmt: skip
    """One definition, two renderings: the EBNF the decoder is given and the regex the pipeline
    uses must accept exactly the same readings, or the grammar would mean two different things."""
    if not fg.normalise(reading):
        pytest.skip("an absent reading is not a grammar question")
    assert _accepts_by_engine(xgr, domain, reading) is ok
    assert fg.accepts(domain, reading) is ok


def test_an_out_of_grammar_dose_cannot_be_emitted(xgr: Any) -> None:
    """The acceptance's first half, measured: the engine refuses ``1-7-1`` at the ``7``."""
    compiled = xgr.GrammarCompiler(xgr.TokenizerInfo([])).compile_grammar(
        xgr.Grammar.from_ebnf(fg.ebnf("dose"))
    )
    matcher = xgr.GrammarMatcher(compiled)
    assert matcher.accept_string("1-")  # fine so far
    assert not matcher.accept_string("7")  # the slot grammar has no 7: the token is refused
    assert _accepts_by_engine(xgr, "dose", "1-0-1")


def test_a_field_locked_schema_compiles_to_the_fields_own_rule(xgr: Any) -> None:
    """E2-S3's converter, extended: a property that names a domain is no longer "any string"."""
    schema = {
        "type": "object",
        "properties": {
            "drug_name": {"type": "string"},
            "dose": {"type": "string", "x-cdi-field": "dose"},
            "frequency": {"type": "string", "x-cdi-field": "frequency"},
        },
        "required": ["drug_name"],
        "additionalProperties": False,
    }
    assert mg.unsupported(schema) == []  # the annotation is not an unenforceable keyword
    ebnf = mg.schema_to_ebnf(schema)
    assert "cdi_field_dose ::=" in ebnf and "cdi_field_frequency ::=" in ebnf
    assert "__cdi_field_dose__" not in ebnf  # the marker was swapped, not left in
    xgr.Grammar.from_ebnf(ebnf)  # the parse vLLM runs before accepting the request


def test_an_unknown_domain_is_refused_loudly(xgr: Any) -> None:
    """A schema naming a field we have no grammar for fails in CI, not silently unconstrained."""
    schema = {"type": "object", "properties": {"x": {"type": "string", "x-cdi-field": "weight"}},
              "additionalProperties": False}  # fmt: skip
    with pytest.raises(mg.GrammarError, match="unknown clinical field domain"):
        mg.schema_to_ebnf(schema)


def test_the_document_schemas_still_compile(xgr: Any) -> None:
    """The extension must not disturb E2-S3: every real schema still grammar-locks exactly."""
    from cdi_adapter.ml.client import bundled_schema, schema_names

    for name in schema_names():
        schema = bundled_schema(name)
        assert mg.unsupported(schema) == [], name
        assert mg.schema_to_ebnf(schema)
