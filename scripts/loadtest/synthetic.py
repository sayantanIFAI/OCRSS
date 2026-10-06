"""Synthetic patients for the load test (backlog E1-S7; ARCHITECTURE §11: no real patient data).

Each patient gets 5-10 scanned-looking documents (the E0-S16 renderer in make_sample_docs.py) of
the types the S2 heuristic classifies without a VLM call (§7.2): prescription, lab report, vitals
sheet, radiology report, discharge summary. Same seed -> byte-identical documents, so two runs
load the system with exactly the same work.
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from make_sample_docs import scanify, text_pdf  # noqa: E402

FIRST = (
    "Asha",
    "Ravi",
    "Meera",
    "Arjun",
    "Kavya",
    "Suresh",
    "Nisha",
    "Vikram",
    "Priya",
    "Manoj",
    "Lakshmi",
    "Rahul",
    "Deepa",
    "Anil",
    "Sunita",
    "Kiran",
    "Farah",
    "Joseph",
    "Gita",
    "Imran",
)
LAST = (
    "Rao",
    "Sharma",
    "Iyer",
    "Menon",
    "Das",
    "Patel",
    "Nair",
    "Khan",
    "Reddy",
    "Singh",
    "Bose",
    "Pillai",
    "Joshi",
    "Verma",
    "Kulkarni",
    "Fernandes",
    "Chopra",
    "Gupta",
)
DRUGS = (
    "Tab Metformin 500 mg 1-0-1 x 30 days after food",
    "Tab Amlodipine 5 mg OD x 30 days",
    "Tab Atorvastatin 10 mg 0-0-1 x 30 days",
    "Tab Telmisartan 40 mg OD x 30 days",
    "Cap Omeprazole 20 mg OD before breakfast x 14 days",
    "Tab Paracetamol 650 mg SOS",
    "Tab Levothyroxine 50 mcg OD empty stomach",
    "Tab Aspirin 75 mg OD after lunch",
)
DIAGNOSES = (
    "Type 2 diabetes mellitus",
    "Hypertension",
    "Dyslipidaemia",
    "Hypothyroidism",
    "Acid peptic disease",
    "Osteoarthritis knee",
)

DOC_TYPES = ("prescription", "lab_report", "vitals_sheet", "radiology", "discharge_summary")


@dataclass(frozen=True)
class Document:
    filename: str
    doc_type: str
    content: bytes
    mime: str = "image/png"


@dataclass(frozen=True)
class Patient:
    index: int
    name: str
    age: int
    sex: str
    documents: list[Document] = field(default_factory=list)


def _d(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _prescription(p: Patient, rnd: random.Random, day: date) -> list[str]:
    dx = rnd.sample(DIAGNOSES, 2)
    return [
        "CareFlow Polyclinic - Rx",
        f"Name: {p.name}   Age/Sex: {p.age} Y / {p.sex}",
        f"Date: {_d(day)}",
        f"Diagnosis: {dx[0]}, {dx[1]}",
        "Rx",
        *rnd.sample(DRUGS, 3),
        f"BP: {rnd.randint(112, 150)}/{rnd.randint(70, 94)} mmHg   "
        f"Weight: {rnd.randint(48, 92)} kg",
        "Review after 1 month",
    ]


def _lab(p: Patient, rnd: random.Random, day: date) -> list[str]:
    return [
        "CAREFLOW DIAGNOSTICS - LABORATORY REPORT",
        f"Name: {p.name}   Age: {p.age} Y   Sex: {p.sex}",
        f"Reported on: {_d(day)}   Specimen: Blood",
        "Test   Result   Unit   Biological Reference Range",
        f"HbA1c {rnd.uniform(5.2, 9.5):.1f} % 4.0-5.6",
        f"Fasting Blood Glucose {rnd.randint(82, 190)} mg/dL 70-100",
        f"Serum Creatinine {rnd.uniform(0.6, 1.6):.1f} mg/dL 0.6-1.3",
        f"Total Cholesterol {rnd.randint(140, 260)} mg/dL 100-200",
        "Pathologist: Dr. S. Gupta",
    ]


def _vitals(p: Patient, rnd: random.Random, day: date) -> list[str]:
    return [
        "CareFlow Polyclinic - VITAL SIGNS CHART",
        f"Name: {p.name}   Age: {p.age} Y   Ward: OPD",
        f"Recorded on: {_d(day)}",
        f"Time 09:00  Pulse: {rnd.randint(62, 98)} /min   "
        f"Respiratory rate: {rnd.randint(14, 20)} /min",
        f"Time 09:00  Temperature: {rnd.uniform(97.6, 99.4):.1f} F   SpO2: {rnd.randint(94, 99)} %",
        f"Weight: {rnd.randint(48, 92)} kg   Height: {rnd.randint(148, 182)} cm",
        "Nurse on duty: R. Thomas",
    ]


def _radiology(p: Patient, rnd: random.Random, day: date) -> list[str]:
    part = rnd.choice(("Chest PA view", "Left knee AP/lateral", "Lumbar spine AP/lateral"))
    return [
        "CAREFLOW IMAGING - RADIOLOGY REPORT",
        f"Name: {p.name}   Age/Sex: {p.age} Y / {p.sex}",
        f"Date: {_d(day)}",
        f"Examination: X-ray {part}",
        "Findings: No acute bony or soft tissue abnormality.",
        "Impression: Study within normal limits.",
        "Radiologist: Dr. P. Menon",
    ]


def _discharge(p: Patient, rnd: random.Random, day: date) -> list[str]:
    dx = rnd.choice(DIAGNOSES)
    return [
        "CareFlow Hospital - DISCHARGE SUMMARY",
        f"Name: {p.name}   Age/Sex: {p.age} Y / {p.sex}",
        f"Date of admission: {_d(day - timedelta(days=3))}   Date of discharge: {_d(day)}",
        f"Final diagnosis: {dx}",
        "Course in hospital: uneventful, managed conservatively.",
        "Condition at discharge: stable",
        f"Advice on discharge: {rnd.choice(DRUGS)}",
        "Follow up in OPD after 2 weeks",
    ]


RENDER: dict[str, Callable[[Patient, random.Random, date], list[str]]] = {
    "prescription": _prescription,
    "lab_report": _lab,
    "vitals_sheet": _vitals,
    "radiology": _radiology,
    "discharge_summary": _discharge,
}


def make_patient(
    index: int, *, seed: int, docs_min: int = 5, docs_max: int = 10, dpi: int = 150
) -> Patient:
    """Patient ``index`` of a run: a unique name, then docs_min..docs_max scanned documents."""
    if not 1 <= docs_min <= docs_max <= 10:  # the upload form takes at most 10 files (§7.1)
        raise ValueError("need 1 <= docs_min <= docs_max <= 10")
    rnd = random.Random(f"{seed}:{index}")
    name = f"{FIRST[index % len(FIRST)]} {LAST[(index // len(FIRST) + seed) % len(LAST)]}"
    base = Patient(
        index, f"{name} LT{seed % 1000:03d}{index:03d}", rnd.randint(22, 78), rnd.choice("MF")
    )
    day = date(2026, 8, 1) + timedelta(days=rnd.randint(0, 40))
    docs = []
    for n in range(rnd.randint(docs_min, docs_max)):
        doc_type = DOC_TYPES[n % len(DOC_TYPES)]
        lines = RENDER[doc_type](base, rnd, day - timedelta(days=7 * n))
        png = scanify(text_pdf(lines), seed=rnd.randint(0, 2**31), dpi=dpi)
        docs.append(Document(f"p{index:03d}_{n:02d}_{doc_type}.png", doc_type, png))
    return Patient(base.index, base.name, base.age, base.sex, docs)
