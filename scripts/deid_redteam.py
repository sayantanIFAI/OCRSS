"""Red-team re-identification attempt on a synthetic sample (backlog E11-S4 acceptance).

    python scripts/deid_redteam.py --patients 8 --report deid_redteam.json

Builds scanned documents for synthetic patients carrying every kind of Indian direct identifier
(name, relative's name, DOB, ABHA number + address, Aadhaar with a valid check digit, PAN, phone,
e-mail, address + PIN, MRN / UHID, CareFlow MPI, the treating doctor) plus an answer key per
document, as E2-S4 will hold. Then, for each document:

  1. control: the attack on the ORIGINAL output must find the identifiers (proves it works)
  2. de-identify (OCR -> cdi_adapter.deid) and re-OCR the redacted page image
  3. the attack on the DE-IDENTIFIED output (text, answer key, re-OCR'd image) must find nothing

Exit 0 only if every control succeeds and no identifier leaks. No real patient data (§11).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from make_sample_docs import scanify, text_pdf  # noqa: E402

FIRST = ("Meenakshi", "Rajendra", "Harpreet", "Sanjukta", "Venkatesh", "Farzana", "Gopinath",
         "Lalitha", "Tejaswini", "Mohammed", "Anupama", "Balwinder")  # fmt: skip
LAST = ("Subramaniam", "Chatterjee", "Deshmukh", "Kulkarni", "Banerjee", "Siddiqui", "Raghavan",
        "Mukherjee", "Venkataraman", "Srivastava", "Bhattacharya", "Gangopadhyay")  # fmt: skip
STREETS = ("Lake Gardens", "Anna Nagar West", "Koregaon Park", "Salt Lake Sector 3",
           "Jayanagar 4th Block", "Banjara Hills")  # fmt: skip
CITIES = ("Kolkata", "Chennai", "Pune", "Bengaluru", "Hyderabad", "Lucknow")


def verhoeff_digit(number: str) -> str:
    from cdi_adapter.deid.recognizers import _D, _P

    inv = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]
    c = 0
    for i, d in enumerate(reversed([int(x) for x in number])):
        c = _D[c][_P[(i + 1) % 8][d]]
    return str(inv[c])


@dataclass
class Person:
    truth: dict[str, Any]
    lines: list[list[str]]  # one list of text lines per document
    keys: list[dict[str, Any]]  # the answer key per document


def make_person(i: int, seed: int) -> Person:
    rnd = random.Random(f"redteam:{seed}:{i}")
    first, last = FIRST[i % len(FIRST)], LAST[(i * 5 + seed) % len(LAST)]
    name = f"{first} {last}"
    father = f"{FIRST[(i + 3) % len(FIRST)]} {last}"
    dob = date(1950, 1, 1) + timedelta(days=rnd.randint(0, 20000))
    abha = f"91-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}"
    base11 = str(rnd.randint(2, 9)) + "".join(str(rnd.randint(0, 9)) for _ in range(10))
    aadhaar = base11 + verhoeff_digit(base11)
    aadhaar_fmt = f"{aadhaar[:4]} {aadhaar[4:8]} {aadhaar[8:]}"
    pan = "".join(rnd.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(3)) + "P" + last[0] + \
        f"{rnd.randint(1000, 9999)}" + rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")  # fmt: skip
    phone = f"+91 9{rnd.randint(100000000, 999999999)}"
    email = f"{first.lower()}.{last.lower()}{rnd.randint(10, 99)}@example.in"
    abha_addr = f"{first.lower()}{rnd.randint(100, 999)}@abdm"
    pin = str(rnd.randint(110001, 855999))
    street = f"{rnd.randint(1, 250)}, {rnd.choice(STREETS)}"
    city = rnd.choice(CITIES)
    uhid = f"UH{rnd.randint(100000, 999999)}"
    mpi = f"CFP-2026-{rnd.randint(100000, 999999)}"
    doctor = f"Dr. {FIRST[(i + 7) % len(FIRST)]} {LAST[(i + 2) % len(LAST)]}"
    visit = date(2026, 8, 1) + timedelta(days=rnd.randint(0, 40))
    age = (visit - dob).days // 365
    sex = rnd.choice("MF")
    d = lambda x: x.strftime("%d/%m/%Y")  # noqa: E731

    registration = [
        "CareFlow Polyclinic - PATIENT REGISTRATION",
        f"Name: {name}   S/o or W/o: {father}",
        f"DOB: {d(dob)}   Age/Sex: {age} Y / {sex}",
        f"ABHA No: {abha}   ABHA Address: {abha_addr}",
        f"Aadhaar: {aadhaar_fmt}   PAN: {pan}",
        f"Mobile: {phone}   Email: {email}",
        f"Address: {street}, {city}   PIN: {pin}",
        f"UHID: {uhid}   MPI: {mpi}",
    ]
    opd = [
        "CareFlow Polyclinic - OPD CONSULTATION",
        f"Patient: {name}   Age: {age} yrs   UHID: {uhid}",
        f"Date: {d(visit)}   Consultant: {doctor}",
        "Complaints: fever with chills for 3 days, body ache",
        "Diagnosis: Dengue fever, suspected",
        "Rx: Tab Paracetamol 650 mg SOS, oral fluids 3 L/day",
        f"Next visit: {d(visit + timedelta(days=3))}   Contact: {phone}",
    ]
    lab = [
        "CAREFLOW DIAGNOSTICS - LABORATORY REPORT",
        f"Name: {name}   Age/Sex: {age} Y / {sex}   Sample ID: LAB{rnd.randint(10000, 99999)}",
        f"Collected on: {d(visit)}   Referred by: {doctor}",
        "Platelet count 88000 /uL 150000-410000 L",
        "Haemoglobin 12.1 g/dL 12.0-15.5",
        "NS1 antigen POSITIVE",
    ]
    truth = {"name": name, "father_name": father, "dob": dob, "abha": abha,
             "abha_address": abha_addr, "aadhaar": aadhaar, "pan": pan, "phone": phone,
             "email": email, "address": f"{street}, {city}", "pin": pin, "uhid": uhid,
             "mpi": mpi, "doctor": doctor}  # fmt: skip
    keys = [
        {"doc_type": "administrative", "patient": {"name": name, "dob": d(dob), "sex": sex,
         "abha_number": abha, "phone": phone, "address": f"{street}, {city} {pin}"}},
        {"doc_type": "opd_note", "patient": {"name": name}, "visit_date": d(visit),
         "facts": [{"type": "symptom", "text": "fever with chills for 3 days"},
                   {"type": "condition", "text": "Dengue fever, suspected"}]},
        {"doc_type": "lab_report", "patient": {"name": name}, "collected": d(visit),
         "facts": [{"type": "lab_result", "text": "Platelet count", "value": 88000}]},
    ]  # fmt: skip
    return Person(truth, [registration, opd, lab], keys)


def run(patients: int, seed: int, dpi: int = 150) -> dict[str, Any]:
    from cdi_adapter.deid import redteam
    from cdi_adapter.deid.document import Page, deidentify_document, ocr_lines

    results = []
    for i in range(patients):
        person = make_person(i, seed)
        attack_truth = {k: v for k, v in person.truth.items() if k != "doctor"}
        attack_truth["doctor_name"] = person.truth["doctor"].removeprefix("Dr. ")
        for n, (lines, key) in enumerate(zip(person.lines, person.keys, strict=True)):
            png = scanify(text_pdf(lines), seed=seed * 1000 + i * 10 + n, dpi=dpi)
            ocr = ocr_lines(png)  # every line, as the de-identification reads it
            original = {"ocr_text": [ln.text for ln in ocr],
                        "payload": redteam.payload_strings(key)}  # fmt: skip
            control = redteam.attempt(_truth(attack_truth), original)
            res = deidentify_document([Page(png, ocr)], key)
            # the attacker reads the redacted image with no confidence cut-off either
            reocr = [ln.text for ln in ocr_lines(res.pages[0])]
            outputs = {"ocr_text": res.ocr_text, "payload": redteam.payload_strings(res.payload),
                       "page_image": reocr}  # fmt: skip
            leaks = redteam.attempt(_truth(attack_truth), outputs)
            results.append(
                {
                    "patient": i,
                    "document": n,
                    "control_found": len(control),
                    "leaks": [leak.__dict__ for leak in leaks],
                    "entities": res.counts,
                    "redacted_lines": res.redacted_lines,
                }
            )
    return {
        "patients": patients,
        "documents": len(results),
        "control_succeeded": all(r["control_found"] > 0 for r in results),
        "leaks": sum(len(r["leaks"]) for r in results),
        "passed": all(r["control_found"] > 0 for r in results)
        and not any(r["leaks"] for r in results),
        "results": results,
    }


def _truth(t: dict[str, Any]) -> dict[str, Any]:
    out = dict(t)
    out["name"] = f"{t['name']} {t['father_name']} {t['doctor_name']}"  # every person's name tokens
    del out["father_name"], out["doctor_name"]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--patients", type=int, default=8)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    report = run(args.patients, args.seed)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for r in report["results"]:
        state = "LEAK" if r["leaks"] else "ok  "
        print(
            f"{state} patient {r['patient']} doc {r['document']}: control found "
            f"{r['control_found']}, leaks {len(r['leaks'])}, redacted {r['redacted_lines']} lines"
        )
        for leak in r["leaks"]:
            print(f"       {leak['identifier']} in {leak['where']} ({leak['how']})")
    control = "OK" if report["control_succeeded"] else "FAILED"
    verdict = "PASSED" if report["passed"] else "FAILED"
    print(f"{report['documents']} documents: control {control}, "
          f"{report['leaks']} leak(s) -> {verdict}")  # fmt: skip
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
