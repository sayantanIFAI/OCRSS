"""S6 deterministic clinical-plausibility rules (ARCHITECTURE §5 S6). **No LLM.**

Each rule returns findings ``(severity in {blocker, warn, info}, code, message)``:

| rule                 | checks                                                                 |
|----------------------|------------------------------------------------------------------------|
| check_value_range    | lab/vital value inside an absolute physiological window per LOINC;     |
|                      | auto-normalises temperature F -> C; flags a unit mismatch              |
| check_unit_present   | a numeric lab/vital has a unit                                         |
| check_medication     | dose/strength present, frequency present, 0 < freq/day <= 6,           |
|                      | total daily dose <= 1.5 x adult ceiling (DOSE_MAX_MG_DAY per SNOMED)   |
| check_dates          | effective/onset/asserted not in the future, not < 1900, within +-400 d |
|                      | of the encounter                                                       |
| check_evidence       | a fact_provenance row with >= 1 OCR block id **or** extracted text     |
| check_terminology    | condition/medication/allergy/procedure is ``bound`` (else warn)        |
| check_field_grammars | a field reading is inside its clinical grammar (E15-S2): strength,     |
|                      | dose (``1-0-1``, never ``1-7-1``), duration, frequency                 |

The windows are *absolute plausibility* limits (values outside them are almost certainly
misreads), not clinical reference ranges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cdi_adapter import fieldgrammar


@dataclass(frozen=True)
class Finding:
    severity: str  # blocker | warn | info
    code: str
    message: str

    def as_json(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


# LOINC -> (low, high, expected UCUM unit)
LAB_RANGE: dict[str, tuple[float, float, str]] = {
    "4548-4": (3.0, 20.0, "%"),  # HbA1c
    "1558-6": (20.0, 1000.0, "mg/dL"),  # fasting glucose
    "1521-4": (20.0, 1000.0, "mg/dL"),
    "2345-7": (20.0, 1500.0, "mg/dL"),
    "2160-0": (0.1, 25.0, "mg/dL"),  # creatinine
    "3094-0": (1.0, 200.0, "mg/dL"),
    "3091-6": (2.0, 400.0, "mg/dL"),
    "2093-3": (40.0, 700.0, "mg/dL"),  # cholesterol
    "2089-1": (5.0, 500.0, "mg/dL"),
    "2085-9": (5.0, 200.0, "mg/dL"),
    "2571-8": (10.0, 5000.0, "mg/dL"),
    "718-7": (2.0, 25.0, "g/dL"),  # haemoglobin
    "2951-2": (100.0, 180.0, "mmol/L"),  # sodium
    "2823-3": (1.0, 10.0, "mmol/L"),  # potassium
    "2075-0": (60.0, 140.0, "mmol/L"),
    "1975-2": (0.05, 50.0, "mg/dL"),
    "1968-7": (0.0, 40.0, "mg/dL"),
    "1751-7": (0.5, 7.0, "g/dL"),
    "2885-2": (2.0, 15.0, "g/dL"),
    "3084-1": (0.5, 20.0, "mg/dL"),
    "17861-6": (3.0, 20.0, "mg/dL"),
    "4537-7": (0.0, 200.0, "mm/h"),
}
VITAL_RANGE: dict[str, tuple[float, float, str]] = {
    "8480-6": (50.0, 260.0, "mm[Hg]"),  # systolic
    "8462-4": (25.0, 160.0, "mm[Hg]"),  # diastolic
    "8867-4": (20.0, 250.0, "/min"),  # heart rate
    "9279-1": (4.0, 70.0, "/min"),  # respiratory rate
    "8310-5": (30.0, 45.0, "Cel"),  # body temperature
    "59408-5": (40.0, 100.0, "%"),  # SpO2
    "29463-7": (0.3, 350.0, "kg"),  # body weight
    "8302-2": (30.0, 250.0, "cm"),  # body height
    "39156-5": (8.0, 90.0, "kg/m2"),  # BMI
    "2339-0": (20.0, 1500.0, "mg/dL"),  # capillary glucose
}

# SNOMED substance -> adult maximum total daily dose in mg
DOSE_MAX_MG_DAY: dict[str, float] = {
    "372567009": 2550,  # metformin
    "386864001": 10,  # amlodipine
    "373444002": 80,  # atorvastatin
    "387458008": 4000,  # aspirin
    "386952008": 600,  # clopidogrel (loading)
    "372826007": 400,  # metoprolol
    "373567002": 150,  # losartan
    "387069000": 80,  # telmisartan
    "386872004": 10,  # ramipril
    "386966003": 8,  # glimepiride
    "387517004": 4000,  # paracetamol
    "395821003": 80,  # pantoprazole
    "387137007": 80,  # omeprazole
    "372687004": 3000,  # amoxicillin
    "387531004": 2000,  # azithromycin
    "372897005": 32,  # salbutamol (oral)
    "387475002": 600,  # furosemide
    "116601002": 80,  # prednisolone
    "372523007": 20,  # cetirizine
    "372487007": 32,  # ondansetron
}
DOSE_CEILING_FACTOR = 1.5
MAX_FREQ_PER_DAY = 6
DATE_WINDOW_DAYS = 400
_MG = {"mg": 1.0, "mcg": 0.001, "ug": 0.001, "µg": 0.001, "g": 1000.0, "gm": 1000.0}
_CODED_TYPES = ("condition", "medication", "allergy", "procedure")


BP_PANEL = "85354-9"
_BP_TEXT = re.compile(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*$")


def _check_bp(fact: dict[str, Any]) -> list[Finding]:
    """One BP fact "138/86": each half against its component window (8480-6 / 8462-4)."""
    m = _BP_TEXT.match(fact.get("value_text") or "")
    if not m:
        return [Finding("blocker", "range-implausible", "blood pressure is not systolic/diastolic")]
    out = []
    for value, loinc, label in ((m.group(1), "8480-6", "systolic"),
                                (m.group(2), "8462-4", "diastolic")):  # fmt: skip
        low, high, unit = VITAL_RANGE[loinc]
        if not low <= float(value) <= high:
            msg = f"{label} {value} outside plausible window {low}-{high} {unit}"
            out.append(Finding("blocker", "range-implausible", msg))
    return out


def _f_to_c(v: float) -> float:
    return round((v - 32) * 5 / 9, 1)


def check_value_range(fact: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    """-> (findings, column updates). Updates carry the °F -> °C normalisation."""
    if fact["fact_type"] == "vital_sign" and fact.get("code") == BP_PANEL:
        return _check_bp(fact), {}
    table = LAB_RANGE if fact["fact_type"] == "lab_result" else VITAL_RANGE
    if fact["fact_type"] not in ("lab_result", "vital_sign") or fact.get("code") not in table:
        return [], {}
    v = fact.get("value_num")
    if v is None:
        return [], {}
    v = float(v)
    low, high, unit = table[fact["code"]]
    got = fact.get("value_unit_ucum")
    updates: dict[str, Any] = {}
    if fact["code"] == "8310-5" and (got == "[degF]" or (got is None and 86 <= v <= 113)):
        v = _f_to_c(v)
        updates = {"value_num": v, "value_unit_ucum": "Cel"}
        got = "Cel"
    out: list[Finding] = []
    if got and got != unit:
        out.append(Finding("warn", "unit-mismatch", f"unit {got!r}, expected {unit!r}"))
        return out, updates  # a value in another unit cannot be range-checked here
    if not low <= v <= high:
        out.append(Finding("blocker", "range-implausible",
                           f"{v} outside plausible window {low}-{high} {unit}"))  # fmt: skip
    return out, updates


def check_unit_present(fact: dict[str, Any]) -> list[Finding]:
    if fact["fact_type"] in ("lab_result", "vital_sign") and fact.get("value_num") is not None:
        if not fact.get("value_unit_ucum"):
            return [Finding("warn", "unit-missing", "numeric value has no unit")]
    return []


def check_medication(fact: dict[str, Any]) -> list[Finding]:
    if fact["fact_type"] != "medication":
        return []
    out: list[Finding] = []
    dose, dose_unit = fact.get("dose_num"), fact.get("dose_unit_ucum")
    strength, s_unit = fact.get("strength_num"), fact.get("strength_unit")
    if dose is None and strength is None:
        out.append(Finding("warn", "med-no-dose", "no dose or strength"))
    fpd = fact.get("frequency_per_day")
    if fpd is None:
        if not fact.get("prn"):
            out.append(Finding("warn", "med-no-frequency", "no frequency"))
    elif not 0 < float(fpd) <= MAX_FREQ_PER_DAY:
        out.append(Finding("blocker", "med-frequency-implausible",
                           f"{fpd}/day outside (0, {MAX_FREQ_PER_DAY}]"))  # fmt: skip
    ceiling = DOSE_MAX_MG_DAY.get(fact.get("code") or "")
    amount, unit = (dose, dose_unit) if dose is not None else (strength, s_unit)
    factor = _MG.get(str(unit or "").lower())
    if ceiling and amount is not None and factor and fpd:
        daily = float(amount) * factor * float(fpd)
        if daily > DOSE_CEILING_FACTOR * ceiling:
            out.append(Finding("blocker", "med-dose-ceiling",
                               f"{daily:g} mg/day exceeds {DOSE_CEILING_FACTOR}x adult ceiling "
                               f"{ceiling:g} mg/day"))  # fmt: skip
    return out


def _as_date(x: Any) -> date | None:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return None


def check_dates(fact: dict[str, Any], encounter_date: date | None,
                today: date | None = None) -> list[Finding]:  # fmt: skip
    today = today or datetime.now(UTC).date()
    out: list[Finding] = []
    for col in ("effective_time", "onset", "asserted_time"):
        d = _as_date(fact.get(col))
        if d is None:
            continue
        if d > today + timedelta(days=1):
            out.append(Finding("blocker", "date-future", f"{col} {d} is in the future"))
        elif d.year < 1900:
            out.append(Finding("blocker", "date-implausible", f"{col} {d} is before 1900"))
        elif (col != "asserted_time" and encounter_date
              and abs((d - encounter_date).days) > DATE_WINDOW_DAYS):  # fmt: skip
            msg = f"{col} {d} is > {DATE_WINDOW_DAYS} d from the encounter"
            out.append(Finding("warn", "date-far-from-encounter", msg))
    return out


def check_evidence(provenance: list[dict[str, Any]]) -> list[Finding]:
    for p in provenance:
        if p.get("ocr_block_ids") or (p.get("extracted_text") or "").strip():
            return []
    return [Finding("blocker", "evidence-missing", "no OCR block or extracted text supports it")]


def check_terminology(fact: dict[str, Any]) -> list[Finding]:
    if fact["fact_type"] in _CODED_TYPES and fact.get("code_status") != "bound":
        return [Finding("warn", "unmapped-terminology", "no standard code bound")]
    return []


def check_field_grammars(fact: dict[str, Any],
                         readings: dict[str, str] | None = None) -> list[Finding]:  # fmt: skip
    """E15-S2: a reading an unconstrained engine produced outside its field's grammar.

    The grammar-locked decoder (``mlserve.grammar``) cannot emit one; RapidOCR, TrOCR and an
    unconstrained VLM can, so the same grammars judge what arrives. ``readings`` are the engine's
    own strings for this medication (``fieldgrammar.readings_by_drug``); the frequency is judged
    from the stored reading as well, since that is kept verbatim. A field that is absent is not
    judged here - that is ``check_medication``'s ``med-no-dose`` / ``med-no-frequency``.
    """
    if fact["fact_type"] != "medication":
        return []
    seen = dict(readings or {})
    if fact.get("frequency_code"):
        seen.setdefault("frequency", str(fact["frequency_code"]))
    out: list[Finding] = []
    for domain in fieldgrammar.DOMAINS:
        why = fieldgrammar.out_of_grammar(domain, seen.get(domain))
        if why:
            out.append(Finding(fieldgrammar.severity(domain, seen.get(domain)),
                               "field-out-of-grammar", why))  # fmt: skip
    return out
