"""S2 classify (ARCHITECTURE §5 S2). Runs after S3, so the page OCR text is already persisted.

``_heuristic_classify(text)``: scored keyword classifier (``_SIGNALS``: weighted word-boundary
patterns across the 8 clinical doc types; weight 3 = near-unique anchor, 2 = strong,
1 = corroborating). It returns a type **only** when the winner scores >= 5 *and* leads the
runner-up by >= 3 on a page that is >= 180 chars and >= 75 % alphanumeric (cleanly OCR'd, almost
certainly printed). Everything else returns ``None`` and falls through to the VLM with schema
``classification.v1``. Deliberately conservative: it trades coverage for never handing S4 the
wrong schema. Gated by ``CDI_FAST_CLASSIFY`` (default on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cdi_adapter import repo
from cdi_adapter.classify.prompt import DOC_TYPES, build_classification_prompt
from cdi_adapter.config import get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.logging import get_logger
from cdi_adapter.ml.client import MLClient, get_client, is_partial
from cdi_adapter.storage import ObjectStore, get_store

log = get_logger(__name__)

MIN_SCORE = 5
MIN_LEAD = 3
MIN_CHARS = 180
MIN_ALNUM = 0.75
HEURISTIC_CONFIDENCE = 0.95


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", re.I)


_SIGNALS: dict[str, list[tuple[re.Pattern[str], int]]] = {
    "prescription": [
        (_p(r"rx"), 3), (_p(r"prescription"), 3),
        (_p(r"(?:tab|tablet|cap|capsule|syp|syrup|inj)\.?"), 2), (_p(r"\d-\d-\d(?:-\d)?"), 2),
        (_p(r"(?:od|bd|tds|qid|hs|sos)"), 1), (_p(r"(?:after|before) (?:food|meals?)"), 1),
        (_p(r"\d+(?:\.\d+)? ?(?:mg|mcg|ml|iu)(?!/)"), 1),  # dose strength, not a lab "mg/dL"
    ],
    "lab_report": [
        (_p(r"(?:laboratory|lab|pathology|investigation) report"), 3),
        (_p(r"(?:biological )?(?:reference|ref\.?|normal) (?:range|interval|value)s?"), 3),
        (_p(r"(?:specimen|sample (?:type|collected|received))"), 2),
        (_p(r"(?:ha?emoglobin|hba1c|creatinine|cholesterol|platelets?|wbc|bilirubin|glucose|"
            r"urea|triglycerides?|tsh)"), 2),  # fmt: skip
        (_p(r"(?:mg/dl|g/dl|mmol/l|iu/l|u/l|cells/cumm)"), 1), (_p(r"pathologist"), 1),
    ],
    "vitals_sheet": [
        (_p(r"(?:vital signs?|vitals (?:chart|sheet|record)|tpr chart)"), 3),
        (_p(r"(?:spo2|oxygen saturation)"), 2), (_p(r"resp(?:iratory)? rate"), 2),
        (_p(r"pulse"), 1), (_p(r"temperature"), 1),
    ],
    "discharge_summary": [
        (_p(r"discharge summary"), 3), (_p(r"(?:date of admission|admitted on|doa)"), 2),
        (_p(r"(?:date of discharge|discharged on|dod)"), 2),
        (_p(r"(?:course in (?:the )?hospital|hospital course)"), 2),
        (_p(r"condition (?:at|on) discharge"), 2), (_p(r"advice on discharge"), 1),
    ],
    "radiology": [
        (_p(r"(?:x-ray|radiograph|ultrasound|usg|ct scan|mri|mammogra\w*|doppler|angiogra\w*|"
            r"echocardiogra\w*)"), 3),  # fmt: skip
        (_p(r"(?:radiology|imaging) (?:report|department)"), 2), (_p(r"radiologist"), 2),
        (_p(r"impression"), 2), (_p(r"findings"), 1),
    ],
    "opd_note": [
        (_p(r"(?:chief|presenting) complaints?"), 3), (_p(r"c/o"), 2),
        (_p(r"(?:opd|out ?patient) (?:card|note|consultation)"), 2),
        (_p(r"(?:history of present illness|hpi|past history)"), 2),
        (_p(r"(?:on examination|o/e|general examination)"), 2), (_p(r"provisional diagnosis"), 1),
    ],
    "referral": [
        (_p(r"referral (?:letter|note|slip)"), 3), (_p(r"referred to"), 2),
        (_p(r"(?:kindly (?:see|evaluate)|for further management|your opinion)"), 2),
        (_p(r"dear dr\.?"), 2),
    ],
    "operative_note": [
        (_p(r"(?:operative|operation|op) notes?"), 3),
        (_p(r"(?:procedure|surgery) performed"), 2), (_p(r"ana?esthesia"), 2),
        (_p(r"(?:surgeon|incision|intra-?operative)"), 2),
    ],
}  # fmt: skip


@dataclass(frozen=True)
class HeuristicResult:
    doc_type: str
    score: int
    runner_up_score: int


def _legible(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    if len(text.strip()) < MIN_CHARS or not chars:
        return False
    return sum(c.isalnum() for c in chars) / len(chars) >= MIN_ALNUM


def score_text(text: str) -> dict[str, int]:
    return {
        doc_type: sum(w for pat, w in signals if pat.search(text))
        for doc_type, signals in _SIGNALS.items()
    }


def _heuristic_classify(text: str) -> HeuristicResult | None:
    if not _legible(text):
        return None
    ranked = sorted(score_text(text).items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= MIN_SCORE and top - second >= MIN_LEAD:
        return HeuristicResult(best, top, second)
    return None


def _ocr_hint(png: bytes) -> str:
    """One-off RapidOCR pass: only when no ``ocr_block`` rows exist (the Celery path)."""
    from cdi_adapter.ocr.rapid import run_rapidocr

    return "\n".join(ln.text for ln in run_rapidocr(png))


def classify_document(
    document_id: UUID | str,
    *,
    client: MLClient | None = None,
    store: ObjectStore | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    settings = get_settings()
    with session_scope() as conn:
        if repo.get_document(conn, document_id) is None:
            raise LookupError(f"document {document_id} not found")
        blocks = repo.get_ocr_blocks(conn, document_id)
        pages = repo.get_pages(conn, document_id)
        run_id = repo.start_run(conn, document_id=document_id, stage="classify")

    try:
        page1 = [b["text"] for b in blocks if b["page_no"] == 1]
        page1_png: bytes | None = None
        if page1:
            hint = "\n".join(page1)
        else:
            page1_png = (store or get_store()).get_bytes(pages[0]["storage_key"])
            hint = _ocr_hint(page1_png)

        heuristic = _heuristic_classify(hint) if settings.fast_classify else None
        if heuristic is not None:
            result: dict[str, Any] = {
                "doc_type": heuristic.doc_type, "specialty": None, "languages": ["en"],
                "is_handwritten": False, "page_spans": [], "confidence": HEURISTIC_CONFIDENCE,
                "method": "heuristic", "model": "keyword-heuristic",
                "raw": {"score": heuristic.score, "runner_up": heuristic.runner_up_score},
            }  # fmt: skip
        else:
            vlm = client or get_client()
            if page1_png is None:
                page1_png = (store or get_store()).get_bytes(pages[0]["storage_key"])
            payload = vlm.vlm_json(page1_png, build_classification_prompt(hint),
                                   "classification.v1", max_tokens=400)  # fmt: skip
            doc_type = payload.get("doc_type")
            conf = float(payload.get("confidence") or 0.0)
            if is_partial(payload) or doc_type not in DOC_TYPES:
                doc_type, conf = "other", min(conf, 0.5)
            result = {
                "doc_type": doc_type, "specialty": payload.get("specialty"),
                "languages": payload.get("languages") or [],
                "is_handwritten": bool(payload.get("is_handwritten", False)),
                "page_spans": payload.get("page_spans") or [], "confidence": conf,
                "method": "vlm", "model": vlm.model, "raw": payload,
            }  # fmt: skip

        with session_scope() as conn:
            repo.insert_classification(conn, document_id=document_id, run_id=run_id, **result)
            repo.set_document_status(conn, document_id, "classified")
            repo.finish_run(conn, run_id, metrics={"doc_type": result["doc_type"],
                                                   "method": result["method"]})  # fmt: skip
            repo.write_audit(conn, actor=actor, action="update", entity="source_document",
                             entity_id=document_id,
                             detail={"stage": "classify", "doc_type": result["doc_type"],
                                     "method": result["method"]})  # fmt: skip
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            repo.set_document_status(conn, document_id, "error",
                                     error={"stage": "classify", "message": str(exc)})  # fmt: skip
        log.error("classify_failed", document_id=str(document_id), error=str(exc))
        raise
    log.info("classified", document_id=str(document_id), doc_type=result["doc_type"],
             method=result["method"], confidence=result["confidence"])  # fmt: skip
    return result
