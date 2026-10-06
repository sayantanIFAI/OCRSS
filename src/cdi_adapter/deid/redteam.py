"""Red-team re-identification check (backlog E11-S4 acceptance; ARCHITECTURE §12).

An attacker who knows who is in a sample (name, ABHA, Aadhaar, phone, DOB, address, MRN, e-mail)
searches everything the de-identification produced (OCR text, payload JSON, and the redacted page
images, re-read with the same RapidOCR engine S3 uses) for any trace of them:

* numbers: the digits of an identifier, ignoring spaces/dashes (also its last 6+ digits alone)
* names: each name token of 3+ letters, case-insensitive, whole word, also with one typo
* dates: the date of birth in the common written forms
* e-mail / ABHA address: case-insensitive

A leak is any hit. The attempt must fail (no leak) on de-identified output, and must succeed on
the original (the control that shows the attack can find what it looks for).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Leak:
    identifier: str  # which truth field leaked (never its value)
    where: str  # "ocr_text" / "payload" / "page_image"
    how: str  # "digits" / "name" / "name~1" / "dob" / "text"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _within_one(a: str, b: str) -> bool:
    """Levenshtein distance <= 1."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return edits + (len(b) - j) <= 1


def _dob_forms(d: date) -> set[str]:
    return {d.strftime(f) for f in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y",
                                    "%d %b %Y", "%d %B %Y", "%d-%b-%Y")} | {d.strftime("%d %b %Y").lower()}  # fmt: skip  # noqa: E501


def attempt(truth: Mapping[str, Any], outputs: Mapping[str, Iterable[str]]) -> list[Leak]:
    """``truth``: field -> known value (``dob`` as a ``date``); ``outputs``: where -> texts."""
    leaks: list[Leak] = []
    for where, texts in outputs.items():
        blob = "\n".join(texts)
        low = blob.lower()
        # the digits of each number as written ("+91 98300 12345", "2341-2341-2346"): groups joined
        # by one space / dash / dot. Not every digit on the page run together, which would find a
        # phone's last six digits in "2027" followed by "3 days" and "650 mg"
        digits = "\n".join(_digits(m.group()) for m in re.finditer(r"\d(?:[ .\-]?\d)*", blob))
        words = re.findall(r"[a-z]{3,}", low)
        for field_name, value in truth.items():
            if value in (None, ""):
                continue
            if field_name == "dob" and isinstance(value, date):
                if any(f.lower() in low for f in _dob_forms(value)):
                    leaks.append(Leak(field_name, where, "dob"))
                continue
            text = str(value)
            d = _digits(text)
            if len(d) >= 6 and (d in digits or d[-6:] in digits):
                leaks.append(Leak(field_name, where, "digits"))
                continue
            if "@" in text or ("-" in text and len(d) < 6):
                if text.lower() in low:
                    leaks.append(Leak(field_name, where, "text"))
                continue
            if field_name in ("name", "father_name", "address"):
                # names also with one OCR-style typo; address words only exactly (with one typo,
                # "Hills" would match the symptom "chills")
                fuzzy = field_name != "address"
                for token in re.findall(r"[a-z]{3,}", text.lower()):
                    if token in words:
                        leaks.append(Leak(field_name, where, "name"))
                        break
                    if fuzzy and len(token) >= 5 and any(_within_one(token, w) for w in words):
                        leaks.append(Leak(field_name, where, "name~1"))
                        break
                continue
            if text.lower() in low:
                leaks.append(Leak(field_name, where, "text"))
    return leaks


def payload_strings(payload: Any) -> list[str]:
    return [json.dumps(payload, ensure_ascii=False)] if payload is not None else []
