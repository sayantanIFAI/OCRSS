"""The Reviewer groups an item's facts into clinical sections (owner, 2026-09-26: "the extracted
data should be grouped properly"): the sections, their order and their columns are fixed here."""

from __future__ import annotations

import re

from cdi_adapter.webapp.reviewer_page import reviewer_page


def test_facts_are_grouped_into_clinical_sections_in_reading_order() -> None:
    html = reviewer_page()
    keys = re.findall(r"\['([a-z_]+)', '[^']+'\]", html.split("const SECTIONS = ")[1].split(";")[0])
    assert keys[:7] == ["condition", "symptom", "finding", "vital_sign", "lab_result",
                        "diagnostic_report", "medication"]  # fmt: skip
    assert keys[-2:] == ["advice", "document"]  # advice last, then "Other"
    medication_columns = ("Strength", "Dose", "Frequency", "Duration", "Route", "Instructions")
    for column in (f"'{c}'" for c in medication_columns):
        assert column in html  # a medication's own columns
    assert "known.has(e.element_type) ? e.element_type : 'document'" in html  # never dropped
    assert "cdn" not in html.lower()


def test_the_uploaded_document_is_shown_beside_its_facts() -> None:
    html = reviewer_page()
    assert "class=rv-split" in html and "class=rv-doc" in html  # facts left, document right
    assert "/api/documents/${encodeURIComponent(viewer.id)}/pages/${viewer.page}" in html
    assert "/api/documents/${esc(viewer.id)}/original" in html  # the file exactly as uploaded
    assert "position:sticky" in html  # stays in view while the facts scroll


def test_medications_show_the_catalogue_identification_beside_the_written_name() -> None:
    html = reviewer_page()
    assert "'Catalogue (Tata 1mg)'" in html and "catalogueText(e.catalogue)" in html
    assert "'UNKNOWN_MEDICATION'" in html  # never a guessed product


def test_medications_show_the_agents_suggestion_which_the_reviewer_applies() -> None:
    """Backlog E12-S4: advisory only. The suggestion pre-fills the Edit form; saving it is the
    reviewer's correction (never applied automatically)."""
    html = reviewer_page()
    assert "'Agent (diagnosis check)'" in html and "agentText(e.agent)" in html
    assert "no matching diagnosis" in html  # the fit check against the prescription's diagnoses
    assert "Use suggestion" in html and "function useAgent" in html
    assert "editFact(fid, item)" in html  # opens the normal Edit form, pre-filled
    assert "/api/reviewer/items/${id}/agent" in html and "Run agent again" in html
