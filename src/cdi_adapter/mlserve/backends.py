"""Model-gateway backends (ARCHITECTURE §3.4): ``stub`` | ``hf``.

``vllm`` is built later by backlog E2-S1 (dormant path in the as-built system).

* ``stub`` — deterministic keyword responder so tests and CI need no GPU. It reads only the text
  between ``<document>`` … ``</document>`` in the prompt (OCR blocks as ``[bN] text`` lines) and
  answers in the shape of the requested schema (``json_schema["$id"]``). With no schema it is a
  transcription request and it echoes the document lines.
* ``hf`` — transformers ``AutoModelForImageTextToText`` running Qwen2.5-VL-7B in bf16 with sdpa
  attention and a ``max_pixels`` cap (VRAM bound). Lazy-loads on the first request (~35 s on the
  pod) and falls back 7B -> 3B on CUDA out-of-memory. One forward pass at a time (a lock).
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from cdi_adapter.config import Settings, get_settings
from cdi_adapter.logging import get_logger

log = get_logger(__name__)


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
               "advice": _coded(_after("advice|adv", lines)), "confidence": _CONF}  # fmt: skip
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


def _default_loader(model_id: str, settings: Settings):  # pragma: no cover - needs a GPU pod
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model = AutoModelForImageTextToText.from_pretrained(
        model_id, torch_dtype=torch.bfloat16, attn_implementation="sdpa", device_map="cuda"
    )
    processor = AutoProcessor.from_pretrained(model_id, max_pixels=settings.vlm_max_pixels_ocr)
    return model, processor


def _is_oom(exc: BaseException) -> bool:
    return type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()


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


class HFBackend(Backend):
    name = "hf"
    device = "cuda"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        loader: Callable[[str, Settings], tuple[Any, Any]] = _default_loader,
        runner: Callable[..., str] = _default_runner,
    ) -> None:
        self.settings = settings or get_settings()
        self._loader, self._runner = loader, runner
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
        try:
            self._model, self._processor = self._loader(self.settings.vlm_model_id, self.settings)
            self.model = self.settings.vlm_model_id
        except Exception as exc:
            if not _is_oom(exc):
                raise
            log.warning("vlm_oom_fallback", primary=self.settings.vlm_model_id,
                        fallback=self.settings.vlm_fallback_model_id)  # fmt: skip
            self._free()
            self._model, self._processor = self._loader(
                self.settings.vlm_fallback_model_id, self.settings
            )
            self.model = self.settings.vlm_fallback_model_id
        log.info("vlm_loaded", model=self.model, seconds=round(time.perf_counter() - t0, 1))

    def _free(self) -> None:
        self._model = self._processor = None
        try:  # pragma: no cover - only meaningful with torch + CUDA present
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None) -> str:
        with self._lock:
            self._ensure_loaded()
            return self._runner(self._model, self._processor, prompt, image_png, max_tokens)


def make_backend(settings: Settings | None = None) -> Backend:
    s = settings or get_settings()
    if s.mlserve_backend == "hf":
        return HFBackend(s)
    return StubBackend()
