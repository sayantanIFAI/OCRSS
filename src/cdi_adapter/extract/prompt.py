"""S4 extraction prompts (ARCHITECTURE §5 S4).

``build_extraction_prompt(doc_type, ocr_blocks)`` presents blocks as ``[b1] text``, ``[b2] ...``
inside ``<document>``; ``block_id_map`` maps ``bN -> ocr_block uuid``. Per-doc-type token
budgets and guidance keep long, list-heavy documents from being cut short.
"""

from __future__ import annotations

from typing import Any

SCHEMA_FOR_DOC_TYPE = {
    "prescription": "prescription.v3",
    "lab_report": "lab_report.v3",
    "vitals_sheet": "vitals.v3",
    "opd_note": "opd_note.v3",
    "referral": "opd_note.v3",
    "discharge_summary": "discharge_summary.v3",
    "operative_note": "discharge_summary.v3",
    "radiology": "radiology.v3",
}

MAX_TOKENS = {
    "prescription": 2600,
    "discharge_summary": 2600,
    "operative_note": 2600,
    "opd_note": 2400,
    "referral": 2400,
    "lab_report": 2000,
    "radiology": 2000,
    "vitals_sheet": 1200,
}

GUIDANCE = {
    "prescription": "List EVERY medication line - usually 5-15 drugs - do not stop early. "
    "Keep strength, dose, the dose-slot pattern (e.g. 1-0-1), route and duration as written.",
    "lab_report": "List EVERY test row with its result, unit and reference range.",
    "vitals_sheet": "List every vital sign that has a number. Record allergies if written.",
    "radiology": "Copy EVERY finding line into findings_list; put the impression separately.",
    "discharge_summary": "Capture final diagnoses, procedures, discharge medications and advice.",
    "operative_note": "Capture the procedure(s) performed, findings, diagnoses and advice.",
    "opd_note": "Capture complaints, examination findings, diagnoses, vitals, medications.",
    "referral": "Capture the reason for referral, diagnoses and whom the patient is referred to.",
}


def max_tokens_for(doc_type: str) -> int:
    return MAX_TOKENS.get(doc_type, 1600)


def build_extraction_prompt(
    doc_type: str, ocr_blocks: list[dict[str, Any]]
) -> tuple[str, dict[str, str]]:
    block_id_map: dict[str, str] = {}
    lines = []
    for i, b in enumerate(ocr_blocks, start=1):
        bid = f"b{i}"
        block_id_map[bid] = str(b["id"])
        lines.append(f"[{bid}] {b['text']}")
    schema = SCHEMA_FOR_DOC_TYPE[doc_type]
    prompt = (
        f"Extract the clinical content of this {doc_type.replace('_', ' ')} into JSON matching "
        f"the schema {schema}.\n"
        f"{GUIDANCE.get(doc_type, '')}\n"
        "Rules: copy values exactly as written; never infer or guess a value that is not on the "
        'page; for every item give `evidence` as the list of block ids (e.g. ["b3"]) it came '
        "from and a `confidence` between 0 and 1. Omit anything you cannot read.\n"
        "OCR blocks of the page, in reading order:\n"
        "<document>\n" + "\n".join(lines) + "\n</document>"
    )
    return prompt, block_id_map
