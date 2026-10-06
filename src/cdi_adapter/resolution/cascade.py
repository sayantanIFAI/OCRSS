"""The evidence hierarchy and the cheapest-first resolution cascade (backlog E4-S8, §15.6).

Pure and deterministic: ``resolve`` takes the evidence matrix (every ``Signal`` with its tier) and
what each built cascade level found, and returns a ``Decision`` with a machine-readable trace.
The precedence is data, in the versioned ``cascade.v1.json``; this module only applies it:

1. **Negative constraints** eliminate candidates (with the rule), never add one.
2. **Tier 0** (pixels) vetoes: a tier-0 contradiction removes the candidate.
3. **Tier 0/1 conflict forces review**: independent readings (or grounding) that support
   different candidates, or contradict the leading one, send the fact to review whatever the
   priors say - majority voting is never sufficient.
4. **Tier 3 priors only re-rank** candidates already supported by tiers 0-2; a prior for any other
   candidate is ignored, and a prior never resolves anything.
5. **The cascade** L0 -> L8 runs level by level, each only while the previous is ambiguous.
   A level not built is skipped, never faked. A level may short-circuit (accept) only where the
   config says it is validated for the field and cohort; otherwise it escalates. L8 is human
   review.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

POLICY_FILE = Path(__file__).with_name("cascade.v1.json")

Relation = Literal["supports", "contradicts", "eliminates", "reranks"]


@dataclass(frozen=True)
class Signal:
    """One entry of the evidence matrix: what one source says about one candidate."""

    source: str  # "rapidocr", "trocr", "qwen", "grounding", "terminology:exact", "prior:doctor"
    tier: int | None  # 0-3; None for a negative constraint
    candidate: str
    relation: Relation
    score: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class LevelResult:
    """What one built cascade level found: one candidate, or ambiguity / nothing."""

    candidate: str | None
    ambiguous: bool = False
    reason: str = ""


@dataclass
class Decision:
    outcome: Literal["accept", "review"]
    winner: str | None  # accepted value, or the top-ranked candidate shown to the reviewer
    resolved_by_level: str
    escalation_cause: str | None
    ranking: list[str]
    matrix: list[dict[str, Any]]
    trace: list[dict[str, Any]] = field(default_factory=list)
    policy: str = ""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Level:
    id: str
    name: str
    built: bool
    implemented_by: str | None = None
    backlog: str | None = None


@dataclass(frozen=True)
class Policy:
    version: str
    levels: tuple[Level, ...]
    validated: tuple[tuple[str, str, str, str], ...]  # (level, cohort, field or "*", basis)
    conflict_tiers: tuple[int, ...]

    def validated_for(self, level: str, field_: str, cohort: str) -> str | None:
        """The validation basis when ``level`` may short-circuit for this field/cohort."""
        for lvl, co, fld, basis in self.validated:
            if lvl == level and co == cohort and fld in ("*", field_) and basis != "assumed":
                return basis
        return None


def parse_policy(raw: dict[str, Any]) -> Policy:
    levels = tuple(Level(id=x["id"], name=x["name"], built=bool(x["built"]),
                         implemented_by=x.get("implemented_by"), backlog=x.get("backlog"))
                   for x in raw["levels"])  # fmt: skip
    ids = [lv.id for lv in levels]
    if (
        not ids
        or len(set(ids)) != len(ids)
        or ids != sorted(ids, key=lambda i: int(i[1:]))
        or ids[-1] != "L8"
    ):
        raise ValueError("cascade levels must run in order and end with L8 (human review)")
    for lv in levels:
        if lv.built and not lv.implemented_by:
            raise ValueError(f"built level {lv.id} must name what implements it")
        if not lv.built and not lv.backlog:
            raise ValueError(f"unbuilt level {lv.id} must name its backlog story")
    if not levels[-1].built:
        raise ValueError("L8 (human review) must always be available")
    validated = []
    for v in raw.get("validated_for", []):
        if v["basis"] not in raw.get("validation_basis", {}):
            raise ValueError(f"unknown validation basis {v['basis']!r}")
        for lvl in v["levels"]:
            if lvl not in ids or not next(x for x in levels if x.id == lvl).built:
                raise ValueError(f"{lvl} is validated but not a built level")
            for fld in v["fields"]:
                validated.append((lvl, v["cohort"], fld, v["basis"]))
    rules = raw["rules"]
    if rules.get("majority_vote_sufficient") or not rules.get("priors_rerank_only"):
        raise ValueError("the precedence rules are fixed: no majority vote, priors only re-rank")
    if set(rules["conflict_forces_review_tiers"]) != {0, 1}:
        raise ValueError("tier 0 and tier 1 conflicts must force review")
    return Policy(version=raw["version"], levels=levels, validated=tuple(validated),
                  conflict_tiers=tuple(rules["conflict_forces_review_tiers"]))  # fmt: skip


@cache
def load_policy(path: Path = POLICY_FILE) -> Policy:
    return parse_policy(json.loads(path.read_text(encoding="utf-8")))


def _best_tier(signals: list[Signal], cand: str) -> int:
    tiers = [s.tier for s in signals
             if s.candidate == cand and s.relation == "supports" and s.tier is not None
             and s.tier <= 2]  # fmt: skip
    return min(tiers) if tiers else 99


def resolve(signals: list[Signal], levels: dict[str, LevelResult], *, field_: str, cohort: str,
            policy: Policy | None = None) -> Decision:  # fmt: skip
    policy = policy or load_policy()
    trace: list[dict[str, Any]] = []
    matrix = [asdict(s) for s in signals]

    def step(kind: str, **kw: Any) -> None:
        trace.append({"step": kind, **kw})

    order: list[str] = []
    for s in signals:
        if s.candidate not in order:
            order.append(s.candidate)

    # 1. negative constraints only eliminate
    eliminated = {s.candidate: s for s in signals if s.relation == "eliminates"}
    for c, s in eliminated.items():
        step("eliminate", candidate=c, source=s.source, reason=s.reason)

    # 2. tier 0 vetoes
    vetoed = {s.candidate for s in signals if s.tier == 0 and s.relation == "contradicts"}
    for c in sorted(vetoed):
        step("veto", tier=0, candidate=c, reason="pixel grounding contradicts it")

    # 3. supported by tiers 0-2 (priors never create a candidate)
    supported = [c for c in order if c not in eliminated and c not in vetoed
                 and _best_tier(signals, c) <= 2]  # fmt: skip
    for s in signals:
        if s.tier == 3 and s.candidate not in supported:
            step("prior_ignored", source=s.source, candidate=s.candidate,
                 reason="priors only re-rank candidates supported by tiers 0-2")  # fmt: skip
    prior = {c: sum(s.score or 0 for s in signals if s.tier == 3 and s.candidate == c)
             for c in supported}  # fmt: skip
    # strongest evidence tier first; a prior only orders candidates tied on it
    ranking = sorted(supported, key=lambda c: (_best_tier(signals, c), -prior[c]))
    if len(ranking) > 1 and any(prior.values()):
        step("rerank", ranking=ranking, reason="tier-3 priors among equally supported candidates")

    def review(cause: str) -> Decision:
        step("escalate", level="L8", cause=cause)
        return Decision("review", ranking[0] if ranking else None, "L8", cause, ranking, matrix,
                        trace, policy.version)  # fmt: skip

    # 4. a tier-0/1 conflict forces review, whatever the priors and however many signals agree
    for t in policy.conflict_tiers:
        sup = {s.candidate for s in signals if s.tier == t and s.relation == "supports"
               and s.candidate not in eliminated}  # fmt: skip
        if len(sup) > 1:
            srcs = ", ".join(f"{s.source}: {s.candidate!r}" for s in signals
                             if s.tier == t and s.relation == "supports")  # fmt: skip
            return review(f"tier-{t} disagreement ({srcs})")
    cross_tier = {
        s.candidate for s in signals if s.tier in policy.conflict_tiers and s.relation == "supports"
    }
    if (
        len(cross_tier) > 1
        and len(
            {
                s.tier
                for s in signals
                if s.relation == "supports" and s.tier in policy.conflict_tiers
            }
        )
        > 1
    ):
        return review("tier-0/1 disagreement across evidence tiers")
    contradictions = [
        s for s in signals if s.tier in policy.conflict_tiers and s.relation == "contradicts"
    ]
    if contradictions:
        s = contradictions[0]
        return review(f"tier-{s.tier} contradiction ({s.source})")
    if ranking:
        against = [s for s in signals if s.tier in policy.conflict_tiers
                   and s.relation == "contradicts" and s.candidate == ranking[0]]  # fmt: skip
        if against:
            a = against[0]
            return review(f"tier-{a.tier} contradiction of {ranking[0]!r} "
                          f"({a.source}: {a.reason or 'contradicts'})")  # fmt: skip
    if not ranking:
        return review("no candidate is supported by tiers 0-2")

    # 5. the cascade, cheapest first; each level only while the previous is ambiguous
    for lv in policy.levels:
        if lv.id == "L8":
            break
        if not lv.built:
            step("skip", level=lv.id, reason=f"not built ({lv.backlog})")
            continue
        r = levels.get(lv.id)
        if r is None:
            step("no_result", level=lv.id, reason="does not apply to this fact")
            continue
        if r.ambiguous or r.candidate is None:
            step("ambiguous", level=lv.id, reason=r.reason or "no single candidate")
            continue
        if r.candidate not in supported:
            step("unsupported", level=lv.id, candidate=r.candidate,
                 reason="not supported by tiers 0-2: a level never creates one")  # fmt: skip
            continue
        basis = policy.validated_for(lv.id, field_, cohort)
        if basis is None:
            step("not_validated", level=lv.id, candidate=r.candidate,
                 reason=f"not validated for {field_} / {cohort}: escalate")  # fmt: skip
            continue
        step("resolved", level=lv.id, candidate=r.candidate, validation=basis, reason=r.reason)
        winner = r.candidate
        return Decision("accept", winner, lv.id, None,
                        [winner, *[c for c in ranking if c != winner]], matrix, trace,
                        policy.version)  # fmt: skip
    return review("no built level validated for this field and cohort resolved it")
