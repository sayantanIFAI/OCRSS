"""S2 VLM classification prompt (fallback path, ARCHITECTURE §5 S2)."""

from __future__ import annotations

DOC_TYPES = (
    "prescription", "lab_report", "vitals_sheet", "opd_note", "referral", "discharge_summary",
    "operative_note", "radiology", "advice_slip", "administrative", "other",
)  # fmt: skip

HINT_MAX_CHARS = 4000


def build_classification_prompt(hint_text: str) -> str:
    hint = (hint_text or "").strip()[:HINT_MAX_CHARS]
    return (
        "You are classifying one scanned hospital document (the image is page 1).\n"
        "Return ONLY a JSON object with these keys:\n"
        f'- "doc_type": exactly one of {", ".join(DOC_TYPES)}\n'
        '- "specialty": the clinical specialty if evident (e.g. "cardiology"), else null\n'
        '- "is_handwritten": true if the clinical content is mainly handwritten\n'
        '- "languages": ISO 639-1 codes of the languages on the page, e.g. ["en", "hi"]\n'
        '- "page_spans": [{"doc_type", "start_page", "end_page"}] if several documents are '
        "scanned together, else []\n"
        '- "confidence": your confidence in doc_type, 0 to 1\n'
        "Rules: judge by the document's purpose, not by single words (a prescription printed "
        'by a "Cardiac Cath Laboratory" is still a prescription). If unsure, use "other" with '
        "low confidence. Do not guess.\n"
        "OCR text of the page (may be incomplete or garbled):\n"
        f"<document>\n{hint}\n</document>"
    )
