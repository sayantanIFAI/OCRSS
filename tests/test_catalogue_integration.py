"""The medicine catalogue layer on real PostgreSQL (``pg_trgm``): import a synthetic catalogue in
the 1mg export layout, identify written names against it, and identify a document's
medications in S5.

The test database is shared across tests, so every test here removes the catalogue it loaded:
another module's S5 metrics must not see it."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from cdi_adapter.catalogue import service as cat
from cdi_adapter.db import session_scope

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).parent / "data" / "catalogue_sample_synthetic.csv"


@pytest.fixture
def catalogue(db_env):
    with session_scope() as c:
        info = cat.import_csv(c, SAMPLE, source="test-1mg", actor="pytest")
    yield info
    with session_scope() as c:
        c.execute(text("DELETE FROM medication_catalogue_match"))
        c.execute(text("DELETE FROM med_catalogue"))
        c.execute(text("DELETE FROM med_catalogue_import"))


def _identify(written, strengths=(), form=None):
    with session_scope() as c:
        return cat.identify(c, written, strengths, form)


def test_import_loads_every_product_with_its_provenance(catalogue):
    assert catalogue["rows"] == 19 and len(catalogue["sha256"]) == 64
    with session_scope() as c:
        assert cat.catalogue_size(c) == 19
        assert cat.latest_import(c)["file_name"] == SAMPLE.name
        row = c.execute(text("SELECT brand_norm, strengths, composition, discontinued FROM "
                             "med_catalogue WHERE source_id = '3'")).mappings().one()  # fmt: skip
    # a name without a strength takes its composition's: Montelukast 10 mg + Fexofenadine 120 mg
    assert row["brand_norm"] == "montina fx" and list(row["strengths"]) == [10, 120]
    assert row["composition"] == "Montelukast (10mg) + Fexofenadine (120mg)"
    # a reload replaces the source's rows, never duplicates them
    with session_scope() as c:
        assert cat.import_csv(c, SAMPLE, source="test-1mg")["rows"] == 19
        assert cat.catalogue_size(c) == 19


def test_written_names_are_identified_through_spelling_variation(catalogue):
    m = _identify("Tab. Montina fx")
    assert (m.status, m.display_name, m.method) == ("matched", "Montina-FX Tablet", "exact")
    assert m.product["manufacturer"] == "Test Pharma B"
    m = _identify("Tab. zincolit")  # the handwriting reading of Zincovit
    assert (m.status, m.display_name, m.method) == ("matched", "Zincovit Tablet", "fuzzy")
    assert m.score >= 0.85
    assert _identify("Tab. rnontina FX").display_name == "Montina-FX Tablet"  # rn read for m


def test_the_stated_strength_picks_the_pack(catalogue):
    assert _identify("Tab. Dolo 650").display_name == "Dolo 650 Tablet"
    m = _identify("Dolo", (500.0,))
    assert (m.display_name, m.strength_match) == ("Dolo 500 Tablet", True)
    m = _identify("Glycomet 500")  # only a discontinued pack under this exact brand
    assert m.display_name == "Glycomet 500 Tablet" and m.product["discontinued"] is True


def test_generic_names_are_identified_as_the_generic(catalogue):
    m = _identify("Tab. Amlodipine 5")
    assert (m.status, m.display_name, m.catalogue_id, m.method) == (
        "matched",
        "Amlodipine (generic)",
        None,
        "generic",
    )  # not "S Amlodipine Tablet"
    m = _identify("Metfornin 500")  # a misread generic
    assert (m.display_name, m.method) == ("Metformin (generic)", "generic_fuzzy")
    # a generic in brackets beside a brand the catalogue does not have
    assert _identify("Ad. Nofza (Telmisartan)").display_name == "Telmisartan (generic)"
    assert _identify("Cap. Aueflu (after meals)").status == "unknown"  # brackets: generics only


def test_how_prescriptions_write_names_is_read_through(catalogue):
    m = _identify("Ad. Sompraz (40)")  # a prescription marker read into the name
    assert (m.status, m.display_name) == ("matched", "Sompraz 40 Tablet")
    m = _identify("CapUprise D3 (60,000U)")  # the form fused to the name; thousands separator
    assert (m.status, m.display_name, m.strength_match) == (
        "matched",
        "Uprise-D3 60K Capsule",
        True,
    )
    assert _identify("CapUprise D2 (60,000U)").status == "unknown"  # D2 is not D3
    assert _identify("T").status == "unknown"  # a lone form letter, not "T-98 Tablet"


def test_the_written_dosage_form_must_agree_with_a_fuzzy_match(catalogue):
    """Owner's handwritten test Rx (2026-09-26): "Cap. Sompraz D" read as "Cap. Sonpraz" must not
    become the Sompraz tablet; the capsule is offered first for the reviewer instead."""
    m = _identify("Cap. Sonpraz")
    assert (m.status, m.display_name) == ("unknown", cat.UNKNOWN)
    assert m.candidates[0]["name"] == "Somprazd 40mg Capsule"
    assert _identify("Tab. Sonpraz").display_name == "Sompraz 40 Tablet"  # the form agrees
    # the form written on the line counts even when the extracted drug text dropped it
    assert _identify("Sonpraz", form="capsule").status == "unknown"


def test_the_closest_products_use_the_written_strength(catalogue):
    """Owner's test Rx (2026-09-26): "Tab. Thyronorm (62.5 mcg)" read as "Muronorm". Too far to
    accept, but the product of that strength is offered first, not the nearer-spelled
    Meronorm injection."""
    m = _identify("Muronorm", (62.5,))
    assert m.status == "unknown" and m.candidates[0]["name"] == "Thyronorm 62.5mcg Tablet"
    m = _identify("Cap. Aueflu (#5)")  # "#5" is a quantity, not a strength
    assert m.status == "unknown" and m.candidates[0]["name"] == "Antiflu 75mg Capsule"


def test_no_reliable_match_is_unknown_medication_never_a_guess(catalogue):
    m = _identify("Tab. PCM - 650")  # an abbreviation, not a catalogue brand
    assert (m.status, m.display_name, m.catalogue_id) == ("unknown", cat.UNKNOWN, None)
    m = _identify("Cap. Aueflu (#5)")  # too far from Antiflu to trust
    assert m.status == "unknown" and "Antiflu 75mg Capsule" in [c["name"] for c in m.candidates]
    assert _identify("Dola 650").status == "unknown"  # a short look-alike brand: exact only
    # plain Telma is its own product: never the Telma H combination
    assert _identify("Telma").display_name == "Telma 40 Tablet"
    assert _identify("Tab. Telma H").display_name == "Telma H Tablet"
    assert _identify("").status == "unknown"


def test_a_documents_medications_are_identified_in_s5(catalogue):
    from cdi_adapter.terminology.service import bind_document
    from tests.test_extract_integration import _doc_with_ocr, _write
    from tests.test_validate_integration import _rx

    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    metrics = bind_document(doc)
    assert metrics["catalogue_matched"] + metrics["catalogue_unknown"] == 3  # three Rx lines
    with session_scope() as c:
        from cdi_adapter import repo

        meds = [f for f in repo.get_facts_for_document(c, doc) if f["fact_type"] == "medication"]
        found = cat.matches_for_facts(c, [str(f["id"]) for f in meds])
    by_text = {f["local_text"]: found[str(f["id"])] for f in meds}
    # written by generic name: identified as the generic, not one maker's "Metformin 500mg Tablet"
    metformin = next(v for k, v in by_text.items() if "metformin" in k.lower())
    assert (metformin["status"], metformin["display_name"], metformin["method"]) == (
        "matched",
        "Metformin (generic)",
        "generic",
    )
    amlodipine = next(v for k, v in by_text.items() if "amlodipine" in k.lower())
    assert amlodipine["display_name"] == "Amlodipine (generic)"  # never "S Amlodipine Tablet"
    atorvastatin = next(v for k, v in by_text.items() if "atorvastatin" in k.lower())
    assert (atorvastatin["status"], atorvastatin["display_name"]) == ("unknown", cat.UNKNOWN)
    # the extracted text is untouched: the catalogue sits beside the fact
    assert any("Metformin" in f["local_text"] for f in meds)


def test_an_empty_catalogue_changes_nothing(db_env):
    from cdi_adapter.terminology.service import bind_document
    from tests.test_extract_integration import _doc_with_ocr, _write
    from tests.test_validate_integration import _rx

    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    metrics = bind_document(doc)
    assert "catalogue_matched" not in metrics and "catalogue_unknown" not in metrics


def test_an_exact_brand_of_another_form_is_not_that_product(catalogue):
    """ "Syp Dolo" is not the Dolo tablet: the brand exists only as tablets, so the line is
    UNKNOWN_MEDICATION rather than a product of the wrong form."""
    assert _identify("Tab Dolo 650").display_name == "Dolo 650 Tablet"
    assert _identify("T Dolo 650").display_name == "Dolo 650 Tablet"  # T = tablet
    m = _identify("Syp Dolo 650")
    assert (m.status, m.display_name) == ("unknown", cat.UNKNOWN)
