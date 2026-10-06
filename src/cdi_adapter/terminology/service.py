"""S5 terminology binding (ARCHITECTURE §5 S5).

``bind_fact(fact)``: normalise ``local_text`` (lowercase, fix ``hba1c`` OCR variants, strip
punctuation) -> exact hit -> alias -> ``difflib.get_close_matches(cutoff=0.86)``. Medications:
strip ``tab/cap/syrup/...`` then match any token (brand -> generic). Sets
``code_system/code/code_display``, ``code_status = bound | local_only``,
``confidence_terminology = 0.9 | 0.4``; units -> UCUM; ``frequency_per_day`` parsed.
Every fact ends ``bound`` or ``local_only`` — never silently unmapped, never force-coded.
``bind_document(document_id)`` updates every fact from that document; ``status='normalized'``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import text

from cdi_adapter import metrics as prom
from cdi_adapter import repo
from cdi_adapter.catalogue.normalize import DOSAGE_FORMS, LEADING_FORMS
from cdi_adapter.config import get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.logging import get_logger
from cdi_adapter.terminology import seed
from cdi_adapter.terminology.parse import freq_per_day, to_ucum

log = get_logger(__name__)

FUZZY_CUTOFF = 0.86
CONF_BOUND = 0.9
CONF_LOCAL = 0.4
# every dosage-form word or abbreviation (the one shared table) and the release forms
_FORM_WORDS = set(DOSAGE_FORMS) | {"xl", "sr", "er", "cr", "od", "mr"}
_FACT_UPDATE_COLS = (
    "code_system",
    "code",
    "code_display",
    "code_status",
    "confidence_terminology",
    "value_unit_ucum",
)
_HBA1C = re.compile(r"\bhb\s*[-.]?\s*a\s*[1il]\s*c\b")


def normalize(s: str | None) -> str:
    t = (s or "").lower()
    t = _HBA1C.sub("hba1c", t)
    t = re.sub(r"[^a-z0-9%/ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_ORDER_PREFIX = re.compile(r"^(?:s|sr|serum|blood|b|test|tests|for|do|get)\s+")
_ORDER_SUFFIX = re.compile(r"\s+(?:test|tests|level|levels|report)$")


def order_key(key: str) -> str:
    """An advised test as ordered, without the words around it: "s creatinine" -> "creatinine",
    "vitamin d level" -> "vitamin d" (a normalised key, see ``normalize``)."""
    prev = None
    while prev != key:
        prev, key = key, _ORDER_SUFFIX.sub("", _ORDER_PREFIX.sub("", key)).strip()
    return key


@dataclass(frozen=True)
class Binding:
    concept: seed.Concept
    how: str  # exact | alias | fuzzy | token


@lru_cache
def _index(fact_type: str) -> tuple[dict[str, seed.Concept], dict[str, seed.Concept]]:
    exact: dict[str, seed.Concept] = {}
    alias: dict[str, seed.Concept] = {}
    for table in seed.TABLES_FOR_FACT_TYPE.get(fact_type, ()):
        for c in table:
            exact.setdefault(normalize(c.display), c)
            for a in c.aliases:
                alias.setdefault(normalize(a), c)
    return exact, alias


def bind_text(fact_type: str, local_text: str | None) -> Binding | None:
    exact, alias = _index(fact_type)
    if not exact:
        return None
    key = normalize(local_text)
    if not key:
        return None
    if key in exact:
        return Binding(exact[key], "exact")
    if key in alias:
        return Binding(alias[key], "alias")
    if fact_type == "lab_order":  # "S. Creatinine", "Sr TSH", "Lipid profile test"
        key = order_key(key)
        if key in exact or key in alias:
            return Binding(exact.get(key) or alias[key], "alias")
    if fact_type == "medication":
        words = key.split()
        if words and words[0] in LEADING_FORMS:  # "t dolo 650": T is the tablet, not the drug
            words = words[1:]
        tokens = [t for t in words if t not in _FORM_WORDS and not t.isdigit()
                  and not re.fullmatch(r"\d+(mg|mcg|ml|g|iu)?", t)]  # fmt: skip
        for tok in tokens:
            if tok in exact or tok in alias:
                return Binding(exact.get(tok) or alias[tok], "token")
        key = " ".join(tokens)
    close = difflib.get_close_matches(key, list(exact) + list(alias), n=1, cutoff=FUZZY_CUTOFF)
    if close:
        return Binding(exact.get(close[0]) or alias[close[0]], "fuzzy")
    return None


def bind_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Return the column updates for one ``clinical_fact`` row (+ ``medication`` updates)."""
    b = bind_text(fact["fact_type"], fact.get("local_text"))
    out: dict[str, Any] = {
        "code_system": b.concept.system if b else None,
        "code": b.concept.code if b else None,
        "code_display": b.concept.display if b else None,
        "code_status": "bound" if b else "local_only",
        "confidence_terminology": CONF_BOUND if b else CONF_LOCAL,
        "value_unit_ucum": to_ucum(fact.get("value_unit_ucum")) or fact.get("value_unit_ucum"),
        "how": b.how if b else None,
    }
    if fact["fact_type"] == "medication":
        out["medication"] = {
            "rxlike_system": out["code_system"],
            "rxlike_code": out["code"],
            "frequency_per_day": fact.get("frequency_per_day")
            if fact.get("frequency_per_day") is not None
            else freq_per_day(fact.get("frequency_code")),
            "dose_unit_ucum": to_ucum(fact.get("dose_unit_ucum")) or fact.get("dose_unit_ucum"),
        }
    return out


def _update_fact(conn, fact_id: UUID, u: dict[str, Any]) -> None:
    conn.execute(
        text(
            "UPDATE clinical_fact SET code_system = :code_system, code = :code, "
            "code_display = :code_display, code_status = :code_status, "
            "confidence_terminology = :confidence_terminology, "
            "value_unit_ucum = :value_unit_ucum, updated_at = now() WHERE id = :id"
        ),
        {"id": fact_id, **{k: u[k] for k in _FACT_UPDATE_COLS}},
    )
    if "medication" in u:
        conn.execute(
            text(
                "UPDATE medication_detail SET rxlike_system = :rxlike_system, "
                "rxlike_code = :rxlike_code, frequency_per_day = :frequency_per_day, "
                "dose_unit_ucum = :dose_unit_ucum WHERE fact_id = :id"
            ),
            {"id": fact_id, **u["medication"]},
        )


def bind_document(document_id: UUID | str, *, actor: str = "system") -> dict[str, Any]:
    with session_scope() as conn:
        model = f"seed:{get_settings().terminology_package}"
        run_id = repo.start_run(
            conn, document_id=document_id, stage="terminology", model_name=model
        )
    try:
        with session_scope() as conn:
            facts = repo.get_facts_for_document(conn, document_id)
            bound = 0
            for f in facts:
                u = bind_fact(f)
                _update_fact(conn, f["id"], u)
                bound += u["code_status"] == "bound"
            metrics = {"facts": len(facts), "bound": bound, "local_only": len(facts) - bound}
            # the medicine catalogue layer (Tata 1mg): identify each medication against the
            # catalogue, beside its coding; the extracted text is never changed
            from cdi_adapter.catalogue.service import match_document_medications

            metrics.update(match_document_medications(conn, document_id))
            # E10-S4: the unmapped-code rate trend (E3-S3 minting keeps feeding the same counter)
            prom.TERMINOLOGY.labels("bound").inc(bound)
            prom.TERMINOLOGY.labels("local_only").inc(len(facts) - bound)
            repo.set_document_status(conn, document_id, "normalized")
            repo.finish_run(conn, run_id, metrics=metrics)
            detail = {"stage": "terminology", **metrics}
            repo.write_audit(conn, actor=actor, action="update", entity="source_document",
                             entity_id=document_id, detail=detail)  # fmt: skip
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            err = {"stage": "terminology", "message": str(exc)}
            repo.set_document_status(conn, document_id, "error", error=err)
        raise
    log.info("terminology_bound", document_id=str(document_id), **metrics)
    return metrics
