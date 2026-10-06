"""E0-S7 unit: date parsing, name/sex/age parsing, candidate extraction, match rules."""

from __future__ import annotations

from datetime import date

import pytest

from cdi_adapter.dates import parse_date
from cdi_adapter.mpi import service as mpi

REF = date(2026, 9, 21)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026", (date(2026, 1, 1), "year")),
        ("2026-08", (date(2026, 8, 1), "month")),
        ("2026-08-12", (date(2026, 8, 12), "day")),
        ("12-Aug-2026", (date(2026, 8, 12), "day")),
        ("12 August 2026", (date(2026, 8, 12), "day")),
        ("03/04/2026", (date(2026, 4, 3), "day")),  # day-first
        ("03.04.26", (date(2026, 4, 3), "day")),
        ("Aug 2026", (date(2026, 8, 1), "month")),
        ("1978-01-01T00:00:00", (date(1978, 1, 1), "day")),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", [None, "", "yesterday", "31/02/2026", "13-Foo-2026"])
def test_parse_date_rejects(text):
    assert parse_date(text) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dr. Ramesh Kumar", "Ramesh Kumar"),
        ("MR. anjali  das", "Anjali Das"),
        ("Smt Priya Nair W/o Suresh Nair", "Priya Nair"),
        ("Master Arjun S/o Vikram", "Arjun"),
        ("Ramesh Kumar (55/M)", "Ramesh Kumar"),
        ("", None),
        (None, None),
    ],
)
def test_parse_name_strips_titles_and_relations(raw, expected):
    assert mpi.parse_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("Male", "M"), ("m", "M"), ("FEMALE", "F"), ("f", "F"), ("Other", "O"), ("x", None),
     (None, None)],
)  # fmt: skip
def test_parse_sex(raw, expected):
    assert mpi.parse_sex(raw) == expected


@pytest.mark.parametrize(
    "age,year",
    [("48 Y", 1978), ("48 yrs", 1978), ("48", 1978), ("6 months", 2026), ("18 months", 2025),
     ("10 D", 2026), ("abc", None), ("400 years", None)],
)  # fmt: skip
def test_parse_age_to_birth_year(age, year):
    assert mpi.parse_age(age, REF) == year


def test_candidate_from_payload():
    c = mpi.candidate_from_payload(
        {"patient": {"name": "Mrs. Anjali Das", "sex": "Female", "age_text": "48 Y",
                     "abha": "91-1234-5678-9012", "mrn": "LEG-1"}}, REF
    )  # fmt: skip
    assert (c.name, c.sex, c.birth_year, c.abha, c.mrn) == ("Anjali Das", "F", 1978,
                                                             "91123456789012", "LEG-1")  # fmt: skip
    c = mpi.candidate_from_payload({"patient": {"name": "X", "dob": "1978-01-01"}}, REF)
    assert c.dob == date(1978, 1, 1) and c.birth_year == 1978
    assert mpi.candidate_from_payload({}, REF).name is None  # handwritten note, no patient block
    assert mpi.candidate_from_payload({"patient": {"abha": "123"}}, REF).abha is None  # malformed


@pytest.mark.parametrize(
    "a,b,ok",
    [
        ("Anjali Das", "Mrs Anjali Das", True),
        ("Anjali Das", "Anjali", True),  # token subset
        ("Anjali Das", "Anjli Das", True),  # OCR slip, fuzzy >= 0.72
        ("Anjali Das", "Ramesh Kumar", False),
        ("Anjali Das", None, False),
    ],
)
def test_names_match(a, b, ok):
    assert mpi.names_match(a, b) is ok


def test_dob_match_plus_minus_one_year():
    assert mpi.dob_match(date(1978, 1, 1), 1979)
    assert not mpi.dob_match(date(1978, 1, 1), 1980)
    assert mpi.dob_match(None, 1950)  # nothing to contradict


def test_mode_prefers_most_supported_then_longest():
    assert mpi._mode(["Anjali", "Anjali Das", "Anjali Das"]) == ("Anjali Das", 2 / 3)
    assert mpi._mode(["Anjali", "Anjali Das"])[0] == "Anjali Das"  # tie -> longest
    assert mpi._mode([None, ""]) == (None, 0.0)
