"""Deterministic clinical text parsing shared by S4 (medication detail) and S5 (terminology).

* ``freq_per_day``: dose-slot columns (``1-0-1`` -> 2/day), Latin/English abbreviations
  (``BD`` -> 2/day, ``TDS`` -> 3/day) and long intervals (``TWICE IN A YEAR`` -> 0.006/day).
  Sub-daily frequencies are rounded **up** to 3 decimals so a dose ceiling check never
  under-counts.
* ``duration_days``: ``x 15 days`` / ``for 2 weeks`` / ``1 month`` — a duration, never a frequency.
* ``to_ucum``: unit text -> UCUM code.
"""

from __future__ import annotations

import math
import re

FREQ_PER_DAY: dict[str, float] = {
    "od": 1, "qd": 1, "once daily": 1, "once a day": 1, "daily": 1, "hs": 1, "at bedtime": 1,
    "bd": 2, "bid": 2, "twice daily": 2, "twice a day": 2,
    "tds": 3, "tid": 3, "thrice daily": 3, "three times a day": 3,
    "qid": 4, "four times a day": 4, "q6h": 4, "q8h": 3, "q12h": 2, "q4h": 6,
    "weekly": 1 / 7, "once a week": 1 / 7, "once weekly": 1 / 7, "twice weekly": 2 / 7,
    "monthly": 1 / 30, "once a month": 1 / 30,
}  # fmt: skip
PRN_TERMS = ("sos", "prn", "as needed", "when required", "if needed")

_PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_COUNT_WORDS = {"once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4}

UCUM_ALIASES: dict[str, str] = {
    "mg": "mg", "milligram": "mg", "mcg": "ug", "µg": "ug", "ug": "ug", "g": "g", "gm": "g",
    "gram": "g", "kg": "kg", "ml": "mL", "l": "L", "iu": "[iU]", "units": "[iU]", "unit": "[iU]",
    "u": "[iU]", "%": "%", "mg/dl": "mg/dL", "g/dl": "g/dL", "mmol/l": "mmol/L",
    "meq/l": "meq/L", "iu/l": "[iU]/L", "u/l": "U/L", "ng/ml": "ng/mL", "pg/ml": "pg/mL",
    "uiu/ml": "u[iU]/mL", "µiu/ml": "u[iU]/mL", "mmhg": "mm[Hg]", "mm hg": "mm[Hg]",
    "mm[hg]": "mm[Hg]", "/min": "/min", "bpm": "/min", "beats/min": "/min",
    "breaths/min": "/min", "c": "Cel", "°c": "Cel", "degc": "Cel", "cel": "Cel",
    "f": "[degF]", "°f": "[degF]", "degf": "[degF]", "cm": "cm", "m": "m", "in": "[in_i]",
    "kg/m2": "kg/m2", "kg/m²": "kg/m2", "cells/cumm": "/uL", "/cumm": "/uL", "/ul": "/uL",
    "10^3/ul": "10*3/uL", "lakh/cumm": "10*5/uL", "fl": "fL", "pg": "pg", "mm/hr": "mm/h",
}  # fmt: skip


def _round_up(x: float, places: int = 3) -> float:
    f = 10**places
    return math.ceil(x * f - 1e-9) / f


def freq_per_day(text: str | None) -> float | None:
    if not text:
        return None
    s = re.sub(r"\s+", " ", str(text).lower()).strip()
    if m := re.search(r"(?<!\d)(\d(?:\.\d)?)-(\d(?:\.\d)?)-(\d(?:\.\d)?)(?:-(\d(?:\.\d)?))?(?!\d)",
                      s):  # fmt: skip
        total = sum(float(g) for g in m.groups() if g is not None)
        return total or None
    if m := re.search(r"\b(once|twice|thrice|one|two|three|four|\d+)\s*(?:times?\s*)?"
                      r"(?:in|a|per|every)(?:\s+a)?\s*(day|week|month|year)\b", s):  # fmt: skip
        count = _COUNT_WORDS.get(m.group(1)) or float(m.group(1))
        return _round_up(count / _PERIOD_DAYS[m.group(2)])
    if m := re.search(r"\bevery\s*(\d+)\s*(hours?|hrs?|h)\b", s):
        return 24 / int(m.group(1))
    for term in sorted(FREQ_PER_DAY, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", s):
            v = FREQ_PER_DAY[term]
            return v if v >= 1 else _round_up(v)
    return None


def is_prn(text: str | None) -> bool:
    s = (text or "").lower()
    return any(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", s) for t in PRN_TERMS)


def duration_days(text: str | None) -> float | None:
    if not text:
        return None
    s = str(text).lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(d|day|days|w|wk|wks|week|weeks|m|mon|month|months)\b", s)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    if unit.startswith("w"):
        return n * 7
    if unit.startswith("m"):
        return n * 30
    return n


def to_ucum(unit: str | None) -> str | None:
    if unit is None:
        return None
    key = re.sub(r"\s+", " ", str(unit).strip().lower())
    if not key:
        return None
    return UCUM_ALIASES.get(key) or UCUM_ALIASES.get(key.replace(" ", ""))
