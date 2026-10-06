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

# how every medication line is read, on any document that lists medicines
MEDICATION_RULES = (
    "A medication is a named drug (Tab / Cap / Syp / Inj / a brand or generic name); drug_name "
    'is the medicine\'s name as written (the form word may lead it: "Cap Clopilet"), never '
    "the form word alone. Keep its strength, dose, dose-slot pattern, route and duration exactly "
    "as written, and leave any of them empty when the line does not state it - never fill in a "
    "typical value such as 1-0-1 or PO. Instructions that are not a drug (gargles, steam "
    "inhalation, fluids / ORS, rest, diet, follow-up) are advice, not medications."
)

# the tests a prescription advises (owner request 2026-09-28): read into investigations_advised
TESTS_ADVISED_RULES = (
    " Tests the doctor advises or orders (under Investigations / Ix / Tests advised, or a test "
    'written under Adv / Advice, e.g. "Adv: CBC, LFT, HbA1c, X-ray chest") go in '
    "investigations_advised, one entry per test, exactly as written; a test written with its "
    "result is a result, not advised. Never list a test the page does not name."
)

GUIDANCE = {
    "prescription": "List EVERY medication line - usually 5-15 drugs - do not stop early. "
    + MEDICATION_RULES
    + TESTS_ADVISED_RULES,
    "lab_report": "List EVERY test row with its result, unit and reference range.",
    "vitals_sheet": "List every vital sign that has a number. Record allergies if written.",
    "radiology": "Copy EVERY finding line into findings_list; put the impression separately.",
    "discharge_summary": "Capture final diagnoses, procedures, discharge medications and advice. "
    + MEDICATION_RULES,
    "operative_note": "Capture the procedure(s) performed, findings, diagnoses and advice.",
    "opd_note": "Capture complaints, examination findings, diagnoses, vitals, and EVERY "
    "medication line (the Rx section). " + MEDICATION_RULES + TESTS_ADVISED_RULES,
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
        "page, and never copy an example from the schema as a value; for every item give "
        '`evidence` as the list of block ids (e.g. ["b3"]) it came from and a `confidence` '
        "between 0 and 1 that is lower where the writing is hard to read. The OCR text can be "
        "wrong, above all on handwriting: read the words from the image and spell them as "
        "written there, not as the OCR garbled them. Omit anything you cannot read.\n"
        "OCR blocks of the page, in reading order:\n"
        "<document>\n" + "\n".join(lines) + "\n</document>"
    )
    return prompt, block_id_map
