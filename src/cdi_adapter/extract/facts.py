"""Payload -> fact drafts (ARCHITECTURE §5 S4 "Payload -> facts"). Pure functions, no I/O.

Per-``doc_type`` handlers (``_facts_prescription``, ``_facts_lab``, ``_facts_vitals``,
``_facts_discharge``, ``_facts_radiology``, ``_facts_opd_note``) built on tolerant readers:

* ``_qty(x)`` accepts a ``{value, unit, evidence}`` object, a bare number, or a string such as
  ``"500 mg"`` / ``"138/86"``.
* ``_coded_text(x)`` accepts ``{text|code|display|name}`` or a plain string.
* Vitals are de-duplicated within a document and only emitted when a numeric value was actually
  read; ``_VITAL_ALIAS`` normalises pulse -> heart_rate etc. Blood pressure is one fact
  ("138/86"); its LOINC 8480-6 / 8462-4 codes are the FHIR components.
* Medication detail: strength parsed out of the drug name (``METPURE XL 50 MG`` -> ``Metpure XL``
  / 50 / mg, preferring a dose unit over a pen's fill volume); frequency from the dose-slot column
  (``1-0-1`` -> 2/day, ``BD`` -> 2/day, ``TWICE IN A YEAR`` -> 0.006/day) with ``x 15 days`` read
  as a *duration*; SC route inferred for pens / insulin injections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cdi_adapter.catalogue.normalize import DOSAGE_FORMS, LEADING_FORMS
from cdi_adapter.terminology.parse import duration_days, freq_per_day, is_prn, to_ucum

NUM = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([^\d\s].*)?$")
_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*$")
_BP = re.compile(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})")

_VITAL_ALIAS = {
    "pulse": "heart_rate", "pr": "heart_rate", "heart rate": "heart_rate", "hr": "heart_rate",
    "heart_rate": "heart_rate", "pulse rate": "heart_rate",
    "bp": "blood_pressure", "blood pressure": "blood_pressure", "blood_pressure": "blood_pressure",
    "temp": "temperature", "temperature": "temperature",
    "spo2": "spo2", "sp02": "spo2", "oxygen saturation": "spo2", "o2 sat": "spo2",
    "rr": "respiratory_rate", "respiratory rate": "respiratory_rate",
    "resp rate": "respiratory_rate",
    "respiratory_rate": "respiratory_rate",
    "wt": "body_weight", "weight": "body_weight", "body weight": "body_weight",
    "body_weight": "body_weight",
    "ht": "body_height", "height": "body_height", "body_height": "body_height",
    "bmi": "bmi", "body mass index": "bmi",
    "rbs": "blood_glucose", "grbs": "blood_glucose", "blood sugar": "blood_glucose",
}  # fmt: skip
_VITAL_TEXT = {
    "heart_rate": "heart rate", "temperature": "body temperature", "spo2": "oxygen saturation",
    "respiratory_rate": "respiratory rate", "body_weight": "body weight",
    "body_height": "body height", "bmi": "body mass index", "blood_glucose": "blood glucose",
}  # fmt: skip

# dosage-form words: the one shared table (catalogue/normalize.py), catalogue form names;
# "T" / "C" / "Cr" are a form only as the first word of a line ("T Dytor", not "Vitamin C")
_FORMS = DOSAGE_FORMS
_LEAD_FORMS = {**DOSAGE_FORMS, **LEADING_FORMS}
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|gm|iu|units?|ml)(?:\s*/\s*ml)?\b", re.I)
# where the drug name ends and the schedule begins
_SCHEDULE = re.compile(
    r"\s+(?:\d(?:\.\d)?-\d(?:\.\d)?-\d(?:\.\d)?|od|bd|bid|tds|tid|qid|hs|sos|stat|x\s*\d|"
    r"for\s+\d|after\b|before\b|once\b|twice\b|thrice\b|daily\b)",
    re.I,
)
_DURATION_IN_TEXT = re.compile(r"(?:x|for)\s*\d+\s*\w+", re.I)
# when to take it, not how it enters the body: a model that writes "after food" / "empty stomach"
# as the route has put an instruction in the wrong field (2026-09-25 check); it moves to
# instructions unless the text also names a real route
_WHEN_TO_TAKE = re.compile(r"\b(food|meals?|breakfast|lunch|dinner|supper|stomach|bed ?time|"
                           r"night|morning|evening|milk|water)\b", re.I)  # fmt: skip
_A_ROUTE = re.compile(r"\b(oral|orally|p\.?o\.?|i\.?v\.?|i\.?m\.?|s\.?c\.?|subcut\w*|topical|"
                      r"local|inhal\w*|nebul\w*|nasal|sublingual|rectal|vaginal|transdermal|"
                      r"eye|ear)\b", re.I)  # fmt: skip
_DOSE_UNITS = ("mg", "mcg", "µg", "g", "gm", "iu", "unit", "units")


@dataclass
class MedDraft:
    drug_text: str
    form: str | None = None
    strength_num: float | None = None
    strength_unit: str | None = None
    dose_num: float | None = None
    dose_unit_ucum: str | None = None
    route: str | None = None
    frequency_code: str | None = None
    frequency_per_day: float | None = None
    duration_days: float | None = None
    prn: bool = False
    instructions: str | None = None
    intent: str = "order"


@dataclass
class FactDraft:
    fact_type: str
    local_text: str
    confidence_extract: float
    evidence: list[str] = field(default_factory=list)
    value_kind: str | None = None
    value_num: float | None = None
    value_unit_ucum: str | None = None
    value_text: str | None = None
    value_low_num: float | None = None
    value_high_num: float | None = None
    ref_range_low: float | None = None
    ref_range_high: float | None = None
    ref_range_text: str | None = None
    abnormal_flag: str | None = None
    clinical_status: str | None = None
    medication: MedDraft | None = None


# ------------------------------------------------------------------ tolerant readers


def _num(s: Any) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _qty(x: Any) -> tuple[float | None, str | None, str | None]:
    """-> (value_num, unit, value_text). Non-numeric strings come back as text."""
    unit = None
    if isinstance(x, dict):
        unit = x.get("unit")
        x = x.get("value")
    if x is None or isinstance(x, bool):
        return None, unit, None
    if isinstance(x, int | float):
        return float(x), unit, None
    s = str(x).strip()
    if not s:
        return None, unit, None
    if _BP.match(s):
        return None, unit, s
    if m := NUM.match(s):
        tail = (m.group(2) or "").strip() or None
        return float(m.group(1)), unit or tail, None
    return None, unit, s


def _coded_text(x: Any) -> str | None:
    if isinstance(x, dict):
        for k in ("text", "display", "name", "code"):
            if isinstance(x.get(k), str) and x[k].strip():
                return x[k].strip()
        return None
    if isinstance(x, str) and x.strip():
        return x.strip()
    return None


def _ev(x: Any) -> list[str]:
    ev = x.get("evidence") if isinstance(x, dict) else None
    return [str(e) for e in ev] if isinstance(ev, list) else []


def _conf(x: Any, default: float) -> float:
    c = x.get("confidence") if isinstance(x, dict) else None
    return float(c) if isinstance(c, int | float) and not isinstance(c, bool) else default


def _items(payload: dict[str, Any], key: str) -> list[Any]:
    v = payload.get(key)
    return v if isinstance(v, list) else ([] if v is None else [v])


# ------------------------------------------------------------------ builders


_JOINED = re.compile(r"\s*(?:[,;+/]|\band\b)\s*", re.I)


def split_joined(fact_type: str, t: str) -> list[str]:
    """One written line naming several diagnoses ("Type 2 diabetes mellitus, Hypertension") ->
    one text per diagnosis, but only when the whole line codes to nothing and EVERY part codes
    on its own (so "Diabetes mellitus, type 2" or "Fracture, left radius" stay one text). One
    uncoded Condition for two diseases was what the download carried (2026-09-25)."""
    from cdi_adapter.terminology.service import bind_text  # the seed lookup only, no I/O

    parts = [p for p in _JOINED.split(t) if p]
    if len(parts) < 2 or bind_text(fact_type, t) is not None:
        return [t]
    return parts if all(bind_text(fact_type, p) for p in parts) else [t]


def _coded_facts(payload, key: str, fact_type: str, default: float) -> list[FactDraft]:
    out = []
    for item in _items(payload, key):
        t = _coded_text(item)
        for part in split_joined(fact_type, t) if t and fact_type == "condition" else [t]:
            if part:
                out.append(FactDraft(fact_type, part, _conf(item, default), _ev(item),
                                     value_kind=None))  # fmt: skip
    return out


_ORDER_SPLIT = re.compile(r"\s*(?:[,;+]|\band\b)\s*", re.I)


def split_orders(text: str | None) -> list[str]:
    """One advised test per item: "CBC, LFT, RFT" / "T3 + T4 + TSH" -> each test. A slash is
    part of a test's name ("Urine R/M", "Urine C/S"), so it never splits."""
    return [t.strip(" .:-") for t in _ORDER_SPLIT.split(text or "") if t.strip(" .:-")]


def _order_facts(payload, default: float) -> list[FactDraft]:
    """The tests a prescription advises (owner request 2026-09-28): ``lab_order`` facts, as
    written, one per test, with the evidence of the line they were read from."""
    out = []
    for item in _items(payload, "investigations_advised"):
        for part in split_orders(_coded_text(item)):
            out.append(FactDraft("lab_order", part, _conf(item, default), _ev(item),
                                 value_kind=None))  # fmt: skip
    return out


def _vital_facts(payload, default: float, seen: set[str] | None = None) -> list[FactDraft]:
    seen = set() if seen is None else seen
    out: list[FactDraft] = []
    for item in _items(payload, "vitals"):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower().replace("_", " ")
        kind = _VITAL_ALIAS.get(name) or _VITAL_ALIAS.get(name.replace(" ", "_"))
        if kind is None:
            continue
        num, unit, txt = _qty(item.get("value"))
        unit = unit or item.get("unit")
        ev, conf = _ev(item) or _ev(item.get("value")), _conf(item, default)
        if kind == "blood_pressure":
            # one BP fact ("138/86"); FHIR carries systolic / diastolic as its two components
            m = _BP.match(txt or "")
            if not m or kind in seen:
                continue
            seen.add(kind)
            out.append(FactDraft("vital_sign", "blood pressure", conf, ev, value_kind="text",
                                 value_text=f"{m.group(1)}/{m.group(2)}",
                                 value_unit_ucum="mm[Hg]"))  # fmt: skip
            continue
        if num is None or kind in seen:  # only emitted when a number was actually read
            continue
        seen.add(kind)
        out.append(FactDraft("vital_sign", _VITAL_TEXT.get(kind, kind), conf, ev,
                             value_kind="quantity", value_num=num,
                             value_unit_ucum=to_ucum(unit) or unit))  # fmt: skip
    return out


def _lab_facts(payload, key: str, default: float) -> list[FactDraft]:
    out = []
    for item in _items(payload, key):
        if not isinstance(item, dict) or not _coded_text(item.get("name")):
            continue
        num, unit, txt = _qty(item.get("value"))
        unit = unit or item.get("unit")
        f = FactDraft("lab_result", _coded_text(item.get("name")) or "", _conf(item, default),
                      _ev(item) or _ev(item.get("value")))  # fmt: skip
        if num is not None:
            f.value_kind, f.value_num, f.value_unit_ucum = "quantity", num, to_ucum(unit) or unit
        elif txt:
            f.value_kind, f.value_text = "text", txt
        else:
            continue
        rr = item.get("ref_range")
        if isinstance(rr, str) and rr.strip():
            if m := _RANGE.match(rr):
                f.ref_range_low, f.ref_range_high = float(m.group(1)), float(m.group(2))
            f.ref_range_text = rr.strip()
        flag = str(item.get("flag") or "").strip().upper()[:1]
        if flag in ("H", "L"):
            f.abnormal_flag = flag
        out.append(f)
    return out


def _allergy_facts(payload, default: float) -> list[FactDraft]:
    out = []
    for item in _items(payload, "allergies"):
        sub = item.get("substance") if isinstance(item, dict) else item
        if isinstance(sub, str) and sub.strip():
            f = FactDraft("allergy", sub.strip(), _conf(item, default), _ev(item))
            if isinstance(item, dict) and item.get("reaction"):
                f.value_kind, f.value_text = "text", str(item["reaction"])
            out.append(f)
    return out


def _procedure_facts(payload, key: str, default: float) -> list[FactDraft]:
    out = []
    for item in _items(payload, key):
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            f = FactDraft("procedure", name.strip(), _conf(item, default), _ev(item))
            if isinstance(item, dict) and item.get("findings"):
                f.value_kind, f.value_text = "text", str(item["findings"])
            out.append(f)
    return out


def reslot_name(item: dict[str, Any]) -> dict[str, Any]:
    """The 7B sometimes writes only the dosage form as ``drug_name`` and the medicine into
    ``strength`` ("CAP" + "LOPACA CV 200 MG", owner's cardiology OPD sheet, 2026-09-28): the name
    is put back together, so the catalogue and the medication agent get a medicine to look up.
    Nothing is invented: the two written values are joined and the strength parsed from them."""
    name = str(item.get("drug_name") or "").strip()
    strength = str(item.get("strength") or "").strip()
    if name.lower().rstrip(".") in _LEAD_FORMS and re.search(
        r"[A-Za-z]{3,}", _STRENGTH.sub("", strength)
    ):
        return {**item, "drug_name": f"{name} {strength}", "strength": ""}
    return item


def parse_medication(item: dict[str, Any]) -> MedDraft:
    raw_name = str(item.get("drug_name") or "").strip()
    tokens = raw_name.split()
    form = None
    if tokens and tokens[0].lower().rstrip(".") in _LEAD_FORMS:
        form = _LEAD_FORMS[tokens[0].lower().rstrip(".")]
        tokens = tokens[1:]
    name = _SCHEDULE.split(" " + " ".join(tokens), maxsplit=1)[0].strip()
    # a form word later in the name ("... PEN") describes the device, not the drug
    kept: list[str] = []
    for w in name.split():
        if w.lower().rstrip(".") in _FORMS and kept:
            form = form or _FORMS[w.lower().rstrip(".")]
            continue
        kept.append(w)
    name = " ".join(kept)

    s_num, s_unit, _ = _qty(item.get("strength"))
    found = [(float(n), u.lower()) for n, u in _STRENGTH.findall(name)]
    if s_num is None and found:
        dose_like = [f for f in found if f[1] in _DOSE_UNITS]
        s_num, s_unit = (dose_like or found)[0]  # a dose unit beats a pen's fill volume (ml)
    name = _STRENGTH.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" -,/")
    drug = " ".join(w if (w.isupper() and len(w) <= 3) or not w.isalpha() else w.title()
                    for w in name.split())  # fmt: skip

    text_all = " ".join(str(item.get(k) or "") for k in ("frequency", "instructions", "duration"))
    text_all = f"{text_all} {raw_name}"
    freq_text = item.get("frequency") or None
    fpd = freq_per_day(freq_text) if freq_text else freq_per_day(text_all)
    route = item.get("route")
    instructions = item.get("instructions") or None
    if isinstance(route, str) and _WHEN_TO_TAKE.search(route) and not _A_ROUTE.search(route):
        instructions = "; ".join(x for x in (instructions, route.strip()) if x)
        route = None
    if not route and (form in ("injection", "pen") or re.search(r"\b(pen|insulin)\b", raw_name,
                                                                re.I)):  # fmt: skip
        route = (
            "SC"
            if re.search(r"\b(pen|insulin|s\.?c\.?)\b", raw_name + text_all, re.I)
            else ("injection")
        )
    d_num, d_unit, _ = _qty(item.get("dose"))
    return MedDraft(
        drug_text=drug or raw_name,
        form=form,
        strength_num=s_num,
        strength_unit=(s_unit or None) and str(s_unit).lower(),
        dose_num=d_num,
        dose_unit_ucum=to_ucum(d_unit) or d_unit,
        route=route,
        frequency_code=str(freq_text) if freq_text else None,
        frequency_per_day=fpd,
        duration_days=duration_days(item.get("duration")) or (
            duration_days(dm.group(0)) if (dm := _DURATION_IN_TEXT.search(text_all)) else None
        ),
        prn=is_prn(text_all),
        instructions=instructions,
    )  # fmt: skip


def _med_facts(payload, default: float) -> list[FactDraft]:
    out = []
    for item in _items(payload, "medications"):
        if isinstance(item, dict) and str(item.get("drug_name") or "").strip():
            item = reslot_name(item)
            med = parse_medication(item)
            out.append(FactDraft("medication", str(item["drug_name"]).strip(),
                                 _conf(item, default), _ev(item), medication=med))  # fmt: skip
    return out


# ------------------------------------------------------------------ per doc type


def _facts_prescription(p, d):
    return (_coded_facts(p, "diagnoses", "condition", d) + _med_facts(p, d)
            + _vital_facts(p, d) + _allergy_facts(p, d)
            + _coded_facts(p, "advice", "advice", d) + _order_facts(p, d))  # fmt: skip


def _facts_lab(p, d):
    return _lab_facts(p, "tests", d)


def _facts_vitals(p, d):
    return _vital_facts(p, d) + _allergy_facts(p, d)


def _facts_opd_note(p, d):
    out = (_coded_facts(p, "chief_complaints", "symptom", d)
           + _coded_facts(p, "symptoms", "symptom", d) + _coded_facts(p, "findings", "finding", d)
           + _coded_facts(p, "diagnoses", "condition", d) + _vital_facts(p, d) + _med_facts(p, d)
           + _allergy_facts(p, d) + _coded_facts(p, "advice", "advice", d)
           + _order_facts(p, d))  # fmt: skip
    if isinstance(p.get("referral_to"), str) and p["referral_to"].strip():
        out.append(FactDraft("referral", p["referral_to"].strip(), d))
    return out


def _facts_discharge(p, d):
    return (_coded_facts(p, "diagnoses", "condition", d) + _procedure_facts(p, "procedures", d)
            + _lab_facts(p, "investigations", d) + _vital_facts(p, d) + _med_facts(p, d)
            + _allergy_facts(p, d) + _coded_facts(p, "advice", "advice", d))  # fmt: skip


def _facts_radiology(p, d):
    out = _coded_facts(p, "findings_list", "finding", d)
    out += _coded_facts(p, "impression_concepts", "condition", d)
    out += _coded_facts(p, "diagnoses", "condition", d)
    study = p.get("study") or p.get("modality")
    if isinstance(study, str) and study.strip():
        texts = (_coded_text(i) for i in _items(p, "impression_concepts"))
        impression = "; ".join(t for t in texts if t)
        out.append(FactDraft("diagnostic_report", study.strip(), d, value_kind="text" if impression
                             else None, value_text=impression or None))  # fmt: skip
    if p.get("procedure"):
        out += _procedure_facts({"p": [p["procedure"]]}, "p", d)
    out += _coded_facts(p, "advice", "advice", d)
    return out


HANDLERS = {
    "prescription": _facts_prescription,
    "lab_report": _facts_lab,
    "vitals_sheet": _facts_vitals,
    "opd_note": _facts_opd_note,
    "referral": _facts_opd_note,
    "discharge_summary": _facts_discharge,
    "operative_note": _facts_discharge,
    "radiology": _facts_radiology,
}

ENCOUNTER_DATE_KEYS = ("encounter_date", "reported_at", "study_date", "discharge_date",
                       "admission_date", "recorded_at")  # fmt: skip


def facts_from_payload(doc_type: str, payload: dict[str, Any]) -> list[FactDraft]:
    default = _conf(payload, 0.5)
    return HANDLERS[doc_type](payload, default)
