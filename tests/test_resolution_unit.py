"""E4-S8 (unit): the evidence hierarchy + cheapest-first cascade (ARCHITECTURE §15.6).

The precedence is versioned config; the engine is pure. The three AC fixtures, the "majority
voting is never sufficient" rule, priors that only re-rank, and levels that are skipped, never
faked."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cdi_adapter.resolution.cascade import (
    POLICY_FILE,
    LevelResult,
    Signal,
    load_policy,
    parse_policy,
)
from cdi_adapter.resolution.cascade import (
    resolve as _resolve,
)

RAW = json.loads(POLICY_FILE.read_text(encoding="utf-8"))


def resolve(*args, **kwargs):
    # Positive resolution cases use an explicitly synthetic validation basis.
    p = load_policy()
    p = replace(
        p,
        validated=tuple((lv, co, fld, "synthetic-unit-fixture") for lv, co, fld, _ in p.validated),
    )
    return _resolve(*args, **kwargs, policy=p)


# ------------------------------------------------------------------ versioned config


def test_the_precedence_is_versioned_config():
    p = load_policy()
    assert p.version == RAW["version"] == "cascade.v1"
    assert [lv.id for lv in p.levels] == [f"L{i}" for i in range(9)]  # L0 reading .. L8 human
    assert {lv.id for lv in p.levels if not lv.built} == {"L4", "L5", "L6", "L7"}
    assert all(lv.backlog for lv in p.levels if not lv.built)
    assert all(lv.implemented_by for lv in p.levels if lv.built)
    assert p.conflict_tiers == (0, 1)
    assert [t["tier"] for t in RAW["tiers"]] == [0, 1, 2, 3]
    assert RAW["tiers"][3]["may"] == ["rerank"] and RAW["negative_constraints"]["may"] == [
        "eliminate"
    ]


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda r: r["rules"].update(majority_vote_sufficient=True), "no majority vote"),
        (lambda r: r["rules"].update(priors_rerank_only=False), "priors only re-rank"),
        (lambda r: r["levels"].pop(), "end with L8"),
        (lambda r: r["levels"][4].pop("backlog"), "backlog story"),
        (
            lambda r: r["validated_for"].append(
                {"levels": ["L6"], "cohort": "printed", "fields": ["*"], "basis": "assumed"}
            ),
            "not a built level",
        ),  # fmt: skip
        (lambda r: r["validated_for"][0].update(basis="guessed"), "unknown validation basis"),
    ],
)
def test_a_config_that_breaks_the_rules_is_refused(change, error):
    raw = json.loads(json.dumps(RAW))
    change(raw)
    with pytest.raises(ValueError, match=error):
        parse_policy(raw)


# ------------------------------------------------------------------ the three AC fixtures


def test_fixture_a_independent_readers_disagree_so_review_despite_frequency():
    """(a) TrOCR 'Telma 40' vs Qwen 'Telmikind 40', both valid, grounding can't separate ->
    review, even though Telma is more frequent."""
    signals = [
        Signal("trocr", 1, "Telma 40", "supports", 0.81),
        Signal("qwen", 1, "Telmikind 40", "supports", 0.79),
        Signal("terminology:exact", 2, "Telma 40", "supports", 0.9, "valid product"),
        Signal("terminology:exact", 2, "Telmikind 40", "supports", 0.9, "valid product"),
        Signal("grounding", 0, "Telma 40", "supports", 0.5, "cannot separate"),
        Signal("grounding", 0, "Telmikind 40", "supports", 0.5, "cannot separate"),
        Signal("prior:doctor-frequency", 3, "Telma 40", "reranks", 0.9, "ordered 212x"),
    ]
    levels = {"L1": LevelResult("Telma 40", reason="exact alias")}
    d = resolve(signals, levels, field_="medication", cohort="printed")
    assert d.outcome == "review" and d.resolved_by_level == "L8"
    assert "tier-0 disagreement" in d.escalation_cause or "tier-1" in d.escalation_cause
    assert d.winner == "Telma 40"  # the more frequent one is shown first, never accepted


def test_fixture_b_a_prior_cannot_override_the_pixels():
    """(b) the doctor usually orders HbA1c but the pixels support HBeAg -> the prior can't
    override: HBeAg is kept, HbA1c never wins."""
    signals = [
        Signal("grounding", 0, "HBeAg", "supports", 0.93, "crop matches HBeAg"),
        Signal("grounding", 0, "HbA1c", "contradicts", 0.9, "crop does not match HbA1c"),
        Signal("rapidocr", 1, "HBeAg", "supports", 0.95),
        Signal("terminology:exact", 2, "HBeAg", "supports", 0.9),
        Signal("terminology:exact", 2, "HbA1c", "supports", 0.9),
        Signal("prior:doctor-frequency", 3, "HbA1c", "reranks", 0.97, "ordered 540x"),
        Signal("prior:co-order", 3, "HbA1c", "reranks", 0.9),
    ]
    d = resolve(signals, {"L1": LevelResult("HBeAg")}, field_="lab_order", cohort="printed")
    assert d.outcome == "review" and d.winner == "HBeAg" and d.resolved_by_level == "L8"
    # Pixel contradictions require review even when the remaining candidate is clear.
    assert "HbA1c" not in d.ranking  # vetoed by the pixels; its priors never revive it
    assert {"step": "veto", "tier": 0, "candidate": "HbA1c",
            "reason": "pixel grounding contradicts it"} in d.trace  # fmt: skip
    assert any(t["step"] == "prior_ignored" and t["candidate"] == "HbA1c" for t in d.trace)
    # the same without grounding: the prior still cannot promote HbA1c over the reading
    no_pixels = [s for s in signals if s.tier != 0]
    d2 = resolve(no_pixels, {"L1": LevelResult("HBeAg")}, field_="lab_order", cohort="printed")
    assert d2.winner == "HBeAg" and d2.ranking[0] == "HBeAg"


def test_fixture_c_any_tier_0_or_1_conflict_forces_review_whatever_the_history():
    """(c) a tier-0/1 conflict -> review regardless of history; and however many priors and
    lexical matches agree, one explicit contradiction from an independent reading wins."""
    many_votes = [Signal(f"prior:{i}", 3, "Amlodipine 5", "reranks", 1.0) for i in range(9)]
    many_votes += [Signal("terminology:exact", 2, "Amlodipine 5", "supports", 0.9),
                   Signal("rapidocr", 1, "Amlodipine 5", "supports", 0.99)]  # fmt: skip
    htr = Signal("trocr", 1, "Amlodipine 5", "contradicts", 0.8, "reads 'Amlodipine 10'")
    d = resolve([*many_votes, htr], {"L1": LevelResult("Amlodipine 5")}, field_="medication",
                cohort="printed")  # fmt: skip
    assert d.outcome == "review" and "tier-1 contradiction" in d.escalation_cause
    pixels = Signal("grounding", 0, "Amlodipine 5", "contradicts", 0.9, "crop reads 10")
    d = resolve([*many_votes, pixels], {"L1": LevelResult("Amlodipine 5")},
                field_="medication", cohort="printed")  # fmt: skip
    assert d.outcome == "review"  # vetoed by the pixels: nothing left to accept
    # without the contradiction the same evidence is accepted at L1
    d = resolve(many_votes, {"L1": LevelResult("Amlodipine 5")}, field_="medication",
                cohort="printed")  # fmt: skip
    assert d.outcome == "accept" and d.resolved_by_level == "L1"


# ------------------------------------------------------------------ the other rules


def test_majority_voting_alone_is_never_sufficient():
    """Nine priors + a lexical match, but no level resolved it: review, not accept."""
    signals = [Signal(f"prior:{i}", 3, "X", "reranks", 1.0) for i in range(9)]
    signals.append(Signal("terminology:fuzzy", 2, "X", "supports", 0.9))
    d = resolve(signals, {}, field_="medication", cohort="printed")
    assert d.outcome == "review" and d.resolved_by_level == "L8"


def test_levels_not_built_are_skipped_never_faked():
    signals = [Signal("rapidocr", 1, "Pan 40", "supports", 0.9)]
    fake = {lv: LevelResult("Pan 40") for lv in ("L4", "L5", "L6", "L7")}  # unbuilt levels
    d = resolve(signals, fake, field_="medication", cohort="printed")
    assert d.outcome == "review"  # a result from an unbuilt level is never used
    skipped = [t["level"] for t in d.trace if t["step"] == "skip"]
    assert skipped == ["L4", "L5", "L6", "L7"]
    assert all("not built" in t["reason"] for t in d.trace if t["step"] == "skip")


def test_a_cheap_level_short_circuits_only_where_validated():
    signals = [Signal("vlm", 1, "Dolo 650", "supports", 0.55),
               Signal("terminology:exact", 2, "Dolo 650", "supports", 0.9)]  # fmt: skip
    levels = {"L1": LevelResult("Dolo 650")}
    printed = resolve(signals, levels, field_="medication", cohort="printed")
    handwritten = resolve(signals, levels, field_="medication", cohort="handwritten")
    assert printed.outcome == "accept" and printed.resolved_by_level == "L1"
    assert handwritten.outcome == "review"  # L1 is not validated for handwriting
    assert any(t["step"] == "not_validated" and t["level"] == "L1" for t in handwritten.trace)


def test_the_cascade_stops_at_the_first_level_that_resolves():
    signals = [Signal("rapidocr", 1, "Metformin", "supports", 0.95),
               Signal("terminology:alias", 2, "Metformin", "supports", 0.9)]  # fmt: skip
    levels = {"L1": LevelResult(None, ambiguous=True, reason="two aliases"),
              "L2": LevelResult("Metformin"), "L3": LevelResult("Metformin")}  # fmt: skip
    d = resolve(signals, levels, field_="medication", cohort="printed")
    assert d.resolved_by_level == "L2"
    assert [t["step"] for t in d.trace if t.get("level") in ("L1", "L2")] == [
        "ambiguous",
        "resolved",
    ]
    assert not any(t.get("level") == "L3" for t in d.trace)  # never ran


def test_constraints_only_eliminate_and_levels_never_create_candidates():
    signals = [Signal("rapidocr", 1, "Telma 40", "supports", 0.9),
               Signal("rule:strength-40-only", None, "Telma 400", "eliminates",
                      reason="no 400 mg strength")]  # fmt: skip
    d = resolve(signals, {"L1": LevelResult("Telma 400")}, field_="medication", cohort="printed")
    assert d.outcome == "review" and "Telma 400" not in d.ranking
    assert any(t["step"] == "unsupported" for t in d.trace)  # the level's answer was unsupported
    assert {"step": "eliminate", "candidate": "Telma 400", "source": "rule:strength-40-only",
            "reason": "no 400 mg strength"} in d.trace  # fmt: skip


def test_the_decision_trace_is_machine_readable():
    signals = [
        Signal("rapidocr", 1, "Hypertension", "supports", 0.97),
        Signal("terminology:exact", 2, "Hypertension", "supports", 0.9, "SNOMED 38341003"),
    ]
    d = resolve(signals, {"L1": LevelResult("Hypertension")}, field_="condition",
                cohort="printed")  # fmt: skip
    out = json.loads(json.dumps(d.as_json()))
    assert out["policy"] == "cascade.v1" and out["resolved_by_level"] == "L1"
    assert {"source", "tier", "candidate", "relation", "score", "reason"} <= set(out["matrix"][0])
    assert all("step" in t for t in out["trace"])
