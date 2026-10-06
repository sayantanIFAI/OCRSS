"""Model-gateway backends (ARCHITECTURE §3.4): ``stub`` | ``hf`` | ``vllm``.

``vllm`` (backlog E2-S1): vLLM AsyncLLMEngine with continuous batching, in its own venv.

* ``stub`` — deterministic keyword responder so tests and CI need no GPU. It reads only the text
  between ``<document>`` … ``</document>`` in the prompt (OCR blocks as ``[bN] text`` lines) and
  answers in the shape of the requested schema (``json_schema["$id"]``). With no schema it is a
  transcription request and it echoes the document lines.
* ``hf`` — transformers ``AutoModelForImageTextToText`` running Qwen2.5-VL-7B in bf16 with sdpa
  attention and a ``max_pixels`` cap (VRAM bound). Lazy-loads on the first request (~35 s on the
  pod) and falls back 7B -> 3B on CUDA out-of-memory. One forward pass at a time (a lock).
  ``hf`` cannot lock decoding to a grammar (that is ``vllm``, E2-S3), so a request's
  ``json_schema`` is shown to the model instead: :func:`with_schema` appends the schema's exact
  key layout to the prompt. Without it the model invents its own key names and wrappers, and the
  schema check then throws correctly read values away.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cdi_adapter import metrics
from cdi_adapter.config import Settings, get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class GenItem:
    """One sequence of a batched call (backlog E2-S8): the same fields as one ``generate``."""

    prompt: str
    image_png: bytes | None = None
    max_tokens: int = 1024
    json_schema: dict[str, Any] | None = None


def _branch(node: dict[str, Any]) -> dict[str, Any]:
    """The object branch of an ``anyOf`` (else its first branch); the node itself otherwise."""
    alts = node.get("anyOf")
    if not alts:
        return node
    return next((a for a in alts if a.get("type") == "object"), alts[0])


# what to write in a field, where the key name alone invites a guess (round 2 of the 2026-09-25
# check: with bare types the 7B split "1-0-1" into a numeric dose plus a shifted frequency, and
# "110/70" into value 110 + unit "/70 mmHg")
# Round 3: dose and route get NO example: with "e.g. 1 tab" / "e.g. oral" the model wrote
# dose "1 tab" and route "oral" onto medicines whose line says neither (a guess, §5 S6).
_HINTS = {
    "strength": "as written, e.g. 500 mg",
    "dose": "only if a dose is written on the page",
    "frequency": "as written, e.g. 1-0-1, BD, OD, SOS",
    "route": "only if a route is written on the page",
    "duration": "as written, e.g. 30 days",
    "instructions": "as written, e.g. after food",
    "value": "as written, e.g. 138/86 or 72",
    "unit": "as written, e.g. mmHg",
    "encounter_date": "as written",
}


def _is_quantity(node: dict[str, Any]) -> bool:
    """A value the schema also accepts as plain text (``"500 mg"``, ``"138/86"``): shown as
    text, so the model copies it as written instead of splitting it into number + unit."""
    alts = node.get("anyOf") or []
    obj = next((a for a in alts if a.get("type") == "object"), None)
    plain = any("string" in (a.get("type") if isinstance(a.get("type"), list)
                             else [a.get("type")]) for a in alts)  # fmt: skip
    return bool(obj and plain and "value" in obj.get("properties", {}))


def _shape(node: dict[str, Any], depth: int = 0, key: str | None = None) -> Any:
    if _is_quantity(node):
        return _HINTS.get(key or "", "text, as written")
    node = _branch(node)
    t = node.get("type")
    types = set(t) if isinstance(t, list) else {t}
    if "object" in types and node.get("properties"):
        if depth > 3:
            return {}
        props = node["properties"]
        if "text" in props and "code" in props:  # coded text: the model copies the words; the
            props = {k: v for k, v in props.items()  # codes come from S5, never from the model
                     if k in ("text", "evidence", "confidence")}  # fmt: skip
        return {k: _shape(v, depth + 1, k) for k, v in props.items()}
    if "array" in types:
        if key == "evidence":
            return ["b3"]
        return [_shape(node.get("items") or {}, depth + 1)]
    if "enum" in node:
        return " | ".join(str(e) for e in node["enum"])
    if key == "confidence":
        return "number 0-1"
    if types & {"number", "integer"}:
        return "number"
    return _HINTS.get(key or "", "text")


def schema_outline(json_schema: dict[str, Any]) -> str:
    """The key layout of a (bundled, ``$ref``-free) JSON schema as a JSON skeleton, e.g.
    ``{"medications": [{"drug_name": "text", ...}], "confidence": "number"}``."""
    return json.dumps(_shape(json_schema), separators=(", ", ": "))


def with_schema(prompt: str, json_schema: dict[str, Any] | None) -> str:
    """The prompt plus the schema's exact keys, for a backend that cannot enforce the schema."""
    if not json_schema or not json_schema.get("properties"):
        return prompt
    if str(json_schema.get("$id", "")).startswith("cdi:med_agent_"):
        # the medication agent (E12-S4) asks for judgement and medical knowledge, not for text
        # copied off the page: the extraction rules below ("never add a value that is not
        # written") would forbid the very answer it asks for
        return (
            f"{prompt}\n\nAnswer with ONE JSON object in exactly this shape, with these key names "
            'exactly. Use JSON null (not the string "null") for a value you cannot give.\n'
            f"{schema_outline(json_schema)}"
        )
    required = ", ".join(json_schema.get("required", [])) or "none"
    return (
        f"{prompt}\n\n"
        "Answer with ONE JSON object in exactly this shape. Use these key names exactly: do not "
        "rename them and do not wrap the object in another key. Fill EVERY key whose "
        "information is on the page, not only the required ones; leave out only keys with "
        "nothing on the page. Copy each value exactly as written and never split one written "
        "value across two keys. Never add a value that is not written on the page, even one "
        "that is usually true. A list holds one item per entry on the page (every medicine "
        f"is its own item). Always include: {required}.\n"
        f"{schema_outline(json_schema)}"
    )


class Backend:
    name = "base"
    model = "none"
    device = "cpu"

    @property
    def loaded(self) -> bool:
        return True

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
        """One text per item, in order. Every item is its own sequence with its own prompt,
        image and schema: nothing is shared between them (E2-S8). The base runs them in turn."""
        return [self.generate(i.prompt, i.image_png, max_tokens=i.max_tokens,
                              json_schema=i.json_schema) for i in items]  # fmt: skip


# =================================================================== stub

_BLOCK = re.compile(r"^\s*\[(b\d+)\]\s?(.*)$")
_DOC = re.compile(r"<document>(.*?)</document>", re.S | re.I)

_CLASS_SIGNALS: dict[str, tuple[str, ...]] = {
    "prescription": ("rx", "tab ", "tab.", "cap ", "syp ", "inj ", "prescription", "1-0-1", "bd",
                     "od", "tds", "x 15 days", "sig"),
    "lab_report": ("laboratory report", "reference range", "haemoglobin", "hemoglobin", "hba1c",
                   "creatinine", "cholesterol", "mg/dl", "specimen", "lab report"),
    "vitals_sheet": ("vitals", "vital signs", "pulse", "spo2", "respiratory rate", "temperature",
                     "blood pressure"),
    "discharge_summary": ("discharge summary", "date of admission", "date of discharge",
                          "course in hospital", "condition at discharge"),
    "radiology": ("radiology", "x-ray", "ultrasound", "ct scan", "mri", "impression",
                  "findings", "angiogram", "echocardiography"),
    "opd_note": ("opd", "chief complaint", "c/o", "history of present illness", "examination",
                 "consultation"),
    "referral": ("referral", "referred to", "kindly see"),
    "operative_note": ("operative note", "operation notes", "procedure performed", "anaesthesia"),
}  # fmt: skip

_MED = re.compile(r"\b(tab|tablet|cap|capsule|syp|syrup|inj|injection)\b\.?\s+(.+)", re.I)
_FREQ = re.compile(r"\b(\d-\d-\d(?:-\d)?|od|bd|bid|tds|tid|qid|hs|sos|stat|once daily|"
                   r"twice daily|thrice daily)\b", re.I)  # fmt: skip
_DUR = re.compile(r"(?:x|for)\s*(\d+)\s*(day|days|week|weeks|month|months)\b", re.I)
_NAME_END = re.compile(
    r"\s+(?:\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu)\b|\d-\d-\d|od\b|bd\b|tds\b|qid\b|hs\b|"
    r"x\s*\d|for\s+\d)",
    re.I,
)
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml|iu|units?)\b", re.I)
_LAB = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 ()%/.,-]*?)\s*[:\-]?\s+(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|[a-zA-Z/µ]+(?:/[a-zA-Z0-9.]+)?)?\s*"
    r"(?:\(?\s*(?P<range>\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?)\s*\)?)?\s*(?P<flag>[HL])?\s*$"
)
_VITALS = (
    ("blood_pressure", re.compile(r"\b(?:bp|blood pressure)\b[^0-9]*(\d{2,3}\s*/\s*\d{2,3})", re.I),
     "mm[Hg]"),
    ("heart_rate", re.compile(r"\b(?:pulse|pr|heart rate|hr)\b[^0-9]*(\d{2,3})", re.I), "/min"),
    ("temperature", re.compile(r"\b(?:temp|temperature)\b[^0-9]*(\d{2,3}(?:\.\d)?)", re.I), None),
    ("spo2", re.compile(r"\bspo2\b[^0-9]*(\d{2,3})", re.I), "%"),
    ("respiratory_rate", re.compile(r"\b(?:rr|resp(?:iratory)? rate)\b[^0-9]*(\d{1,2})", re.I),
     "/min"),
    ("body_weight", re.compile(r"\b(?:wt|weight)\b[^0-9]*(\d{1,3}(?:\.\d)?)", re.I), "kg"),
    ("body_height", re.compile(r"\b(?:ht|height)\b[^0-9]*(\d{2,3}(?:\.\d)?)", re.I), "cm"),
    ("bmi", re.compile(r"\bbmi\b[^0-9]*(\d{1,2}(?:\.\d)?)", re.I), "kg/m2"),
)  # fmt: skip
_SKIP_LAB = re.compile(r"^(name|age|sex|gender|date|mrn|uhid|abha|dr\.?|doctor|patient|ref|"
                       r"sample|page|mobile|phone)\b", re.I)  # fmt: skip
_CONF = 0.97


def _doc_lines(prompt: str) -> list[tuple[str | None, str]]:
    m = _DOC.search(prompt)
    body = m.group(1) if m else prompt
    out: list[tuple[str | None, str]] = []
    for raw in body.splitlines():
        if not raw.strip():
            continue
        bm = _BLOCK.match(raw)
        out.append((bm.group(1), bm.group(2).strip()) if bm else (None, raw.strip()))
    return out


def _ev(bid: str | None) -> list[str]:
    return [bid] if bid else []


def _after(label: str, lines, *, stop: tuple[str, ...] = ()) -> list[tuple[str | None, str]]:
    """Items on an inline 'Label: a, b' line, or the lines under a bare 'Label:' header up to
    the next label."""
    items: list[tuple[str | None, str]] = []
    pat = re.compile(rf"^\s*(?:{label})\s*[:\-]\s*(.*)$", re.I)
    for i, (bid, text) in enumerate(lines):
        m = pat.match(text)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:
            items += [(bid, p.strip()) for p in re.split(r"[;,]", rest) if p.strip()]
            continue
        for nbid, ntext in lines[i + 1 :]:
            if re.match(r"^\s*[A-Za-z /]+\s*:", ntext) or any(
                ntext.lower().startswith(s) for s in stop
            ):
                break
            items.append((nbid, ntext.strip(" -•*")))
    return items


def _patient(lines) -> dict[str, Any] | None:
    p: dict[str, Any] = {}
    ev: list[str] = []
    for bid, text in lines:
        if m := re.search(r"\bname\s*[:\-]\s*([A-Za-z .']+?)(?:\s{2,}|\s+(?:age|sex)\b|$)",
                          text, re.I):  # fmt: skip
            p["name"] = m.group(1).strip()
            ev += _ev(bid)
        if m := re.search(r"\bage\s*(?:/\s*sex)?\s*[:\-]\s*(\d{1,3}\s*(?:y|yrs|years)?)", text,
                          re.I):  # fmt: skip
            p["age_text"] = m.group(1).strip()
            ev += _ev(bid)
        if m := re.search(r"\b(?:sex|gender)\s*[:\-]\s*([A-Za-z]+)", text, re.I) or re.search(
            r"\bage\s*/\s*sex\s*[:\-]\s*\d{1,3}\s*\w*\s*/\s*([MF]\w*)", text, re.I
        ):
            p["sex"] = m.group(1)
            ev += _ev(bid)
        if m := re.search(r"\b(?:dob|date of birth)\s*[:\-]\s*([0-9./-]+)", text, re.I):
            p["dob"] = m.group(1)
        if m := re.search(r"\b(?:mrn|uhid)\s*[:\-]\s*([A-Za-z0-9-]+)", text, re.I):
            p["mrn"] = m.group(1)
        if m := re.search(r"\babha\s*(?:no\.?|number)?\s*[:\-]\s*([0-9-]{14,17})", text, re.I):
            p["abha"] = m.group(1)
    if not p:
        return None
    p["evidence"] = sorted(set(ev))
    return p


def _date(lines, labels: str) -> str | None:
    for _, text in lines:
        if m := re.search(rf"\b(?:{labels})\s*[:\-]\s*([0-9]{{1,4}}[./-][0-9A-Za-z]{{1,3}}"
                          rf"[./-][0-9]{{2,4}})", text, re.I):  # fmt: skip
            return m.group(1)
    return None


def _medications(lines) -> list[dict[str, Any]]:
    meds = []
    for bid, text in lines:
        m = _MED.search(text)
        if not m:
            # Printed OCR can join a form to a capitalized drug and its strength.
            # Keep this normalization in the synthetic stub, retaining the source evidence.
            spaced = re.sub(r"^(Tab|Cap|Syp|Inj)(?=[A-Z])", r"\1 ", text)
            spaced = re.sub(r"(?<=[A-Za-z])(?=\d+(?:mg|mcg|ml|iu)\b)", " ", spaced)
            m = _MED.search(spaced)
        if not m:
            continue
        rest = m.group(2)
        name = re.split(_NAME_END, rest, maxsplit=1)[0]
        med: dict[str, Any] = {"drug_name": f"{m.group(1).title()} {name.strip()}".strip(),
                               "evidence": _ev(bid), "confidence": _CONF}  # fmt: skip
        if s := _STRENGTH.search(rest):
            med["strength"] = {"value": float(s.group(1)), "unit": s.group(2).lower()}
        if f := _FREQ.search(rest):
            med["frequency"] = f.group(1)
        if d := _DUR.search(rest):
            med["duration"] = f"{d.group(1)} {d.group(2)}"
        if m.group(1).lower().startswith("inj"):
            med["route"] = "SC" if re.search(r"insulin|pen", rest, re.I) else "IV/IM"
        meds.append(med)
    return meds


def _vitals(lines) -> list[dict[str, Any]]:
    out, seen = [], set()
    for bid, text in lines:
        for name, pat, unit in _VITALS:
            if name in seen:
                continue
            if m := pat.search(text):
                out.append({"name": name, "value": m.group(1).replace(" ", ""), "unit": unit,
                            "evidence": _ev(bid), "confidence": _CONF})  # fmt: skip
                seen.add(name)
    return out


def _labs(lines) -> list[dict[str, Any]]:
    out = []
    for bid, text in lines:
        if _SKIP_LAB.match(text):
            continue
        if m := _LAB.match(text):
            item: dict[str, Any] = {"name": m.group("name").strip(" :-"),
                                    "value": float(m.group("value")), "unit": m.group("unit"),
                                    "evidence": _ev(bid), "confidence": _CONF}  # fmt: skip
            if m.group("range"):
                item["ref_range"] = m.group("range").replace(" ", "")
            if m.group("flag"):
                item["flag"] = m.group("flag")
            out.append(item)
    return out


def _allergies(lines) -> list[dict[str, Any]]:
    out = []
    for bid, text in lines:
        m = re.search(r"\b(?:allerg(?:y|ies)|allergic to)\s*[:\-]?\s*(.+)$", text, re.I)
        if m and not re.match(r"^(nil|none|nkda|no known)", m.group(1).strip(), re.I):
            for sub in re.split(r"[;,]", m.group(1)):
                if sub.strip():
                    out.append({"substance": sub.strip(), "evidence": _ev(bid),
                                "confidence": _CONF})  # fmt: skip
    return out


# the header a prescription writes its advised tests under (owner request 2026-09-28)
_TESTS_ADVISED = r"investigations?(?: advised)?|ix|tests? advised|lab tests?|advised tests?"


def _coded(items) -> list[dict[str, Any]]:
    return [{"text": t, "evidence": _ev(b), "confidence": _CONF} for b, t in items if t]


class StubBackend(Backend):
    name = "stub"
    model = "stub-keyword-responder"

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        lines = _doc_lines(prompt)
        sid = (json_schema or {}).get("$id", "")
        if not sid:
            # transcription: the stub cannot read pixels, so it only echoes supplied text
            return "\n".join(t for _, t in lines) if _DOC.search(prompt) else ""
        kind = sid.removeprefix("cdi:").split(".")[0]
        payload = getattr(self, f"_{kind}", self._unknown)(lines)
        return json.dumps(payload)

    # --- per schema
    def _classification(self, lines) -> dict[str, Any]:
        text = " ".join(t for _, t in lines).lower()
        scores = {k: sum(text.count(s) for s in sigs) for k, sigs in _CLASS_SIGNALS.items()}
        best = max(scores, key=lambda k: scores[k])
        ranked = sorted(scores.values(), reverse=True)
        clear = ranked[0] > 0 and ranked[0] - ranked[1] >= 2
        return {"doc_type": best if ranked[0] > 0 else "other",
                "confidence": 0.9 if clear else 0.55, "is_handwritten": False,
                "languages": ["en"], "specialty": None}  # fmt: skip

    def _prescription(self, lines) -> dict[str, Any]:
        out = {"medications": _medications(lines), "vitals": _vitals(lines),
               "allergies": _allergies(lines),
               "diagnoses": _coded(_after("diagnosis|dx|diagnoses|impression", lines)),
               "advice": _coded(_after("advice|adv", lines)),
               "investigations_advised": _coded(_after(_TESTS_ADVISED, lines)),
               "confidence": _CONF}  # fmt: skip
        return self._with_meta(out, lines, "encounter_date", "date|visit date")

    def _lab_report(self, lines) -> dict[str, Any]:
        return self._with_meta({"tests": _labs(lines), "confidence": _CONF}, lines,
                               "reported_at", "reported on|report date|date")  # fmt: skip

    def _vitals(self, lines) -> dict[str, Any]:
        out = {"vitals": _vitals(lines), "allergies": _allergies(lines), "confidence": _CONF}
        return self._with_meta(out, lines, "recorded_at", "recorded on|date")

    def _opd_note(self, lines) -> dict[str, Any]:
        out = {"chief_complaints": _coded(_after("c/o|chief complaints?|complaints", lines)),
               "findings": _coded(_after("o/e|on examination|findings|examination", lines)),
               "diagnoses": _coded(_after("diagnosis|dx|impression", lines)),
               "vitals": _vitals(lines), "medications": _medications(lines),
               "allergies": _allergies(lines), "advice": _coded(_after("advice|plan", lines)),
               "investigations_advised": _coded(_after(_TESTS_ADVISED, lines)),
               "confidence": _CONF}  # fmt: skip
        return self._with_meta(out, lines, "encounter_date", "date|visit date")

    def _discharge_summary(self, lines) -> dict[str, Any]:
        procs = [{"name": t, "evidence": _ev(b), "confidence": _CONF}
                 for b, t in _after("procedures?|operation|surgery", lines)]  # fmt: skip
        out = {"diagnoses": _coded(_after("final diagnosis|diagnosis|diagnoses", lines)),
               "procedures": procs, "medications": _medications(lines),
               "vitals": _vitals(lines), "allergies": _allergies(lines),
               "advice": _coded(_after("advice on discharge|advice|follow up", lines)),
               "confidence": _CONF}  # fmt: skip
        out = self._with_meta(out, lines, "admission_date", "date of admission|doa|admitted on")
        if d := _date(lines, "date of discharge|dod|discharged on"):
            out["discharge_date"] = d
        return out

    def _radiology(self, lines) -> dict[str, Any]:
        out = {"findings_list": _coded(_after("findings", lines, stop=("impression",))),
               "impression_concepts": _coded(_after("impression|conclusion", lines)),
               "confidence": _CONF}  # fmt: skip
        for _, text in lines:
            if m := re.search(r"\b(x-ray|ultrasound|usg|ct|mri|angiogra\w*|echo\w*)\b", text, re.I):
                out["modality"] = m.group(1)
                out["study"] = text
                break
        return self._with_meta(out, lines, "study_date", "study date|date")

    # the medication agent (E12-S4): the stub cannot read handwriting nor knows medicines, so it
    # chooses no product and links no diagnosis; the agent then records "no suggestion"
    def _med_agent_recall(self, lines) -> dict[str, Any]:
        return {"lines": [], "confidence": 0.3}

    def _med_agent_choice(self, lines) -> dict[str, Any]:
        return {"lines": [], "confidence": 0.3}

    def _med_agent_fit(self, lines) -> dict[str, Any]:
        return {"lines": [], "confidence": 0.3}

    def _med_agent_verify(self, lines) -> dict[str, Any]:
        return {"pairs": [], "confidence": 0.3}

    def _unknown(self, lines) -> dict[str, Any]:
        return {"confidence": 0.3}

    @staticmethod
    def _with_meta(out: dict[str, Any], lines, date_key: str, date_labels: str) -> dict[str, Any]:
        if p := _patient(lines):
            out["patient"] = p
        if d := _date(lines, date_labels):
            out[date_key] = d
        return out


# =================================================================== hf (Qwen2.5-VL)


def _revision(model_id: str, settings: Settings) -> str | None:
    """The registered version's pinned revision (E10-S5); it pins the primary model only."""
    return (settings.vlm_model_revision or None) if model_id == settings.vlm_model_id else None


def _load_kwargs(model_id: str, settings: Settings) -> dict[str, Any]:
    """``from_pretrained`` arguments: bf16 + sdpa on the GPU; with ``vlm_quantize="8bit"`` the
    linear layers are stored as int8 (bitsandbytes LLM.int8), the rest stays bf16."""
    import torch

    kwargs: dict[str, Any] = {
        "revision": _revision(model_id, settings),
        "torch_dtype": torch.bfloat16,
        "attn_implementation": "sdpa",
        "device_map": "cuda",
    }
    if settings.vlm_quantize == "8bit":
        from transformers import BitsAndBytesConfig

        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
    return kwargs


def _default_loader(model_id: str, settings: Settings):  # pragma: no cover - needs a GPU pod
    from transformers import AutoModelForImageTextToText, AutoProcessor

    rev = _revision(model_id, settings)
    kwargs = _load_kwargs(model_id, settings)
    model = AutoModelForImageTextToText.from_pretrained(model_id, **kwargs)
    processor = AutoProcessor.from_pretrained(
        model_id, revision=rev, max_pixels=settings.vlm_max_pixels_ocr
    )
    return model, processor


def _is_oom(exc: BaseException) -> bool:
    return type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()


def _release_cuda() -> None:
    """Collect unreachable objects, then hand the cached CUDA blocks back. Model graphs hold
    reference cycles, so their tensors are freed only by the collector, not by dropping a name."""
    import gc

    gc.collect()
    try:  # pragma: no cover - only meaningful with torch + CUDA present
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _load_with_fallback(load: Callable[[str], Any], settings: Settings,
                        release: Callable[[], None]) -> tuple[Any, str]:  # fmt: skip
    """``load`` the primary model, or the fallback on CUDA out-of-memory -> (loaded, model id).

    The fallback loads only after the ``except`` block has ended: until then the caught error's
    traceback holds the loader's frames, and with them the half-built primary's weights, so
    nothing can be freed (Colab T4, 2026-09-26: the 7B took 14.4 of 14.6 GiB, and the 3B then
    ran out of memory on what the failed 7B still held)."""
    primary, fallback = settings.vlm_model_id, settings.vlm_fallback_model_id
    try:
        return load(primary), primary
    except Exception as exc:
        if not _is_oom(exc):
            raise
    metrics.VLM_OOM.labels("load").inc()
    log.warning("vlm_oom_fallback", primary=primary, fallback=fallback)
    release()
    return load(fallback), fallback


def _default_runner(model, processor, prompt: str, image_png: bytes | None,
                    max_tokens: int) -> str:  # pragma: no cover - needs a GPU pod  # fmt: skip
    import torch
    from PIL import Image
    from qwen_vl_utils import process_vision_info

    content: list[dict[str, Any]] = []
    if image_png:
        content.append({"type": "image", "image": Image.open(io.BytesIO(image_png)).convert("RGB")})
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]
    chat = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    images, videos = process_vision_info(messages)
    inputs = processor(text=[chat], images=images, videos=videos, return_tensors="pt").to("cuda")
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
    trimmed = out[:, inputs.input_ids.shape[1] :]
    return processor.batch_decode(trimmed, skip_special_tokens=True)[0]


def _default_batch_runner(model, processor, items: list[GenItem]) -> list[str]:  # pragma: no cover
    """One padded ``generate`` over every item (E2-S8): output tokens are produced for all
    sequences in the same decode steps. Left padding, as batched decoder-only generation needs;
    each row is cut to its own ``max_tokens`` (greedy, so that is the text it would stop at)."""
    import torch
    from PIL import Image
    from qwen_vl_utils import process_vision_info

    conversations = []
    for it in items:
        content: list[dict[str, Any]] = []
        if it.image_png:
            content.append(
                {"type": "image", "image": Image.open(io.BytesIO(it.image_png)).convert("RGB")}
            )
        content.append({"type": "text", "text": it.prompt})
        conversations.append([{"role": "user", "content": content}])
    chats = [processor.apply_chat_template(c, tokenize=False, add_generation_prompt=True)
             for c in conversations]  # fmt: skip
    images, videos = process_vision_info(conversations)
    processor.tokenizer.padding_side = "left"
    inputs = processor(text=chats, images=images, videos=videos, padding=True,
                       return_tensors="pt").to("cuda")  # fmt: skip
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max(it.max_tokens for it in items),
                             do_sample=False)  # fmt: skip
    trimmed = out[:, inputs.input_ids.shape[1] :]
    return [processor.decode(row[: it.max_tokens], skip_special_tokens=True)
            for row, it in zip(trimmed, items, strict=True)]  # fmt: skip


class HFBackend(Backend):
    name = "hf"
    device = "cuda"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        loader: Callable[[str, Settings], tuple[Any, Any]] = _default_loader,
        runner: Callable[..., str] = _default_runner,
        batch_runner: Callable[..., list[str]] = _default_batch_runner,
    ) -> None:
        self.settings = settings or get_settings()
        self._loader, self._runner, self._batch_runner = loader, runner, batch_runner
        self._model = self._processor = None
        self.model = self.settings.vlm_model_id
        self._lock = threading.Lock()  # one forward pass at a time

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        t0 = time.perf_counter()
        (self._model, self._processor), self.model = _load_with_fallback(
            lambda mid: self._loader(mid, self.settings), self.settings, self._free
        )
        log.info("vlm_loaded", model=self.model, seconds=round(time.perf_counter() - t0, 1))

    def _free(self) -> None:
        self._model = self._processor = None
        self._empty_cache()

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        prompt = with_schema(prompt, json_schema)  # hf cannot lock decoding: show the keys
        with self._lock:
            self._ensure_loaded()
            return self._runner(self._model, self._processor, prompt, image_png, max_tokens)

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        """One batched forward pass for all items (E2-S8), still one pass at a time on the GPU.
        A batch that does not fit (CUDA OOM) is run item by item instead, never dropped."""
        if len(items) <= 1:
            return super().generate_batch(items)
        items = [GenItem(with_schema(i.prompt, i.json_schema), i.image_png, i.max_tokens,
                         None) for i in items]  # fmt: skip
        with self._lock:
            self._ensure_loaded()
            try:
                return self._batch_runner(self._model, self._processor, items)
            except Exception as exc:
                if not _is_oom(exc):
                    raise
                metrics.VLM_OOM.labels("batch").inc()
                log.warning("vlm_batch_oom_split", items=len(items))
                self._empty_cache()
        return super().generate_batch(items)

    @staticmethod
    def _empty_cache() -> None:
        _release_cuda()


# =================================================================== vllm (backlog E2-S1)
#
# vLLM ``AsyncLLMEngine``: paged KV cache + continuous batching (§3.4). The engine runs on its
# own asyncio loop in a background thread; the gateway serves ``/vlm/generate`` from a thread pool,
# so concurrent requests reach the engine together and are batched, while ``generate`` keeps the
# same synchronous contract as ``hf`` and ``stub``. vLLM 0.11.0 is the last release on torch 2.8
# (App. B) and needs transformers < 5, so it runs in the gateway's own venv (``.venv-vllm``,
# ``infra/runpod/requirements-vllm.lock``); the pipeline venv is untouched.
#
# Grammar-locked decoding (backlog E2-S3): a request's ``json_schema`` becomes an exact XGrammar
# grammar (``mlserve.grammar``) and vLLM masks every token it cannot accept. The engine's
# structured-output backend is fixed to xgrammar with no fallback, so a request is never quietly
# served by another backend or unconstrained. ``CDI_VLLM_GUIDED=false`` switches it off.


def _default_engine_factory(model_id: str, settings: Settings):  # pragma: no cover - GPU pod
    from vllm import AsyncEngineArgs, AsyncLLMEngine

    args = AsyncEngineArgs(
        model=model_id,
        revision=_revision(model_id, settings),
        dtype="bfloat16",
        gpu_memory_utilization=settings.vllm_gpu_memory_utilization,
        max_model_len=settings.vllm_max_model_len,
        max_num_seqs=settings.vllm_max_num_seqs,
        limit_mm_per_prompt={"image": 1},
        mm_processor_kwargs={"max_pixels": settings.vlm_max_pixels_ocr},
        enforce_eager=False,
        **_structured_output_args(settings),
    )
    return AsyncLLMEngine.from_engine_args(args)


def _structured_output_args(settings: Settings) -> dict[str, Any]:  # pragma: no cover - needs vllm
    from vllm.config import StructuredOutputsConfig

    return {
        "structured_outputs_config": StructuredOutputsConfig(**structured_output_config(settings))
    }


def structured_output_config(settings: Settings) -> dict[str, Any]:
    """vLLM ``StructuredOutputsConfig`` fields (E2-S3): xgrammar only, never a silent fallback."""
    return {"backend": "xgrammar", "disable_fallback": True}


def _default_prompt_builder(
    model_id: str, *, revision: str | None = None
) -> Callable[[str, bool], str]:  # pragma: no cover
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(model_id, revision=revision)

    def build(prompt: str, with_image: bool) -> str:
        content: list[dict[str, Any]] = [{"type": "image"}] if with_image else []
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        return str(processor.apply_chat_template(messages, tokenize=False,
                                                 add_generation_prompt=True))  # fmt: skip

    return build


def _default_sampling(max_tokens: int, grammar: str | None = None) -> Any:  # pragma: no cover
    from vllm import SamplingParams
    from vllm.sampling_params import StructuredOutputsParams

    so = StructuredOutputsParams(grammar=grammar) if grammar else None
    # greedy, as hf do_sample=False
    return SamplingParams(temperature=0.0, max_tokens=max_tokens, structured_outputs=so)


class VLLMBackend(Backend):
    name = "vllm"
    device = "cuda"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        engine_factory: Callable[[str, Settings], Any] = _default_engine_factory,
        prompt_builder: Callable[[str], Callable[[str, bool], str]] = _default_prompt_builder,
        sampling: Callable[[int, str | None], Any] = _default_sampling,
        grammar_builder: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        from cdi_adapter.mlserve.grammar import schema_to_ebnf

        self.settings = settings or get_settings()
        self._engine_factory, self._prompt_builder, self._sampling = (
            engine_factory, prompt_builder, sampling)  # fmt: skip
        self._grammar = grammar_builder or schema_to_ebnf
        self.model = self.settings.vlm_model_id
        self._engine: Any = None
        self._build: Callable[[str, bool], str] | None = None
        self._loop: Any = None
        self._start_lock = threading.Lock()  # only engine start-up is serialised, not generation
        self._seq = 0
        self._loop_thread: threading.Thread | None = None

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def _ensure_loaded(self) -> None:
        if self._engine is not None:
            return
        with self._start_lock:
            if self._engine is not None:
                return
            import asyncio

            loop = asyncio.new_event_loop()

            def serve_loop() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_forever()
                finally:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()

            thread = threading.Thread(target=serve_loop, name="vllm-engine", daemon=True)
            thread.start()
            t0 = time.perf_counter()

            async def initialize() -> tuple[Any, str, Callable[[str, bool], str]]:
                engine, model = _load_with_fallback(
                    lambda mid: self._engine_factory(mid, self.settings),
                    self.settings,
                    _release_cuda,
                )
                try:
                    builder = (
                        _default_prompt_builder(model, revision=_revision(model, self.settings))
                        if self._prompt_builder is _default_prompt_builder
                        else self._prompt_builder(model)
                    )
                except BaseException:
                    if shutdown := getattr(engine, "shutdown", None):
                        shutdown()
                    raise
                return engine, model, builder

            try:
                engine, self.model, self._build = asyncio.run_coroutine_threadsafe(
                    initialize(), loop
                ).result()
            except BaseException:
                loop.call_soon_threadsafe(loop.stop)
                thread.join()
                raise
            self._loop, self._loop_thread, self._engine = loop, thread, engine
            log.info("vlm_loaded", backend="vllm", model=self.model,
                     seconds=round(time.perf_counter() - t0, 1))  # fmt: skip

    def close(self) -> None:
        """Release the engine process and its event loop when the gateway shuts down."""
        import asyncio

        with self._start_lock:
            if self._loop is None:
                return
            loop, thread, engine = self._loop, self._loop_thread, self._engine

            async def shutdown() -> None:
                if stop := getattr(engine, "shutdown", None):
                    stop()

            try:
                asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=10)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                if thread is not None:
                    thread.join(timeout=10)
                self._loop = self._loop_thread = self._engine = self._build = None

    async def _run(self, request: dict[str, Any], params: Any, request_id: str) -> Any:
        final = None
        async for out in self._engine.generate(request, params, request_id):
            final = out
        if final is None or not final.outputs:
            raise RuntimeError(f"vllm returned no output for {request_id}")
        return final

    def kv_cache_tokens(self) -> int:
        """Tokens the engine's paged KV cache holds (blocks x block size), as sized by vLLM at
        start-up from ``gpu_memory_utilization``. Used by the E2-S6 benchmark for slot counts."""
        self._ensure_loaded()
        cache = self._engine.vllm_config.cache_config
        return int(cache.num_gpu_blocks or 0) * int(cache.block_size)

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        return self.generate_with_usage(prompt, image_png, max_tokens=max_tokens,
                                        json_schema=json_schema)[0]  # fmt: skip

    def generate_batch(self, items: list[GenItem]) -> list[str]:
        """All items reach the engine together, so continuous batching decodes them in the same
        steps (E2-S8); each keeps its own grammar lock."""
        from concurrent.futures import ThreadPoolExecutor

        if len(items) <= 1:
            return super().generate_batch(items)

        def one(i: GenItem) -> str:
            return self.generate(i.prompt, i.image_png, max_tokens=i.max_tokens,
                                 json_schema=i.json_schema)  # fmt: skip

        with ThreadPoolExecutor(max_workers=len(items), thread_name_prefix="vllm-batch") as pool:
            return list(pool.map(one, items))

    def generate_with_usage(
        self,
        prompt: str,
        image_png: bytes | None = None,
        *,
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
    ) -> tuple[str, int, int]:
        """``generate`` plus (prompt tokens incl. image tokens, generated tokens) (E2-S6)."""
        import asyncio

        from PIL import Image

        # built before the engine is touched: a schema that cannot be locked exactly fails fast
        grammar = (self._grammar(json_schema)
                   if json_schema is not None and self.settings.vllm_guided else None)  # fmt: skip
        self._ensure_loaded()
        assert self._build is not None
        request: dict[str, Any] = {"prompt": self._build(prompt, image_png is not None)}
        if image_png:
            image = Image.open(io.BytesIO(image_png)).convert("RGB")
            request["multi_modal_data"] = {"image": image}
        with self._start_lock:
            self._seq += 1
            request_id = f"cdi-{self._seq}"
        fut = asyncio.run_coroutine_threadsafe(
            self._run(request, self._sampling(max_tokens, grammar), request_id), self._loop
        )
        try:
            final = fut.result(timeout=self.settings.vllm_request_timeout_s)
        except TimeoutError:
            # Cancellation reaches vLLM's generate(), which aborts the engine request.
            fut.cancel()
            raise TimeoutError("vLLM generation exceeded its deadline") from None
        out = final.outputs[0]
        if json_schema is not None and self.settings.vllm_guided:
            from cdi_adapter.mlserve.grammar import validate_output

            validate_output(str(out.text), json_schema)
        return (str(out.text), len(getattr(final, "prompt_token_ids", None) or ()),
                len(getattr(out, "token_ids", None) or ()))  # fmt: skip


def make_backend(settings: Settings | None = None) -> Backend:
    s = settings or get_settings()
    if s.mlserve_backend == "hf":
        return HFBackend(s)
    if s.mlserve_backend == "vllm":
        return VLLMBackend(s)
    return StubBackend()
