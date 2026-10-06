"""E11-S4 (unit, no models): recognizers, merging, replacement, image redaction, red-team logic."""

from __future__ import annotations

import io
from datetime import date

import pytest
from PIL import Image

pytest.importorskip("presidio_analyzer", reason="pip install -e .[deid]")

from cdi_adapter.deid import document as D  # noqa: E402
from cdi_adapter.deid import engine as E  # noqa: E402
from cdi_adapter.deid import redteam as RT  # noqa: E402
from cdi_adapter.deid.recognizers import india_recognizers, verhoeff_ok  # noqa: E402


def _hits(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in india_recognizers():
        for res in r.analyze(text, entities=r.supported_entities, nlp_artifacts=None):
            if res.score >= 0.35:
                out.setdefault(res.entity_type, []).append(text[res.start : res.end])
    return out


# ---------------------------------------------------------------- recognizers


def test_aadhaar_needs_a_valid_verhoeff_check_digit() -> None:
    assert verhoeff_ok("234123412346") and not verhoeff_ok("234123412345")
    assert _hits("Aadhaar: 2341 2341 2346").get("IN_AADHAAR") == ["2341 2341 2346"]
    assert "IN_AADHAAR" not in _hits("Invoice total 234123412345")  # fails the checksum
    assert "IN_AADHAAR" not in _hits("Ref 134123412346")  # Aadhaar never starts with 0/1


def test_abha_number_and_address() -> None:
    h = _hits("ABHA No: 91-1234-5678-9012  ABHA Address: anjali.das@abdm")
    assert h["IN_ABHA_NUMBER"] == ["91-1234-5678-9012"]
    assert h["IN_ABHA_ADDRESS"] == ["anjali.das@abdm"]


def test_pin_code_only_when_labelled() -> None:
    assert _hits("Address: 12 Lake Road, Kolkata PIN: 700045")["IN_PIN_CODE"] == ["700045"]
    assert "IN_PIN_CODE" not in _hits("Platelet count 700045 /uL")


def test_record_numbers_take_the_value_not_the_label() -> None:
    h = _hits("UHID: UH482913   IP No. 2026/7781   MRN#AB-99812   MPI: CFP-2026-000123")
    assert set(h["MEDICAL_RECORD_NUMBER"]) >= {"UH482913", "2026/7781", "AB-99812",
                                               "CFP-2026-000123"}  # fmt: skip
    assert not any("UHID" in v or "MRN" in v for v in h["MEDICAL_RECORD_NUMBER"])


def test_pan_phone_email_and_indian_dates() -> None:
    h = _hits("PAN: ABCPD1234F  Mobile: +91 98300 12345  mail: a.das@example.in  "
              "DOB 14/03/1978, seen 5th Aug 2026 and 2026-08-12")  # fmt: skip
    assert h["IN_PAN"] == ["ABCPD1234F"]
    assert any("98300" in p for p in h["PHONE_NUMBER"])
    assert h["EMAIL_ADDRESS"] == ["a.das@example.in"]
    assert {"14/03/1978", "5th Aug 2026", "2026-08-12"} <= set(h["DATE_TIME"])


# ---------------------------------------------------------------- merging + replacement


def test_overlapping_spans_merge_and_the_most_specific_type_wins() -> None:
    spans = [E.Span("PERSON", 0, 10, 0.9), E.Span("IN_ABHA_NUMBER", 5, 22, 0.6),
             E.Span("DATE_TIME", 30, 40, 0.6), E.Span("LOCATION", 38, 45, 0.5)]  # fmt: skip
    merged = E.merge(spans)
    assert [(s.entity, s.start, s.end) for s in merged] == [
        ("IN_ABHA_NUMBER", 0, 22), ("DATE_TIME", 30, 45)]  # fmt: skip


def test_dates_shift_consistently_and_keep_their_format() -> None:
    assert E.shift_date("14/03/1978", 10) == "24/03/1978"
    assert E.shift_date("2026-08-12", -12) == "2026-07-31"
    assert E.shift_date("5th Aug 2026", 1) == "06 Aug 2026"
    assert E.shift_date("next Tuesday", 3) is None
    key = E.DocumentKey(shift_days=30)
    text = "Admitted 01/08/2026, discharged 04/08/2026"
    spans = [E.Span("DATE_TIME", 9, 19, 1), E.Span("DATE_TIME", 32, 42, 1)]
    assert E.replace(text, spans, key) == "Admitted 31/08/2026, discharged 03/09/2026"  # 3 days


def test_surrogates_are_consistent_inside_a_document_and_differ_across() -> None:
    k1, k2 = E.DocumentKey(), E.DocumentKey()
    s = E.Span("PERSON", 0, 10, 1)
    assert E.surrogate(s, "Anjali Das", k1) == E.surrogate(s, "anjali das", k1)
    many = {E.surrogate(s, "Anjali Das", E.DocumentKey()) for _ in range(40)}
    assert len(many) > 5  # not linkable across documents by the surrogate
    assert "Anjali" not in E.surrogate(s, "Anjali Das", k2)
    assert E.surrogate(E.Span("AGE", 0, 2, 1), "94", k1) == "90+"
    assert E.surrogate(E.Span("AGE", 0, 2, 1), "48", k1) == "48"
    assert E.surrogate(E.Span("IN_AADHAAR", 0, 1, 1), "2341 2341 2346", k1) == "[AADHAAR]"


# ---------------------------------------------------------------- document: image + payload


def _page() -> D.Page:
    im = Image.new("RGB", (400, 120), "white")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    lines = [D.OcrLine("Name: Anjali Das", [10, 10, 200, 30]),
             D.OcrLine("Diagnosis: Type 2 diabetes", [10, 50, 300, 70]),
             D.OcrLine("Phone 9830012345", [10, 90, 220, 110])]  # fmt: skip
    return D.Page(buf.getvalue(), lines)


def test_only_lines_holding_identifiers_are_blacked_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_detect(text: str) -> list[E.Span]:
        out = []
        for needle, kind in (("Anjali Das", "PERSON"), ("9830012345", "PHONE_NUMBER")):
            i = text.find(needle)
            if i >= 0:
                out.append(E.Span(kind, i, i + len(needle), 1.0))
        return out

    monkeypatch.setattr(D, "detect", fake_detect)
    counts: dict[str, int] = {}
    png, text, n = D.redact_page(_page(), E.DocumentKey(), counts)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert n == 2 and counts == {"PERSON": 1, "PHONE_NUMBER": 1}
    assert im.getpixel((100, 20)) == (0, 0, 0) and im.getpixel((100, 100)) == (0, 0, 0)
    assert im.getpixel((100, 60)) == (255, 255, 255)  # the clinical line stays readable
    assert "Anjali" not in text and "9830012345" not in text and "diabetes" in text


def test_identity_fields_are_replaced_even_when_undetected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(D, "detect", lambda text: [])  # a detector that misses everything
    counts: dict[str, int] = {}
    key = E.DocumentKey(shift_days=30)
    out = D.deidentify_value({"patient": {"name": "Anjali Das", "dob": "14/03/1978",
                                          "abha_number": "91-1234-5678-9012",
                                          "address": "12 Lake Road"},
                              "facts": [{"text": "HbA1c 7.8"}]}, key, counts)  # fmt: skip
    p = out["patient"]
    assert "Anjali" not in p["name"] and p["dob"] == "13/04/1978"
    assert p["abha_number"] == "[REDACTED]" and p["address"] == "[REDACTED]"
    assert out["facts"] == [{"text": "HbA1c 7.8"}]


# ---------------------------------------------------------------- red team


TRUTH = {"name": "Meenakshi Subramaniam", "dob": date(1978, 3, 14),
         "abha": "91-1234-5678-9012", "aadhaar": "234123412346", "email": "m.s@example.in",
         "uhid": "UH482913", "phone": "+91 98300 12345"}  # fmt: skip


def test_red_team_finds_every_kind_of_trace() -> None:
    leak = RT.attempt(TRUTH, {"t": ["name MEENAKSHI s", "dob 14-03-1978", "id 9112345678 9012",
                                    "aad 2341-2341-2346", "mail M.S@EXAMPLE.IN", "UH482913",
                                    "call 9830012345", "Subramanian"]})  # fmt: skip
    found = {(x.identifier, x.how) for x in leak}
    assert ("name", "name") in found and ("dob", "dob") in found
    assert {("abha", "digits"), ("aadhaar", "digits"), ("email", "text"),
            ("phone", "digits")} <= found  # fmt: skip
    one_typo = RT.attempt({"name": "Subramaniam"}, {"t": ["Subramanian"]})
    assert [x.how for x in one_typo] == ["name~1"]  # an OCR-style typo is still a leak


def test_red_team_is_quiet_on_clean_output() -> None:
    clean = [
        "Name: Uday Hegde   DOB: 13/04/1978   ABHA No: [ABHA]",
        "Mobile: [PHONE]  Email: [EMAIL]  UHID: [ID]",
        "Diagnosis: Dengue fever",
    ]
    assert RT.attempt(TRUTH, {"ocr_text": clean}) == []


def test_identifiers_glued_to_the_next_word_by_ocr_are_still_found() -> None:
    # what RapidOCR really printed on the red-team sample (the space after the value is lost)
    h = _hits("DOB: 01/09/1987Age/Sex: 38 Y   Mobile:+919105446581E   Aadhaar:234123412346PAN")
    assert "01/09/1987" in h["DATE_TIME"]
    assert any("9105446581" in p for p in h["PHONE_NUMBER"])
    assert h["IN_AADHAAR"] == ["234123412346"]


def test_durations_and_prescription_abbreviations_are_not_identifiers() -> None:
    text = "fever for 3 days since 12/08/2026, Tab Paracetamol 650 mg SOS, seen 5 Aug 2026"

    def fp(value: str, entity: str) -> bool:
        i = text.index(value)
        return E.clinical_false_positive(text, E.Span(entity, i, i + len(value), 1.0))

    assert fp("3 days", "DATE_TIME") and fp("SOS", "ORGANIZATION")
    assert not fp("12/08/2026", "DATE_TIME") and not fp("5 Aug 2026", "DATE_TIME")


def test_model_hits_on_form_labels_are_trimmed_to_the_value() -> None:
    text = "ABHA No: 91-4821-7730-1956   UHID: UH482913"
    assert E.tidy(text, E.Span("ORGANIZATION", 0, 2, 0.7)) is None  # "AB" of "ABHA" only
    i = text.index("UHID")
    s = E.tidy(text, E.Span("MEDICAL_RECORD_NUMBER", i, len(text), 0.8))  # "UHID: UH482913"
    assert s is not None and text[s.start : s.end] == "UH482913"
    s = E.tidy("Review at CareFlow Polyclinic.", E.Span("ORGANIZATION", 10, 30, 0.9))
    assert s is not None and s.end == 29  # the full stop stays


def test_address_words_are_matched_exactly_names_with_one_typo() -> None:
    assert RT.attempt({"address": "143, Banjara Hills, Hyderabad"},
                      {"t": ["fever with chills"]}) == []  # fmt: skip
    assert [x.how for x in RT.attempt({"address": "Banjara Hills"}, {"t": ["banjara"]})] == ["name"]


# ---------------------------------------------------------------- fixes found by the red team


def test_an_id_split_by_ocr_is_removed_whole() -> None:
    text = "Aadhaar:48031611.6055:PAN"
    s = E.extend_digits(text, E.Span("MEDICAL_RECORD_NUMBER", 8, 16, 0.8, "clinical_deid_ner"))
    assert text[s.start : s.end] == "48031611.6055"
    other = E.extend_digits(text, E.Span("PERSON", 8, 16, 0.8))  # only id types grow
    assert (other.start, other.end) == (8, 16)


def test_a_date_inside_an_age_hit_is_never_kept() -> None:
    glued = "13/07/1974Age/Sex:52Y / M"
    merged = E.merge([E.Span("AGE", 0, len(glued), 0.9), E.Span("DATE_TIME", 0, 10, 0.6)])
    assert [s.entity for s in merged] == ["DATE_TIME"]  # the date type wins the merge
    key = E.DocumentKey()
    assert E.surrogate(E.Span("AGE", 0, len(glued), 0.9), glued, key) == "[AGE]"
    for pure in ("52", "52 Y", "65 yrs", "29.Y"):
        assert E.surrogate(E.Span("AGE", 0, len(pure), 1), pure, key) == pure


def test_lab_values_are_not_phones_or_record_numbers() -> None:
    text = "Platelet count 88000/uL 150000-410000 L   Mobile: +91 9647627589"

    def fp(value: str, entity: str, source: str = "") -> bool:
        i = text.index(value)
        return E.clinical_false_positive(text, E.Span(entity, i, i + len(value), 0.9, source))

    assert fp("150000-41", "PHONE_NUMBER") and fp("88000", "MEDICAL_RECORD_NUMBER",
                                                 "clinical_deid_ner")  # fmt: skip
    assert not fp("+91 9647627589", "PHONE_NUMBER")
    assert not fp("88000", "MEDICAL_RECORD_NUMBER", "")  # a pattern hit (labelled) stays


def test_the_whole_address_value_and_a_glued_pin_are_found() -> None:
    h = _hits("Address: 165, Jayanagar 4th Block, Hyderabad PIN: 595816\n"
              "Address:57, Banjara Hills, LucknowPIN: 465007")  # fmt: skip
    assert "165, Jayanagar 4th Block, Hyderabad" in h["LOCATION"]
    assert "57, Banjara Hills, Lucknow" in h["LOCATION"]
    assert set(h["IN_PIN_CODE"]) == {"595816", "465007"}


def test_the_redacted_image_is_read_again_until_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line the first OCR missed is caught by re-reading the redacted page."""
    monkeypatch.setattr(
        D,
        "detect",
        lambda text: [
            E.Span("PERSON", i, i + 10, 1.0) for i in [text.find("Anjali Das")] if i >= 0
        ],
    )
    page = _page()
    page.lines = [ln for ln in page.lines if "Anjali" not in ln.text]  # OCR missed the name
    reads: list[int] = []

    def reocr(png: bytes) -> list[D.OcrLine]:
        reads.append(1)
        im = Image.open(io.BytesIO(png)).convert("RGB")
        visible = im.getpixel((100, 20)) != (0, 0, 0)
        return [D.OcrLine("Name: Anjali Das", [10, 10, 200, 30])] if visible else []

    png, _, n = D.redact_page(page, E.DocumentKey(), {}, reocr)
    assert n == 1 and len(reads) == 2  # found on the 1st re-read; the 2nd re-read is clean
    assert Image.open(io.BytesIO(png)).convert("RGB").getpixel((100, 20)) == (0, 0, 0)


def test_red_team_digits_stay_within_one_number() -> None:
    # "…2027" + "3 days" + "650 mg" contain 027365 only if every digit on the page is joined
    text = ["Next visit: 09/09/2027 3 days, 650 mg", "Contact: [PHONE]"]
    assert RT.attempt({"phone": "+91 9224027365"}, {"t": text}) == []
    assert RT.attempt({"phone": "+91 9224027365"}, {"t": ["call 92240 27365"]}) != []


def test_an_ocr_glued_sentence_is_not_a_url() -> None:
    text = "fever with chills.for 3 days, see www.careflow.in or https://x.example"

    def fp(value: str) -> bool:
        i = text.index(value)
        return E.clinical_false_positive(text, E.Span("URL", i, i + len(value), 0.6))

    assert fp("chills.for") and not fp("www.careflow.in") and not fp("https://x.example")


def test_a_line_caught_only_on_the_image_is_removed_from_the_text_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first OCR misread the phone (so the text missed it); the image re-read caught it."""
    monkeypatch.setattr(
        D,
        "detect",
        lambda text: [
            E.Span("PHONE_NUMBER", i, i + 10, 1.0) for i in [text.find("9830012345")] if i >= 0
        ],
    )
    page = _page()
    page.lines[2] = D.OcrLine("Phone 983OO12345", [10, 90, 220, 110])  # OCR read O for 0

    def reocr(png: bytes) -> list[D.OcrLine]:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        visible = im.getpixel((100, 100)) != (0, 0, 0)
        return [D.OcrLine("Phone 9830012345", [10, 90, 220, 110])] if visible else []

    _, text, _ = D.redact_page(page, E.DocumentKey(), {}, reocr)
    assert text.splitlines()[2] == "[REDACTED]" and "983" not in text
    assert text.splitlines()[1] == "Diagnosis: Type 2 diabetes"
