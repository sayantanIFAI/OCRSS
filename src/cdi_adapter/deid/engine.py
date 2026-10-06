"""Find and replace direct identifiers in text (backlog E11-S4; ARCHITECTURE §12).

    Presidio AnalyzerEngine
      NLP: spaCy ``en_core_web_lg`` (PERSON / GPE / LOC / ORG / DATE)
      + the Indian recognizers (recognizers.py)
      + the clinical de-id NER (ner.py)
    -> spans merged, recall first (the lowest threshold that still ignores noise)
    -> replaced (``replace``): consistent surrogate names, per-document date shift keeping the
       format and the intervals, typed placeholders for every identifier, ages over 89 -> "90+".

Nothing here keeps a mapping from surrogate back to the original: de-identified output cannot be
reversed from what this module writes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from cdi_adapter.config import get_settings

# identifiers replaced by a typed placeholder (never a fake value that could be a real one)
PLACEHOLDER = {
    "IN_ABHA_NUMBER": "[ABHA]", "IN_ABHA_ADDRESS": "[ABHA-ADDRESS]", "IN_AADHAAR": "[AADHAAR]",
    "IN_PAN": "[PAN]", "IN_PIN_CODE": "[PIN]", "MEDICAL_RECORD_NUMBER": "[ID]",
    "PHONE_NUMBER": "[PHONE]", "EMAIL_ADDRESS": "[EMAIL]", "LOCATION": "[ADDRESS]",
    "ORGANIZATION": "[FACILITY]", "OTHER_PHI": "[REDACTED]", "URL": "[URL]", "IP_ADDRESS": "[IP]",
}  # fmt: skip
# spaCy / Presidio built-in labels folded into ours
ALIASES = {"GPE": "LOCATION", "NRP": None, "DATE": "DATE_TIME", "ORG": "ORGANIZATION"}
ENTITIES = sorted({*PLACEHOLDER, "PERSON", "DATE_TIME", "AGE"})

SURROGATE_FIRST = ("Arun", "Bina", "Chitra", "Devan", "Esha", "Farid", "Gauri", "Hari", "Indu",
                   "Jatin", "Kala", "Lalit", "Mala", "Naren", "Oviya", "Prem", "Rekha", "Sunil",
                   "Tara", "Uday", "Veda", "Yash")  # fmt: skip
SURROGATE_LAST = (
    "Acharya",
    "Bhat",
    "Chauhan",
    "Dutta",
    "Gill",
    "Hegde",
    "Jain",
    "Kaul",
    "Lobo",
    "Mistry",
    "Nayak",
    "Oberoi",
    "Pande",
    "Rana",
    "Saxena",
    "Thakur",
)

_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d %b, %Y",
    "%b %d, %Y",
    "%d %b %y",
)


@dataclass(frozen=True)
class Span:
    entity: str
    start: int
    end: int
    score: float
    source: str = ""


@dataclass
class DocumentKey:
    """Per-document secrets: surrogate choice and date shift. Never written anywhere."""

    salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    shift_days: int = field(default_factory=lambda: secrets.choice(
        [d for d in range(-365, 366) if abs(d) >= 30]))  # fmt: skip

    def pick(self, pool: tuple[str, ...], original: str) -> str:
        h = hmac.new(self.salt, original.lower().encode(), hashlib.sha256).digest()
        return pool[int.from_bytes(h[:4], "big") % len(pool)]


# ---------------------------------------------------------------- detection


@lru_cache(maxsize=1)
def analyzer() -> Any:
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    from cdi_adapter.deid.ner import ClinicalNerRecognizer
    from cdi_adapter.deid.recognizers import india_recognizers

    s = get_settings()
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": s.deid_spacy_model}],
    }).create_engine()  # fmt: skip
    registry = RecognizerRegistry(supported_languages=["en"])
    registry.load_predefined_recognizers(languages=["en"], nlp_engine=nlp)
    for r in india_recognizers():
        registry.add_recognizer(r)
    registry.add_recognizer(ClinicalNerRecognizer(s.deid_ner_model, s.deid_ner_revision))
    return AnalyzerEngine(registry=registry, nlp_engine=nlp, supported_languages=["en"])


def detect(text: str, *, min_score: float | None = None) -> list[Span]:
    """Identifier spans in ``text``: overlapping hits merged, the widest span wins."""
    if not text.strip():
        return []
    threshold = get_settings().deid_min_score if min_score is None else min_score
    raw = analyzer().analyze(text=text, language="en", score_threshold=threshold)
    spans = []
    for r in raw:
        entity = ALIASES.get(r.entity_type, r.entity_type)
        if entity is None or (entity not in ENTITIES):
            continue
        src = getattr(r.analysis_explanation, "recognizer", "") if r.analysis_explanation else ""
        span = tidy(text, Span(entity, r.start, r.end, r.score, src))
        if span is not None and not clinical_false_positive(text, span):
            spans.append(extend_digits(text, span))
    return merge(spans)


# a DATE_TIME hit is an identifier only if it names a calendar day (a duration such as "3 days"
# or "1 month" is clinical content, not a date of the patient's life)
_CALENDAR = re.compile(
    r"(?i)\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}|(?:19|20)\d{2}|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
)  # fmt: skip
# prescription / ward abbreviations the NER takes for an organisation or a place
CLINICAL_ABBREVIATIONS = frozenset("""sos od bd bid tds tid qid qds hs prn stat ac pc opd ipd icu
nicu ot er cbc lft kft rft esr crp ns1 bp hr rr spo2 ecg ekg usg ct mri tab cap inj syp iv im sc
po npo nbm rx dx""".split())  # fmt: skip


def clinical_false_positive(text: str, span: Span) -> bool:
    value = text[span.start : span.end]
    digits = sum(c.isdigit() for c in value)
    if span.entity == "DATE_TIME" and not _CALENDAR.search(value):
        return True
    if span.entity in ("ORGANIZATION", "LOCATION") and value.lower().strip(".") in (
        CLINICAL_ABBREVIATIONS
    ):
        return True
    # OCR drops spaces ("chills.for"): a URL needs a scheme, "www." or a real top-level domain
    if span.entity == "URL" and not re.search(
        r"(?i)://|\bwww\.|\.(?:com|in|org|net|gov|edu|co|io|info|health|nic)\b", value
    ):
        return True
    # an Indian phone number has 10 digits (+ country / trunk code): "150000-41" in a lab range
    # is a reference interval, not a phone
    if span.entity == "PHONE_NUMBER" and digits < 10:
        return True
    # the model's bare numbers ("88000" before "/uL") are lab values; a labelled record number is
    # caught by the MRN pattern (value after the label), so a model-only short number is dropped
    if (span.entity == "MEDICAL_RECORD_NUMBER" and span.source == "clinical_deid_ner"
            and re.fullmatch(r"[\d.,]+", value) and digits <= 7):  # fmt: skip
        return True
    return False


_ID_TYPES = frozenset({"IN_ABHA_NUMBER", "IN_AADHAAR", "IN_PIN_CODE", "MEDICAL_RECORD_NUMBER",
                       "PHONE_NUMBER"})  # fmt: skip


def extend_digits(text: str, span: Span) -> Span:
    """An id OCR broke apart ("48031611.6055", "465 007") is removed whole: the span grows over
    digit groups joined to it by one separator."""
    if span.entity not in _ID_TYPES:
        return span
    start, end = span.start, span.end
    grown = True
    while grown:
        grown = False
        m = re.match(r"[.\s-]?\d+", text[end:])
        if m and text[end - 1 : end].isdigit():
            end += m.end()
            grown = True
        b = re.search(r"\d+[.\s-]?$", text[:start])
        if b and text[start : start + 1].isdigit() and b.start() < start:
            start = b.start()
            grown = True
    return Span(span.entity, start, end, span.score, span.source)


# form labels printed next to the values: a hit on the label alone is not an identifier, and
# keeping the label keeps the document useful for training (E2-S4 answer keys name the fields)
LABELS = frozenset("""abha no no. address uhid mrn dob pan aadhaar aadhar mobile phone tel email
e-mail pin pincode code name patient s/o w/o d/o c/o age sex date consultant doctor dr dr. ref
referred by ip op opd ipd reg regn registration id uid mpi sample lab collected reported on at
visit next contact""".split())  # fmt: skip


def tidy(text: str, span: Span) -> Span | None:
    """Snap to whole words, then peel form labels off both edges; None if only labels remain."""
    start, end = span.start, span.end
    # a model hit that cuts a word ("AB|HA", "U|HID") covers the whole word; a digit glued to a
    # letter by OCR ("1987Age") is a real boundary and is left alone
    while start > 0 and text[start - 1].isalpha() and text[start].isalpha():
        start -= 1
    while end < len(text) and text[end - 1].isalpha() and text[end].isalpha():
        end += 1
    tokens = [(m.start() + start, m.end() + start, m.group())
              for m in re.finditer(r"\S+", text[start:end])]  # fmt: skip

    def is_label(tok: str) -> bool:  # noqa: E306
        return tok.lower().strip(":#.-,()") in LABELS or tok in (":", "#", "-", "/")

    while tokens and is_label(tokens[0][2]):
        tokens.pop(0)
    while tokens and is_label(tokens[-1][2]):
        tokens.pop()
    if not tokens:
        return None
    end = tokens[-1][1]
    while end > tokens[0][0] and text[end - 1] in ".,;:":  # sentence punctuation stays
        end -= 1
    return Span(span.entity, tokens[0][0], end, span.score, span.source)


def merge(spans: list[Span]) -> list[Span]:
    """Overlapping or touching spans become one; the type of the most specific (id > name >
    date > location) is kept, so a number inside a name hit is still an id."""
    # Indian ids first (an ABHA address is also e-mail shaped), then the generic ones
    ids = sorted(
        PLACEHOLDER.keys() - {"LOCATION", "ORGANIZATION", "OTHER_PHI"},
        key=lambda e: (not e.startswith("IN_"), e),
    )
    # a date outranks an age: an "age" hit that swallowed a date of birth must not keep it
    order = [*ids, "PERSON", "DATE_TIME", "AGE", "LOCATION", "ORGANIZATION", "OTHER_PHI"]
    rank = {e: i for i, e in enumerate(order)}
    out: list[Span] = []
    for s in sorted(spans, key=lambda x: (x.start, -x.end)):
        if out and s.start <= out[-1].end:
            prev = out[-1]
            keep = prev if rank.get(prev.entity, 99) <= rank.get(s.entity, 99) else s
            out[-1] = Span(keep.entity, prev.start, max(prev.end, s.end),
                           max(prev.score, s.score), keep.source)  # fmt: skip
        else:
            out.append(s)
    return out


# ---------------------------------------------------------------- replacement


def shift_date(value: str, days: int) -> str | None:
    """The same date format, moved by ``days``; None when it cannot be parsed."""
    from datetime import datetime

    clean = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value.strip(), flags=re.I)
    for fmt in _DATE_FORMATS:
        try:
            d: date = datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
        shifted = d + timedelta(days=days)
        out = shifted.strftime(fmt)
        return out.upper() if clean.isupper() else out
    return None


def surrogate(span: Span, original: str, key: DocumentKey) -> str:
    if span.entity == "PERSON":
        parts = original.split()
        if len(parts) >= 2:
            return f"{key.pick(SURROGATE_FIRST, parts[0])} {key.pick(SURROGATE_LAST, original)}"
        return key.pick(SURROGATE_FIRST, original)
    if span.entity == "DATE_TIME":
        return shift_date(original, key.shift_days) or "[DATE]"
    if span.entity == "AGE":
        # kept only when the text is an age and nothing else ("52", "52 Y", "65 yrs"): an "age" hit
        # can swallow a glued date of birth; ages <= 89 are not identifying, older ones are
        m = re.fullmatch(r"(?i)\s*(\d{1,3})\s*\.?\s*(?:y|yr|yrs|year|years|yo)?\.?\s*", original)
        if not m:
            return "[AGE]"
        return "90+" if int(m.group(1)) > 89 else original
    return PLACEHOLDER.get(span.entity, "[REDACTED]")


def replace(text: str, spans: list[Span], key: DocumentKey) -> str:
    out, last = [], 0
    for s in sorted(spans, key=lambda x: x.start):
        out.append(text[last : s.start])
        out.append(surrogate(s, text[s.start : s.end], key))
        last = s.end
    out.append(text[last:])
    return "".join(out)


def deidentify_text(text: str, key: DocumentKey | None = None) -> tuple[str, list[Span]]:
    key = key or DocumentKey()
    spans = detect(text)
    return replace(text, spans, key), spans
