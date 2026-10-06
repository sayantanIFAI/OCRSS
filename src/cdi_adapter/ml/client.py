"""Model-gateway client + structured-output safety (ARCHITECTURE §3.4 "Client side").

``vlm_json(image, prompt, schema)`` = generate -> ``extract_json`` (tolerates fences / prose) ->
``repair_payload`` (rehomes values put in the wrong key, fills required confidence, drops stray
keys, resolves ``cdi:common.defs`` ``$ref``) -> validate against a ``referencing.Registry`` of all
local schemas -> on repeated failure returns the repaired best-effort object with
``_partial: true`` **instead of raising**. A document is never lost to a formatting nit, and a
``_partial`` payload is never auto-trusted downstream (S6 holds it for review).
"""

from __future__ import annotations

import base64
import copy
import difflib
import itertools
import json
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from cdi_adapter import tracing
from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger
from cdi_adapter.mlserve.backends import GenItem

if TYPE_CHECKING:
    from cdi_adapter.modelops.router import Route

log = get_logger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"
FILLED_CONFIDENCE = 0.5  # a confidence the model did not give is filled low, never high

# key aliases the 7B commonly emits instead of the schema's own names
_ALIASES = {
    "medications": ("medicines", "drugs", "rx", "prescriptions", "medication", "meds"),
    "tests": ("results", "lab_results", "labs", "investigations", "test_results", "analytes"),
    "vitals": ("vital_signs", "vital", "observations"),
    "diagnoses": ("diagnosis", "dx", "impression_diagnoses", "conditions", "provisional_diagnosis"),
    "allergies": ("allergy", "allergic_to", "drug_allergies"),
    "advice": ("instructions", "advise", "recommendations", "notes"),
    # tests a prescription advises (owner request 2026-09-28); only where the schema has the key
    "investigations_advised": ("investigations", "investigation", "tests_advised", "lab_tests",
                               "advised_tests", "tests", "ix", "lab_investigations",
                               "investigations_ordered", "tests_ordered"),
    "chief_complaints": ("complaints", "chief_complaint", "c_o", "presenting_complaints"),
    "findings_list": ("findings", "observations_list"),
    "impression_concepts": ("impression", "impressions", "conclusion"),
    "procedures": ("procedure_list", "operations", "surgeries"),
    "patient": ("patient_details", "patient_info", "demographics"),
    "encounter_date": ("date", "visit_date", "prescription_date", "consultation_date",
                       "date_of_visit", "opd_date"),
    "prescriber": ("doctor", "physician", "prescribed_by", "consultant", "doctor_name"),
}  # fmt: skip
_ITEM_NAME_ALIASES = (
    "name", "drug", "drug_name", "medicine", "medication", "medicine_name", "test", "test_name",
    "analyte", "vital", "parameter", "substance", "allergen", "text", "procedure",
)  # fmt: skip
# field-level aliases inside an item: schema field -> names the model uses for it. Applied only
# when the item schema has that field and the model did not already give it
_FIELD_ALIASES = {
    "frequency": ("dose_pattern", "dose_slot_pattern", "dosage_pattern", "dose_slots",
                  "schedule", "timing", "freq", "dosing", "times", "how_often"),
    "duration": ("days", "period", "course", "for"),
    "instructions": ("instruction", "directions", "remarks", "how_to_take", "note"),
    "dose": ("dosage",),
    "age_text": ("age",),
    "sex": ("gender",),
    "mrn": ("id", "patient_id", "uhid", "mr_no", "mrd_no", "registration_no", "reg_no"),
    "abha": ("abha_number", "abha_id", "abha_no"),
}  # fmt: skip
# patient details the model puts as loose top-level keys -> the key inside the "patient" block
_PATIENT_KEYS = {"name": "name", "patient_name": "name", "sex": "sex", "gender": "sex",
                 "age": "age_text", "age_text": "age_text", "dob": "dob", "date_of_birth": "dob",
                 "abha": "abha", "abha_number": "abha", "abha_id": "abha", "mrn": "mrn",
                 "uhid": "mrn"}  # fmt: skip
# vital signs the model puts as loose top-level keys (or as a {name: value} object)
_VITAL_KEYS = ("blood_pressure", "bp", "weight", "wt", "body_weight", "height", "ht", "pulse",
               "pulse_rate", "heart_rate", "hr", "temperature", "temp", "spo2", "sp02",
               "oxygen_saturation", "respiratory_rate", "rr", "bmi", "rbs", "grbs",
               "blood_sugar")  # fmt: skip
# follow-up keys: kept as an advice line, with the label the key carried
_FOLLOW_UP = {"review": "Review", "review_date": "Review", "review_after": "Review",
              "follow_up": "Follow-up", "followup": "Follow-up", "follow_up_date": "Follow-up",
              "next_visit": "Next visit", "next_review": "Review"}  # fmt: skip
_WRAPPER_MAX = 2  # unwrap at most this many nested single-key wrappers


# ------------------------------------------------------------------ schema registry


@lru_cache
def registry() -> Registry:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def schema_names() -> list[str]:
    return [p.name[: -len(".json")] for p in sorted(SCHEMA_DIR.glob("*.json"))]


def load_schema(name: str) -> dict[str, Any]:
    uri = name if name.startswith("cdi:") else f"cdi:{name}"
    return registry().contents(uri)


def _deref(node: Any, base: str) -> tuple[Any, str]:
    """Follow ``$ref`` (absolute ``cdi:`` or document-relative ``#/...``) to its target."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        uri = base + ref if ref.startswith("#") else ref
        doc, _, pointer = uri.partition("#")
        base = doc
        node = registry().contents(doc)
        for part in filter(None, pointer.split("/")):
            node = node[part]
    return node, base


def bundled_schema(name: str) -> dict[str, Any]:
    """The schema with every ``$ref`` inlined (for a server-side grammar that cannot resolve
    ``cdi:`` URIs)."""
    root = load_schema(name)

    def inline(node: Any, base: str) -> Any:
        node, base = _deref(node, base)
        if isinstance(node, dict):
            return {k: inline(v, base) for k, v in node.items() if k not in ("$defs", "$schema")}
        if isinstance(node, list):
            return [inline(v, base) for v in node]
        return node

    out = inline(root, root["$id"])
    out["$id"] = root["$id"]
    return out


def validate_schema(payload: Any, name: str) -> list[str]:
    """Validation errors as readable strings; empty list means valid."""
    schema = load_schema(name)
    validator = Draft202012Validator(schema, registry=registry())
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors]


# ------------------------------------------------------------------ JSON extraction


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """First JSON object in the model's text, tolerating ``` fences and surrounding prose."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(cand, i)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object found in model output")


# ------------------------------------------------------------------ repair


def _types(node: dict[str, Any]) -> set[str]:
    t = node.get("type")
    if isinstance(t, list):
        return set(t)
    if isinstance(t, str):
        return {t}
    types: set[str] = set()
    for alt in node.get("anyOf", []):
        types |= _types(alt)
    return types


def _fix_confidence(v: Any) -> Any:
    if isinstance(v, str):
        try:
            v = float(v.strip().rstrip("%")) / (100 if v.strip().endswith("%") else 1)
        except ValueError:
            return FILLED_CONFIDENCE
    if isinstance(v, bool) or not isinstance(v, int | float):
        return FILLED_CONFIDENCE
    if 1 < v <= 100:  # a percentage
        v = v / 100
    return min(max(float(v), 0.0), 1.0)


def _object_branch(node: dict[str, Any], base: str) -> tuple[dict[str, Any] | None, str]:
    node, base = _deref(node, base)
    if node.get("type") == "object":
        return node, base
    for alt in node.get("anyOf", []):
        alt, b = _deref(alt, base)
        if alt.get("type") == "object":
            return alt, b
    return None, base


def _repair_item(item: Any, item_schema: dict[str, Any], base: str) -> Any:
    obj, obase = _object_branch(item_schema, base)
    if obj is None:
        return item
    required = obj.get("required", [])
    name_key = next((k for k in required if k != "confidence"), None)
    only_object = "string" not in _types(_deref(item_schema, base)[0])
    if isinstance(item, str) and only_object and name_key:
        item = {name_key: item}
    if not isinstance(item, dict):
        return item
    item = dict(item)
    if name_key and name_key not in item:
        for alias in _ITEM_NAME_ALIASES:
            if alias in item and isinstance(item[alias], str):
                item[name_key] = item.pop(alias)
                break
    fields = obj.get("properties", {})
    for field, aliases in _FIELD_ALIASES.items():
        if field in fields and item.get(field) in (None, ""):
            found = next((a for a in aliases if item.get(a) not in (None, "")), None)
            if found is not None:
                item[field] = item.pop(found)
    for field, node in fields.items():
        if field in item and field != "confidence":
            item[field] = _scalar(item[field], _types(_deref(node, obase)[0]))
    if "confidence" in obj.get("properties", {}):
        if "confidence" in item:
            item["confidence"] = _fix_confidence(item["confidence"])
        elif "confidence" in required:
            item["confidence"] = FILLED_CONFIDENCE
    return item


_TRUE, _FALSE = {"true", "yes"}, {"false", "no"}


def _scalar(value: Any, types: set[str]) -> Any:
    """A boolean or number the model wrote as text (``"true"``, ``"0.9"``) -> the JSON type the
    schema asks for, only where the schema does not accept text there. 2026-09-25: the hf model
    answered ``"is_handwritten": "true"``, the classification failed the schema and every such
    upload fell back to ``other``: nothing extracted, nothing to review."""
    if not isinstance(value, str) or "string" in types:
        return value
    v = value.strip().lower()
    if "boolean" in types and v in _TRUE | _FALSE:
        return v in _TRUE
    if "integer" in types and re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if "number" in types and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", v):
        return float(v)
    return value


def _key(k: str) -> str:
    return k.lower().strip().replace(" ", "_").replace("/", "_").replace("-", "_")


def _known(key: str, props: dict[str, Any]) -> bool:
    """A top-level key the repair can place: a schema key, an alias of one, a loose vital, a
    follow-up line or a loose patient detail."""
    k = _key(key)
    return (key in props or k in props or k in _VITAL_KEYS or k in _FOLLOW_UP
            or ("patient" in props and k in _PATIENT_KEYS)
            or any(k in al for p, al in _ALIASES.items() if p in props))  # fmt: skip


def _loose_patient(payload: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """``"name": ..., "sex": ..., "age_text": ...`` at the top level -> the ``patient`` block.
    2026-09-26 (Qwen2.5-VL-7B int8, an OPD note): the model wrote the demographics loose, the
    repair dropped them as unknown keys and the upload stayed an "Unnamed patient". A detail the
    block already has is never overwritten; the values are moved exactly as given."""
    if "patient" not in props:
        return payload
    payload = dict(payload)
    have = payload.get("patient")
    block = dict(have) if isinstance(have, dict) else {}
    moved = False
    for k in list(payload):
        target = _PATIENT_KEYS.get(_key(k))
        value = payload[k]
        if k in props or target is None or not isinstance(value, str) or not value.strip():
            continue
        if block.get(target) in (None, ""):
            block[target] = payload.pop(k)
            moved = True
    if moved:
        payload["patient"] = block
    return payload


def _unwrap(payload: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """``{"prescription": {...}}`` -> ``{...}``: the answer wrapped in ONE extra key that the
    schema does not know. Values next to the wrapper (e.g. a top-level confidence) are kept."""
    for _ in range(_WRAPPER_MAX):
        strays = [k for k in payload if not k.startswith("_") and not _known(k, props)]
        if len(strays) != 1 or not isinstance(payload[strays[0]], dict):
            break
        inner = payload[strays[0]]
        rest = {k: v for k, v in payload.items() if k != strays[0]}
        payload = {**rest, **inner}
    return payload


def _lone_item(payload: dict[str, Any], root: dict[str, Any], base: str) -> dict[str, Any]:
    """A required list given as ONE flat item at the top level (``{"medication": "X",
    "strength": "10 mg", ...}``) -> ``{"medications": [{...}]}``. Needs the item's name plus at
    least one more of its fields, so an unrelated top-level key is never swallowed."""
    props: dict[str, Any] = root.get("properties", {})
    for key in root.get("required", []):
        node, nbase = _deref(props.get(key, {}), base)
        if "array" not in _types(node) or key in payload:
            continue
        aliases = _ALIASES.get(key, ())
        if any(isinstance(v, list) and _key(k) in aliases for k, v in payload.items()):
            continue
        obj, _ = _object_branch(node.get("items") or {}, nbase)
        if obj is None:
            continue
        fields = set(obj.get("properties", {}))
        name_key = next((k for k in obj.get("required", []) if k != "confidence"), None)
        names: set[str] = set(_ITEM_NAME_ALIASES) | ({name_key} if name_key else set())
        if not any(isinstance(payload.get(n), str) for n in names):
            continue
        item_keys = fields | names | {a for f, al in _FIELD_ALIASES.items() if f in fields
                                      for a in al}  # fmt: skip
        taken = [k for k in payload if not k.startswith("_") and _key(k) in item_keys
                 and (k not in props or k == "confidence")]  # fmt: skip
        body = [k for k in taken if _key(k) not in names | {"confidence", "evidence"}]
        if not body:  # the name alone (or with evidence) is not an item
            continue
        item = {k: payload[k] for k in taken}
        payload = {k: v for k, v in payload.items() if k not in taken or k == "confidence"}
        payload[key] = [item]
    return payload


def _loose_vitals(payload: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """``"blood_pressure": "145/84"`` / ``"vitals": {"bp": ..., "weight": ...}`` -> vital items
    ``{"name", "value", "confidence"}``. The value is kept exactly as the model gave it."""
    if "vitals" not in props:
        return payload
    payload = dict(payload)
    items: list[Any] = []

    def vital(name: str, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {"name": name, **value}
        return {"name": name, "value": value}

    v = payload.get("vitals")
    if isinstance(v, dict) and not {"name", "value"} & set(v):
        items += [vital(k, x) for k, x in v.items() if x not in (None, "")]
        payload.pop("vitals")
    for k in list(payload):
        if k not in props and _key(k) in _VITAL_KEYS and payload[k] not in (None, ""):
            items.append(vital(k, payload.pop(k)))
    if items:
        have = payload.get("vitals")
        payload["vitals"] = (have if isinstance(have, list) else [have] if have else []) + items
    return payload


def _follow_up(payload: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """``"review_date": "after 1 month"`` -> an advice line ``"Review after 1 month"``."""
    if "advice" not in props:
        return payload
    payload = dict(payload)
    lines = []
    for k in list(payload):
        label = _FOLLOW_UP.get(_key(k))
        if k not in props and label and isinstance(payload[k], str) and payload[k].strip():
            lines.append({"text": f"{label} {payload.pop(k).strip()}"})
    if lines:
        have = payload.get("advice")
        payload["advice"] = (have if isinstance(have, list) else [have] if have else []) + lines
    return payload


def repair_payload(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """Best-effort structural repair against schema ``name``. Never invents clinical values: it
    only moves what the model wrote to the key the schema expects. Handles a wrapper key, a lone
    flat item, alias key names (top level and inside items), loose vitals and follow-up lines;
    any top-level key it still cannot place is logged by name (never its value) and dropped."""
    root = load_schema(name)
    base = root["$id"]
    props: dict[str, Any] = root.get("properties", {})
    payload = _unwrap(payload, props)
    payload = _lone_item(payload, root, base)
    payload = _loose_patient(payload, props)
    payload = _loose_vitals(payload, props)
    payload = _follow_up(payload, props)
    out: dict[str, Any] = {}
    strays: dict[str, Any] = {}
    for k, v in payload.items():
        (out if k in props else strays)[k] = v

    # rehome values put under the wrong key
    dropped: list[str] = []
    for k, v in strays.items():
        if k.startswith("_"):
            continue
        key = _key(k)
        target = next((p for p, al in _ALIASES.items() if p in props and key in al), None)
        if target is None:
            close = difflib.get_close_matches(key, list(props), n=1, cutoff=0.8)
            target = close[0] if close else None
        if target and out.get(target) in (None, [], {}, ""):
            out[target] = v
        elif v not in (None, "", [], {}):
            dropped.append(k)
    if dropped:  # key names only: the values are patient data
        log.warning("repair_dropped_keys", schema=name, keys=dropped)

    for key, node in props.items():
        if key not in out:
            continue
        resolved, rbase = _deref(node, base)
        types = _types(resolved)
        value = out[key]
        if key == "confidence":
            out[key] = _fix_confidence(value)
        elif "array" in types:
            if value is None:
                out[key] = []
            elif not isinstance(value, list):
                out[key] = [value]
            items = resolved.get("items")
            if items is not None:
                out[key] = [_repair_item(i, items, rbase) for i in out[key]]
        elif "object" in types and resolved.get("type") == "object" and isinstance(value, dict):
            out[key] = _repair_item(value, resolved, rbase)
        else:
            out[key] = _scalar(value, types)

    if "confidence" in root.get("required", []) and "confidence" not in out:
        out["confidence"] = FILLED_CONFIDENCE
    # stray keys that could not be rehomed are dropped (additionalProperties: false), and logged
    return out


def is_partial(payload: dict[str, Any]) -> bool:
    return bool(payload.get("_partial"))


@dataclass(frozen=True)
class JsonRequest:
    """One document's structured-output request (``vlm_json`` arguments), for batching (E2-S8)."""

    image_png: bytes | None
    prompt: str
    schema: str
    max_tokens: int = 1024


# ------------------------------------------------------------------ clients


class GeneratedText(str):
    """A response's decoding guarantee travels with its text, including through batches."""

    grammar_locked: bool

    def __new__(cls, text: str, *, grammar_locked: bool = False):
        value = super().__new__(cls, text)
        value.grammar_locked = grammar_locked
        return value


class MLClient:
    """Base client: subclasses implement :meth:`generate`."""

    backend = "base"
    model = "unknown"
    version: str | None = None  # the registered model version serving it (backlog E10-S5)
    role = "primary"  # "primary" or "canary" (E10-S5)

    def generate(
        self,
        prompt: str,
        image_png: bytes | None = None,
        *,
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        raise NotImplementedError

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        """One text per item, in order (backlog E2-S8). The base calls ``generate`` in turn."""
        return [self.generate(i.prompt, i.image_png, max_tokens=i.max_tokens,
                              json_schema=i.json_schema) for i in items]  # fmt: skip

    def vlm_json_batch(self, requests: list[JsonRequest]) -> list[dict[str, Any]]:
        """``vlm_json`` for several documents with the first attempt in one batched call (E2-S8).

        Every document is its own sequence with its own prompt, image and schema. A document whose
        batched answer does not validate continues on exactly the single-call path (the same
        re-ask with its errors, the same retries, the same ``_partial`` fallback).
        """
        items = [GenItem(r.prompt, r.image_png, r.max_tokens, bundled_schema(r.schema))
                 for r in requests]  # fmt: skip
        texts: list[str | None] = [None] * len(items)
        if len(items) > 1:
            texts = list(self.generate_batch(items))
        return [
            self.vlm_json(r.image_png, r.prompt, r.schema, max_tokens=r.max_tokens, first_text=t)
            for r, t in zip(requests, texts, strict=True)
        ]

    def vlm_json(
        self,
        image_png: bytes | None,
        prompt: str,
        schema: str,
        *,
        max_tokens: int = 1024,
        retries: int | None = None,
        first_text: str | None = None,
    ) -> dict[str, Any]:
        """``first_text``: the answer to the first attempt, already generated (by a batch)."""
        retries = get_settings().extract_retries if retries is None else retries
        grammar = bundled_schema(schema)
        best: dict[str, Any] | None = None
        errors: list[str] = []
        ask = prompt
        for attempt in range(retries + 1):
            if attempt == 0 and first_text is not None:
                text = first_text
            else:
                text = self.generate(ask, image_png, max_tokens=max_tokens, json_schema=grammar)
            if isinstance(text, GeneratedText) and text.grammar_locked:
                # The gateway constrained every token. Never repair or retry an invalid
                # constrained response: fail the document and expose the serving defect.
                from cdi_adapter.mlserve.grammar import strict_json

                raw = strict_json(text)
                if validate_schema(raw, schema):
                    raise ValueError("grammar-locked gateway returned schema-invalid JSON")
                return raw
            try:
                repaired = repair_payload(extract_json(text), schema)
            except ValueError as exc:
                errors = [str(exc)]
            else:
                errors = validate_schema(repaired, schema)
                if not errors:
                    return repaired
                best = repaired
            ask = (
                f"{prompt}\n\nYour previous answer was invalid ({'; '.join(errors[:5])}). "
                "Return ONLY one JSON object that matches the schema."
            )
            log.info("vlm_json_retry", schema=schema, attempt=attempt + 1, errors=errors[:5])
        partial = copy.deepcopy(best) if best is not None else {}
        partial["_partial"] = True
        partial["_errors"] = errors[:20]
        log.warning("vlm_json_partial", schema=schema, errors=errors[:5])
        return partial


def split_urls(urls: str) -> list[str]:
    """``"http://a:8077, http://b:8078/"`` -> the replica URLs (E10-S9)."""
    return [u.strip().rstrip("/") for u in urls.split(",") if u.strip()]


# E10-S9: gateway replicas that just failed, shared by every client of this process
# (a client is made per document): replica URL -> time.monotonic() until which it is skipped
_down: dict[str, float] = {}
_down_lock = threading.Lock()
_turn = itertools.count()  # spreads calls over the replicas that are up
# the gateway process is gone or going (connection refused / reset / closed mid-answer); a slow
# answer (ReadTimeout) is NOT here: the same call elsewhere would only be as slow and doubles work
_REPLICA_GONE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                 httpx.ReadError, httpx.WriteError)  # fmt: skip
_REPLICA_BUSY = (502, 503, 504)  # restarting / loading the model / behind a dead proxy


def reset_replicas() -> None:
    with _down_lock:
        _down.clear()


class HttpMLClient(MLClient):
    """Calls the model gateway (``POST {CDI_MLSERVE_URL}/vlm/generate``).

    ``base_url`` may list several replicas of the same model, comma separated (backlog E10-S9).
    Each call starts at the next replica that is up; a replica that refuses the connection, drops
    it, or answers 502/503/504 is skipped for ``CDI_MLSERVE_DOWN_S`` and the call goes to the
    next one. When every replica fails, the last error is raised as with one gateway.
    """

    backend = "http"

    def __init__(self, base_url: str | None = None, *, timeout: float = 300.0,
                 transport: httpx.BaseTransport | None = None) -> None:  # fmt: skip
        self._timeout, self._transport = timeout, transport
        self._clients: dict[str, httpx.Client] = {}
        self._set_urls(base_url or get_settings().mlserve_url)
        self.model = get_settings().vlm_model_id
        self.replica: str | None = None  # the replica that answered last

    def _set_urls(self, urls: str) -> None:
        for c in self._clients.values():
            c.close()
        self._clients = {}
        self.urls = split_urls(urls)
        self.base_url = ",".join(self.urls)

    def _client(self, url: str) -> httpx.Client:
        c = self._clients.get(url)
        if c is None:
            c = self._clients[url] = httpx.Client(base_url=url, timeout=self._timeout,
                                                  transport=self._transport)  # fmt: skip
        return c

    def _order(self) -> list[str]:
        if len(self.urls) == 1:
            return self.urls
        k = next(_turn) % len(self.urls)
        order = self.urls[k:] + self.urls[:k]
        now = time.monotonic()
        with _down_lock:
            up = [u for u in order if _down.get(u, 0.0) <= now]
        return up + [u for u in order if u not in up]  # all down: try them anyway

    def _mark_down(self, url: str, why: str) -> None:
        from cdi_adapter import metrics

        with _down_lock:
            _down[url] = time.monotonic() + get_settings().mlserve_down_s
        metrics.MLSERVE_FAILOVERS.inc()
        log.warning("mlserve_replica_down", replica=url, error=why)

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        order = self._order()
        last: Exception | None = None
        for i, url in enumerate(order):
            more = i < len(order) - 1
            try:
                r = self._client(url).request(method, path, **kw)
            except _REPLICA_GONE as exc:
                if not more:
                    raise
                self._mark_down(url, f"{type(exc).__name__}: {exc}")
                last = exc
                continue
            if r.status_code in _REPLICA_BUSY and more:
                self._mark_down(url, f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            self.replica = url
            return r
        assert last is not None  # pragma: no cover - the last replica returns or raises
        raise last

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        body = {
            "prompt": prompt,
            "image_b64": base64.b64encode(image_png).decode() if image_png else None,
            "max_tokens": max_tokens,
            "json_schema": json_schema,
        }
        t0 = time.perf_counter()
        # the gateway continues this trace (E10-S3)
        r = self._request("POST", "/vlm/generate", json=body, headers=tracing.inject_headers({}))
        data = r.json()
        self.model = data.get("model", self.model)
        log.info("vlm_generate", model=self.model, seconds=round(time.perf_counter() - t0, 2))
        return GeneratedText(
            data["text"], grammar_locked=data.get("usage", {}).get("grammar_locked") is True
        )

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        body = {"items": [{"prompt": i.prompt,
                           "image_b64": base64.b64encode(i.image_png).decode()
                           if i.image_png else None,
                           "max_tokens": i.max_tokens, "json_schema": i.json_schema}
                          for i in items]}  # fmt: skip
        t0 = time.perf_counter()
        r = self._request("POST", "/vlm/generate_batch", json=body,
                          headers=tracing.inject_headers({}))  # fmt: skip
        data = r.json()
        self.model = data.get("model", self.model)
        texts: list[str] = data["texts"]
        if len(texts) != len(items):
            raise ValueError(f"gateway returned {len(texts)} texts for {len(items)} items")
        log.info("vlm_generate_batch", model=self.model, items=len(items),
                 seconds=round(time.perf_counter() - t0, 2))  # fmt: skip
        locks = data.get("usage", {}).get("grammar_locked", [False] * len(texts))
        if not isinstance(locks, list) or len(locks) != len(texts):
            raise ValueError("gateway returned invalid grammar metadata")
        return [
            GeneratedText(t, grammar_locked=flag is True)
            for t, flag in zip(texts, locks, strict=True)
        ]

    def healthz(self) -> dict[str, Any]:
        """The first replica that answers (the web app's /healthz: can extraction run at all)."""
        return dict(self._request("GET", "/healthz").json())


class StubMLClient(MLClient):
    """In-process deterministic keyword responder (CI / no GPU); same contract as the gateway."""

    backend = "stub"

    def __init__(self) -> None:
        from cdi_adapter.mlserve.backends import StubBackend

        self._backend = StubBackend()
        self.model = self._backend.model

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        return self._backend.generate(prompt, image_png, max_tokens=max_tokens,
                                      json_schema=json_schema)  # fmt: skip

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        return self._backend.generate_batch(items)


class RoutedClient(HttpMLClient):
    """The gateway client for one route of the rollout (backlog E10-S5).

    A canary that cannot answer (unreachable, or a 5xx) hands the document to the primary, so a
    broken canary costs a retry, never a document. A 4xx (a refused grammar, E2-S3) is an answer
    and is not retried elsewhere. ``version`` / ``role`` always name the side that answered last.
    """

    def __init__(self, route: Route, **kw: Any) -> None:
        super().__init__(route.url, **kw)
        if route.model_id:
            self.model = route.model_id
        self.version, self.role, self._fallback = route.version, route.role, route.fallback

    def _falls_back(self, exc: Exception) -> bool:
        if self._fallback is None:
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500
        return isinstance(exc, httpx.TransportError)

    def _to_fallback(self, exc: Exception) -> None:
        from cdi_adapter import metrics

        fb = self._fallback
        assert fb is not None
        log.warning("canary_fallback", canary=self.version, primary=fb.version, error=str(exc))
        metrics.CANARY_FALLBACKS.inc()
        self._set_urls(fb.url)
        self.model = fb.model_id or self.model
        self.version, self.role, self._fallback = fb.version, fb.role, None

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        try:
            return super().generate(prompt, image_png, max_tokens=max_tokens,
                                    json_schema=json_schema)  # fmt: skip
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if not self._falls_back(exc):
                raise
            self._to_fallback(exc)
            return super().generate(prompt, image_png, max_tokens=max_tokens,
                                    json_schema=json_schema)  # fmt: skip

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        try:
            return super().generate_batch(items)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if not self._falls_back(exc):
                raise
            self._to_fallback(exc)
            return super().generate_batch(items)


def client_for(route: Route) -> MLClient:
    """``stub`` backend -> in-process stub; anything else -> the gateway of ``route``."""
    if get_settings().mlserve_backend == "stub":
        return StubMLClient()
    return RoutedClient(route)


def get_client(document_id: Any = None) -> MLClient:
    """The client for ``document_id``'s side of the rollout: primary or canary (E10-S5)."""
    if get_settings().mlserve_backend == "stub":
        return StubMLClient()
    from cdi_adapter.modelops import router

    return client_for(router.route(document_id))
