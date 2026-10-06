"""Partial-date parsing shared by identity (S4 patient block) and encounter dating (§5 S4).

``parse_date`` handles ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, ``DD-Mon-YYYY``, ``DD/MM/YYYY``
(also ``-``/``.`` separators and 2-digit years) and returns the date with its precision
(``year`` | ``month`` | ``day``), matching ``encounter.date_precision``. Indian documents write
day-first, so ``03/04/2026`` is 3 April.
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}  # fmt: skip


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 2:  # 2-digit year: pivot at the current year
        n += 2000 if n <= date.today().year % 100 else 1900
    return n


def parse_date(text: str | None) -> tuple[date, str] | None:
    if not text:
        return None
    s = str(text).strip().lower().rstrip(".")
    try:
        if m := re.fullmatch(r"(\d{4})", s):
            return date(int(m[1]), 1, 1), "year"
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s):
            return date(int(m[1]), int(m[2]), 1), "month"
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[t ].*)?", s):
            return date(int(m[1]), int(m[2]), int(m[3])), "day"
        if m := re.fullmatch(r"(\d{1,2})[-/. ]([a-z]{3})[a-z]*[-/., ]+(\d{2}|\d{4})", s):
            return date(_year(m[3]), _MONTHS[m[2]], int(m[1])), "day"
        if m := re.fullmatch(r"([a-z]{3})[a-z]*[-/., ]+(\d{4})", s):
            return date(int(m[2]), _MONTHS[m[1]], 1), "month"
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s):
            return date(_year(m[3]), int(m[2]), int(m[1])), "day"
    except (ValueError, KeyError):
        return None
    return None
