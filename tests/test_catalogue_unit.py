"""The medicine catalogue layer's name handling and scoring (no database)."""

from __future__ import annotations

import pytest

from cdi_adapter.catalogue.normalize import (
    best_brand,
    composition_strengths,
    dosage_form,
    edit_distance,
    ingredients,
    query_keys,
    score,
    split_name,
    strength_agrees,
)


@pytest.mark.parametrize(
    ("written", "brand", "strengths"),
    [
        ("Tab. Montina-FX", "montina fx", ()),
        ("Montina-FX Tablet", "montina fx", ()),
        ("Dolo 650 Tablet", "dolo", (650.0,)),
        ("Tab. PCM - 650", "pcm", (650.0,)),
        ("Antiflu 75mg Capsule", "antiflu", (75.0,)),
        ("Syp. Crocin 120 mg/5 ml", "crocin", (120.0, 5.0)),
        ("Glycomet 500 SR Tablet", "glycomet sr", (500.0,)),  # a release form stays in the brand
        # ... but a qualifier AFTER the dosage form qualifies the form (18k 1mg names do this)
        ("Sompraz D 40 Capsule SR", "sompraz d", (40.0,)),
        ("Glycomet-GP 1 Tablet PR", "glycomet gp", (1.0,)),
        ("Uprise-D3 60K Capsule", "uprise d3", (60000.0,)),  # 60K = 60 000 IU; D3 is a name
        ("Cap Uprise D2 (60,000U)", "uprise d2", (60000.0,)),
        ("Probact XL12s", "probact xl12s", ()),
        ("Cap. Aueflu (#5)", "aueflu", ()),  # "#5": a quantity to dispense
        ("", "", ()),
    ],
)
def test_names_split_into_brand_words_and_strengths(written, brand, strengths):
    assert split_name(written) == (brand, strengths)


def test_ocr_and_handwriting_misreads_score_high_only_when_close():
    assert score("montina fx", "montina fx") == 1.0
    assert score("rnontina fx", "montina fx") == 0.97  # rn read for m
    assert score("zincolit", "zincovit") >= 0.85  # one wrong letter in eight
    assert score("aueflu", "antiflu") < 0.85  # too far to trust: UNKNOWN, a candidate only
    assert score("telma", "telma h") < 0.85  # a combination product is a different medicine
    assert edit_distance("zincolit", "zincovit") == 1
    assert score("uprise d2", "uprise d3") == 0.0  # a different number: another product


def test_short_brands_are_matched_exactly_or_not_at_all():
    """Look-alike short names (Dolo / Dola) are where one misread letter is another medicine."""
    assert score("dolo", "dola") == 0.0 and score("dolo", "dolo") == 1.0


def test_a_brand_is_accepted_only_with_a_lead_over_every_other_brand():
    brand, top, runner = best_brand([("zincovit", 0.88), ("zincovit", 0.8), ("zinconia", 0.86)])
    assert (brand, top, runner) == ("zincovit", 0.88, 0.86)  # a 0.02 lead: not reliable


def test_strength_agreement_is_unknown_when_either_side_is_silent():
    assert strength_agrees((650.0,), (650.0,)) is True
    assert strength_agrees((500.0,), (650.0,)) is False
    assert strength_agrees((), (650.0,)) is None and strength_agrees((650.0,), ()) is None


def test_written_names_are_also_tried_without_a_prescription_marker():
    assert query_keys("Ad. Sompraz (40)") == [("ad sompraz", (40.0,)), ("sompraz", (40.0,))]
    assert query_keys("Rx Dolo 650")[1] == ("dolo", (650.0,))
    assert query_keys("Adalat 10") == [("adalat", (10.0,))]  # a brand that begins with "Ad"
    assert query_keys("CapUprise D2") == [("uprise d2", ())]  # the form fused to the name
    assert query_keys("Captopril 25") == [("captopril", (25.0,))]  # ... but not a real name


def test_a_composition_lists_its_generics():
    assert ingredients("Amoxycillin (500mg) + Clavulanic Acid (125mg)") == [
        ("amoxycillin", "Amoxycillin"),
        ("clavulanic acid", "Clavulanic Acid"),
    ]
    assert ingredients("Vitamin D3 (60000IU)") == [("vitamin d3", "Vitamin D3")]
    assert ingredients(None) == []


def test_the_dosage_form_a_name_states():
    assert dosage_form("Cap. Sonpraz") == "capsule" and dosage_form("CapUprise D3") == "capsule"
    assert dosage_form("Sompraz D 40 Capsule SR") == "capsule"
    assert dosage_form("Tab. Dolo 650") == "tablet" and dosage_form("Syp. Crocin") == "syrup"
    assert dosage_form("Montina FX") is None and dosage_form(None) is None


def test_a_composition_states_each_ingredients_strength():
    assert composition_strengths("Sulfasalazine (500mg)") == (500.0,)
    assert composition_strengths("Vitamin D3 (60000IU)") == (60000.0,)
    assert composition_strengths("Montelukast (10mg) + Fexofenadine (120mg)") == (10.0, 120.0)
    assert composition_strengths("Amlodipine (NA)") == () and composition_strengths(None) == ()


@pytest.mark.parametrize(
    ("written", "form"),
    [
        ("T Dytor 10", "tablet"), ("T. Dolo 650", "tablet"), ("Tab Pan 40", "tablet"),
        ("Tabs Crocin", "tablet"), ("C Pan-D", "capsule"), ("Cap Sompraz D", "capsule"),
        ("Syp Crocin", "syrup"), ("Syr Ascoril", "syrup"), ("Inj Monocef", "injection"),
        ("Susp Augmentin", "suspension"), ("Cr Candid", "cream"), ("Crm Candid B", "cream"),
        ("Oint Soframycin", "ointment"), ("Gtt Moxicip", "drops"), ("Lot Calamine", "lotion"),
        ("Sol Betadine", "solution"), ("Resp Budecort", "respules"),
        ("Moxicip Infusion", "infusion"), ("Betadine 2% Gargle", "gargle"),
        # a single letter is a form only as the FIRST word; CR after a name is controlled release
        ("Vitamin C 500", None), ("Neurobion Forte T", None), ("Glycomet GP 1 CR", None),
    ],
)  # fmt: skip
def test_every_dosage_form_abbreviation_maps_to_the_catalogue_form(written, form):
    """Owner, 2026-09-28: T / Tab / Tabs -> Tablet, C / Cap -> Capsule, Syp -> Syrup, Inj ->
    Injection, Susp -> Suspension, Cr -> Cream, Oint -> Ointment ...: one table, every medicine."""
    assert dosage_form(written) == form


def test_a_leading_abbreviation_is_read_as_the_form_not_the_name():
    assert query_keys("T Dytor 10") == [("dytor", (10.0,))]
    assert query_keys("C Pan-D") == [("pan d", ())]
    assert query_keys("Vitamin C 500") == [("vitamin c", (500.0,))]  # "C" is part of the name
