"""Indian direct identifiers as Presidio recognizers (backlog E11-S4; ARCHITECTURE §12).

Presidio's defaults are US-centric. These cover what an Indian clinic document carries: ABHA
number + address, Aadhaar (Verhoeff-checked, so an arbitrary 12-digit number is not flagged),
PAN, PIN code (only next to "PIN"), hospital record numbers (the *value* after MRN / UHID / IP No
/ Reg No ...), CareFlow MPI ids and dates in the forms Indian documents use. Phone numbers use
Presidio's libphonenumber recognizer with region IN; e-mail is Presidio's own.
"""

from __future__ import annotations

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.predefined_recognizers import EmailRecognizer, PhoneRecognizer

# --------------------------------------------------------------- Verhoeff (Aadhaar check digit)
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
      [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
      [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
      [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]  # fmt: skip
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
      [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
      [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]  # fmt: skip


def verhoeff_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][d]]
    return c == 0 and len(digits) > 1


class AadhaarRecognizer(PatternRecognizer):
    """12 digits, first digit 2-9, valid Verhoeff check digit (UIDAI's scheme)."""

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_AADHAAR",
            patterns=[Pattern("aadhaar", r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)", 0.5)],
            context=["aadhaar", "aadhar", "uid", "uidai"],
        )

    def validate_result(self, pattern_text: str) -> bool | None:
        return verhoeff_ok(pattern_text)


_ID_LABELS = (
    r"MRN|UHID|IP\s*No|OP\s*No|IPD\s*No|OPD\s*No|Reg(?:istration)?\s*No|"
    r"Patient\s*ID|Hosp(?:ital)?\s*No|Case\s*No|CR\s*No|Bed\s*No|Lab\s*No|Sample\s*ID"
)


def india_recognizers() -> list[EntityRecognizer]:
    return [
        PatternRecognizer(
            supported_entity="IN_ABHA_NUMBER",
            patterns=[Pattern("abha", r"(?<!\d)\d{2}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}(?!\d)", 0.6)],
            context=["abha", "health id", "healthid", "hid", "abdm", "ndhm"],
        ),
        PatternRecognizer(
            supported_entity="IN_ABHA_ADDRESS",
            patterns=[Pattern("abha_address", r"(?i)\b[a-z0-9._-]{3,}@(?:abdm|sbx|ndhm)\b", 0.9)],
        ),
        AadhaarRecognizer(),
        PatternRecognizer(
            supported_entity="IN_PAN",
            patterns=[Pattern("pan", r"\b[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]\b", 0.7)],
            context=["pan", "income tax"],
        ),
        PatternRecognizer(  # a 6-digit number is a PIN code only when labelled as one
            supported_entity="IN_PIN_CODE",
            # no word boundary before "PIN": OCR glues it to the city ("LucknowPIN: 465007")
            patterns=[
                Pattern("pin", r"(?i)(?<=pin(?:\s*code)?\s*[:.-]?\s*)[1-9]\d{2}\s?\d{3}(?!\d)", 0.9)
            ],  # fmt: skip
        ),
        PatternRecognizer(  # everything after an "Address:" label, up to PIN / phone / line end
            supported_entity="LOCATION",
            name="address_value",
            patterns=[
                Pattern(
                    "address_value",
                    r"(?i)(?<=\baddress\s*[:.-]\s*)[^\s][^\n]*?"
                    r"(?=\s*(?:pin(?:\s*code)?\b|ph(?:one)?\b|mob(?:ile)?\b|\n|$))",
                    0.9,
                )
            ],  # fmt: skip
        ),
        PatternRecognizer(  # the value after a record-number label, never the label itself
            supported_entity="MEDICAL_RECORD_NUMBER",
            patterns=[
                Pattern(
                    "mrn",
                    rf"(?i)(?<=\b(?:{_ID_LABELS})\.?\s*[:#.-]?\s*)"
                    r"[A-Z0-9][A-Z0-9/-]{2,24}\b",
                    0.85,
                )
            ],  # fmt: skip
        ),
        PatternRecognizer(
            supported_entity="MEDICAL_RECORD_NUMBER",
            name="careflow_mpi",
            patterns=[
                Pattern("cfp_mpi", r"\bCFP-\d{4}-\d{6}\b", 0.95),
                Pattern("legacy_mrn", r"\bLEG-\d{1,10}\b", 0.9),
            ],  # fmt: skip
        ),
        PatternRecognizer(
            supported_entity="DATE_TIME",
            name="india_dates",
            patterns=[
                Pattern(
                    "dmy_numeric",
                    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[/.-](?:0?[1-9]|1[0-2])"
                    r"[/.-](?:19|20)?\d{2}(?!\d)",
                    0.6,
                ),
                Pattern(
                    "dmy_text",
                    r"(?i)\b(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[ -]?"
                    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
                    r"[a-z]*[ ,-]*(?:19|20)\d{2}(?!\d)",
                    0.6,
                ),
                Pattern(
                    "iso",
                    r"(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
                    r"(?!\d)",
                    0.6,
                ),
            ],  # fmt: skip
        ),
        PhoneRecognizer(supported_regions=("IN",), supported_language="en"),
        PatternRecognizer(  # Indian mobiles as OCR really prints them, glued to the next word
            supported_entity="PHONE_NUMBER",
            name="india_mobile",
            patterns=[
                Pattern("in_mobile", r"(?<!\d)(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)", 0.7)
            ],  # fmt: skip
            context=["mobile", "phone", "contact", "tel", "mob", "ph"],
        ),
        EmailRecognizer(),
    ]


def as_result(entity: str, start: int, end: int, score: float) -> RecognizerResult:
    return RecognizerResult(entity_type=entity, start=start, end=end, score=score)
