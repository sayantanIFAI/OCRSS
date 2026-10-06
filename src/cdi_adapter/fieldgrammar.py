"""Clinical field grammars (backlog E15-S2, HW-Phase B; §3.4, §5 S6).

E2-S3 locks the *document* schema: the model can only emit JSON that matches it. It says nothing
about what goes **inside** a field, so ``dose: "1-7-1"`` is schema-valid and clinically impossible.
This module is the field half of the same idea - the four prescription fields whose grammar is
closed, written once and used twice:

* as **constrained decoding**: ``mlserve.grammar`` swaps a field's schema node for the exact
  XGrammar rule from here, so an out-of-grammar reading is not a token the model may emit;
* as a **validator** on any engine's output (RapidOCR, TrOCR, an unconstrained VLM), so a reading
  that *was* produced out of grammar is flagged instead of travelling on (S6 finding
  ``field-out-of-grammar``).

The grammars (from the story):

| domain      | grammar                                                              |
|-------------|----------------------------------------------------------------------|
| ``strength``| ``<number><unit>`` - the unit from the project's own UCUM aliases     |
| ``dose``    | ``d-d-d``, each slot a number or a half (``½``, ``1/2``)             |
| ``duration``| ``x<n>d`` \\| ``x<n> days`` \\| ``x<n>wk`` \\| ``x<n>/52``           |
| ``frequency``| the closed set OD, BD, TDS, QID, HS, SOS, PRN                       |

**One definition, two renderings.** Each grammar is a small expression tree (:class:`Seq`,
:class:`Alt`, …) rendered to XGrammar EBNF *and* to a regular expression. The decoder uses the
EBNF, the validator uses the compiled regex (it runs in the pipeline process, which has no
``xgrammar``), and ``tests/test_fieldgrammar_unit.py`` asserts the two agree on every fixture with
the **real XGrammar 0.1.25** - so they cannot drift apart silently.

A reading is judged only when the field is **present and non-empty**: an absent dose is
``check_medication``'s business (``med-no-dose``), not a grammar violation.

**The validator half is live; the decoder lock is opt-in.** Any schema property may carry
``"x-cdi-field": "<domain>"`` and ``mlserve.grammar`` then locks it to that field's rule - but the
shipped document schemas are **not** annotated yet, deliberately. Locking a free-text clinical
field means the model can no longer write what is on the page when the page is outside the
grammar (a doctor who writes "alternate day" in the frequency slot), and whether that trades well
is a question for the E2-S4 gold set, not an assumption. Until that is measured, an out-of-grammar
reading is *flagged* rather than *impossible*, which is the same no-guess posture as the rest of
S6. Annotating a schema is then a one-line change per field, with the grammar already proven.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------- the expression tree


@dataclass(frozen=True)
class Lit:
    """An exact string."""

    text: str


@dataclass(frozen=True)
class Cls:
    """A character class, written as its members (``"0-9"`` is a range)."""

    members: str


@dataclass(frozen=True)
class Seq:
    parts: tuple[Any, ...]

    def __init__(self, *parts: Any) -> None:
        object.__setattr__(self, "parts", tuple(parts))


@dataclass(frozen=True)
class Alt:
    options: tuple[Any, ...]

    def __init__(self, *options: Any) -> None:
        object.__setattr__(self, "options", tuple(options))


@dataclass(frozen=True)
class Opt:
    inner: Any


@dataclass(frozen=True)
class Rep:
    """``inner`` repeated between ``lo`` and ``hi`` times (inclusive)."""

    inner: Any
    lo: int
    hi: int


def _ebnf_of(node: Any) -> str:
    if isinstance(node, Lit):
        return _ebnf_literal(node.text)
    if isinstance(node, Cls):
        return f"[{node.members}]"
    if isinstance(node, Seq):
        return "(" + " ".join(_ebnf_of(p) for p in node.parts) + ")"
    if isinstance(node, Alt):
        return "(" + " | ".join(_ebnf_of(o) for o in node.options) + ")"
    if isinstance(node, Opt):
        return f"{_ebnf_of(node.inner)}?"
    if isinstance(node, Rep):
        return f"{_ebnf_of(node.inner)}{{{node.lo},{node.hi}}}"
    raise TypeError(f"not a grammar node: {node!r}")


def _ebnf_literal(text: str) -> str:
    """A literal as XGrammar writes one: a quoted string with non-ASCII escaped, so the grammar
    file stays ASCII and ``½`` cannot be mangled by an encoding on the way to the gateway."""
    out = []
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 32 or ord(ch) > 126:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _regex_of(node: Any) -> str:
    if isinstance(node, Lit):
        return re.escape(node.text)
    if isinstance(node, Cls):
        return f"[{node.members}]"
    if isinstance(node, Seq):
        return "(?:" + "".join(_regex_of(p) for p in node.parts) + ")"
    if isinstance(node, Alt):
        return "(?:" + "|".join(_regex_of(o) for o in node.options) + ")"
    if isinstance(node, Opt):
        return f"{_regex_of(node.inner)}?"
    if isinstance(node, Rep):
        return f"{_regex_of(node.inner)}{{{node.lo},{node.hi}}}"
    raise TypeError(f"not a grammar node: {node!r}")


# ---------------------------------------------------------------- the clinical fields

_DIGIT = Cls("0-9")
# a plain number: 1, 10, 0.5, 12.25 - no leading zeros, at most 2 decimals (no clinical field
# needs more, and "0.500" is a reading artefact worth flagging)
NUMBER = Seq(
    Alt(Lit("0"), Seq(Cls("1-9"), Rep(_DIGIT, 0, 3))),
    Opt(Seq(Lit("."), Rep(_DIGIT, 1, 2))),
)
HALF = Alt(Lit("½"), Lit("1/2"))  # ½ as written, and as typed

# A dose slot is a **count per administration**, and that is what makes ``1-7-1`` the story's
# worked example: 7 is a perfectly good number, so a slot of "any number" would emit it. The slot
# is therefore a closed set - 0 to DOSE_SLOT_MAX in halves - so an implausible count is not a
# string the grammar can produce. Raising the ceiling is a deliberate edit here, not a side effect
# of how a number is written; a prescription that really needs more is flagged for review instead.
DOSE_SLOT_MAX = 4
_HALF_STEPS: list[Any] = []
for _n in range(DOSE_SLOT_MAX, -1, -1):  # longest first: "1½" before "1", "1.5" before "1"
    for _frac in (f"{_n}½", f"{_n}.5", f"{_n}"):
        if _n == 0 and _frac == "0½":
            continue  # "0½" is not written; a half is "½"
        _HALF_STEPS.append(Lit(_frac))
DOSE_SLOT = Alt(Lit("½"), Lit("1/2"), Lit(".5"), *_HALF_STEPS)

# the units a strength may carry: the project's own UCUM aliases (terminology/parse.py), as a
# doctor writes them, longest first so "mcg" cannot be read as "mg" + "cg"
STRENGTH_UNITS = (
    "mcg", "µg", "ug", "mg/ml", "mg/mL", "mg", "gm", "g", "ml", "mL", "IU", "iu", "U", "%",
)  # fmt: skip
# The closed frequency set the story names - and, in the same closed set, every spelling the
# project's own parser already understands (``terminology.parse``), so a reading this pipeline
# handles correctly is never flagged as out of grammar. Both cases are enumerated because
# XGrammar EBNF has no case-insensitive form, and the two renderings must stay identical.
STORY_FREQUENCY_CODES = ("OD", "BD", "TDS", "QID", "HS", "SOS", "PRN")


def _frequency_vocabulary() -> tuple[str, ...]:
    from cdi_adapter.terminology.parse import FREQ_PER_DAY, PRN_TERMS

    words = {*STORY_FREQUENCY_CODES}
    words |= {c.lower() for c in STORY_FREQUENCY_CODES}
    words |= set(FREQ_PER_DAY) | set(PRN_TERMS)
    words |= {w.upper() for w in (*FREQ_PER_DAY, *PRN_TERMS) if len(w) <= 4}
    # longest first: "bid" must not be read as "bi" + "d"
    return tuple(sorted(words, key=lambda w: (-len(w), w)))


FREQUENCY_CODES = _frequency_vocabulary()

_UNIT = Alt(*[Lit(u) for u in STRENGTH_UNITS])
_WEEKS = Alt(Lit("wk"), Lit("wks"), Lit("week"), Lit("weeks"))
_DAYS = Alt(Lit("d"), Lit("day"), Lit("days"))

DOSE_PATTERN = Seq(DOSE_SLOT, Lit("-"), DOSE_SLOT, Lit("-"), DOSE_SLOT)

FIELDS: dict[str, Any] = {
    # <number><unit>, with the space a doctor may or may not leave
    "strength": Seq(NUMBER, Opt(Lit(" ")), _UNIT),
    # d-d-d, each slot a number or a half: 1-0-1, 1-½-1, 0.5-0-0.5
    "dose": DOSE_PATTERN,
    # x<n>d | x<n> days | x<n>wk | x<n>/52
    "duration": Seq(
        Opt(Alt(Lit("x"), Lit("X"))),
        Rep(_DIGIT, 1, 3),
        Alt(
            Seq(Opt(Lit(" ")), _DAYS),
            Seq(Opt(Lit(" ")), _WEEKS),
            Lit("/52"),
        ),
    ),
    # the closed set - **or a dose pattern**, because that is how a prescription here says it:
    # ``frequency: "1-0-1"`` is what the extractor stores and what ``parse.freq_per_day`` reads
    # (1-0-1 -> 2/day). A d-d-d frequency is therefore judged by the dose grammar, so ``1-7-1``
    # is out of grammar wherever it is written.
    "frequency": Alt(*[Lit(c) for c in FREQUENCY_CODES], DOSE_PATTERN),
}
DOMAINS = tuple(FIELDS)
# E4-S3 calls strength and dose the strictest numeric fields; a violation there is a blocker,
# while a frequency or duration the grammar does not know is a warning worth a human's eye
BLOCKER_DOMAINS = ("strength", "dose")


# ---------------------------------------------------------------- what the two sides use

# the validator's sentinel: a complete reading, and nothing more. XGrammar's matcher only reports
# termination on a stop token, so the validation grammar ends with a character no field contains.
END = "\u0003"


def ebnf(domain: str, *, terminated: bool = False) -> str:
    """The XGrammar EBNF for one field. ``terminated`` appends the validator's end sentinel, so a
    partial reading cannot be accepted as a whole one."""
    node = FIELDS[domain]
    body = _ebnf_of(node) + (f" {_ebnf_literal(END)}" if terminated else "")
    return f"root ::= {body}\n"


@lru_cache(maxsize=16)
def pattern(domain: str) -> re.Pattern[str]:
    """The compiled, fully anchored regex for one field - the same grammar, rendered for the
    pipeline process (which has no ``xgrammar``)."""
    return re.compile(f"\\A{_regex_of(FIELDS[domain])}\\Z")


def normalise(text: str | None) -> str:
    """What is judged: the reading with its surrounding whitespace removed and inner runs of
    whitespace collapsed. Nothing else is changed - the grammar judges the reading, it never
    rewrites it (E4-S7: the image stays authoritative)."""
    return re.sub(r"\s+", " ", str(text or "").strip())


def accepts(domain: str, text: str | None) -> bool:
    """Is this reading in the field's grammar? An empty reading is not judged here."""
    value = normalise(text)
    return bool(value) and bool(pattern(domain).match(value))


def looks_like_dose(text: str) -> bool:
    """Is this reading an *attempt* at a d-d-d pattern? ``1-7-1`` is, ``OD`` is not - so a
    frequency written as a broken dose pattern is judged (and reported) as the dose it meant."""
    return bool(re.fullmatch(r"[0-9½./\-]{3,}", text)) and "-" in text


def out_of_grammar(domain: str, text: str | None) -> str | None:
    """``None`` when the reading is in grammar (or absent); else why it is not, for the finding."""
    value = normalise(text)
    if not value or accepts(domain, value):
        return None
    meant = "dose" if domain == "frequency" and looks_like_dose(value) else domain
    return f"{meant} {value!r} is not {DESCRIPTIONS[meant]}"


DESCRIPTIONS = {
    "strength": "a number with a unit (e.g. 500mg, 2.5 mL)",
    "dose": f"three slots d-d-d, each 0-{DOSE_SLOT_MAX} in halves (e.g. 1-0-1, 1-½-1)",
    "duration": "a count of days or weeks (e.g. x5d, x2 weeks, x14/52)",
    "frequency": f"one of {', '.join(STORY_FREQUENCY_CODES)} (or a parser spelling)",
}


def severity(domain: str, text: str | None = None) -> str:
    """How bad an out-of-grammar reading is. A numeric field is a blocker (E4-S3 calls strength
    and dose the strict ones), and so is a frequency written as a broken dose pattern - it is a
    misread number either way."""
    value = normalise(text)
    if domain == "frequency" and value and looks_like_dose(value):
        return "blocker"
    return "blocker" if domain in BLOCKER_DOMAINS else "warn"


# ---------------------------------------------------------------- the engine's own readings


def _string_reading(value: Any) -> str | None:
    """A field's reading **as the engine wrote it**, or ``None``.

    A ``quantity`` arrives either as a bare string ("500 mg") or as ``{value, unit}``. Only the
    bare string is judged here: a structured quantity was already parsed into numbers, so its
    grammar is the decoder's business (and its plausibility ``check_medication``'s), and
    re-rendering it would mean judging our own formatting instead of the engine's reading.
    """
    if isinstance(value, str):
        text = normalise(value)
        return text or None
    return None


def readings(med: dict[str, Any]) -> dict[str, str]:
    """The per-field string readings of one extracted medication (the payload object)."""
    out = {}
    for domain, key in (("strength", "strength"), ("dose", "dose"),
                        ("duration", "duration"), ("frequency", "frequency")):  # fmt: skip
        reading = _string_reading(med.get(key))
        if reading is not None:
            out[domain] = reading
    return out


def readings_by_drug(payload: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Every medication of an extraction payload, keyed by its drug name as written (normalised
    and folded), so the gate can find the readings behind a medication fact."""
    out: dict[str, dict[str, str]] = {}
    for med in (payload or {}).get("medications") or []:
        if not isinstance(med, dict):
            continue
        name = normalise(med.get("drug_name")).lower()
        if name:
            out[name] = readings(med)
    return out
