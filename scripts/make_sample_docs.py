"""Generate synthetic scanned documents (ARCHITECTURE §11): the §8.2 "Anjali Das" set — a
prescription, a lab report and a vitals sheet — rendered to PDF, then made to look scanned
(small rotation, speckle noise, soft blur). No real patient data.

    python scripts/make_sample_docs.py --out data/samples [--clean]
"""

from __future__ import annotations

import argparse
import io
import random
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

DOCS: dict[str, list[str]] = {
    "prescription_00": [
        "CareFlow Polyclinic - Rx",
        "Name: Anjali Das   Age/Sex: 48 Y / F",
        "Date: 12/08/2026",
        "Diagnosis: Type 2 diabetes mellitus, Hypertension",
        "Rx",
        "Tab Metformin 500 mg 1-0-1 x 30 days after food",
        "Tab Amlodipine 5 mg OD x 30 days",
        "Tab Atorvastatin 10 mg 0-0-1 x 30 days",
        "BP: 138/86 mmHg   Weight: 72 kg",
        "Advice: low salt diet, walk 30 min daily",
        "Review after 1 month with HbA1c",
    ],
    "lab_report_01": [
        "CAREFLOW DIAGNOSTICS - LABORATORY REPORT",
        "Name: Anjali Das   Age: 48 Y   Sex: F",
        "Reported on: 11/08/2026   Specimen: Blood",
        "Test   Result   Unit   Biological Reference Range",
        "HbA1c 7.8 % 4.0-5.6 H",
        "Fasting Blood Glucose 142 mg/dL 70-100 H",
        "Serum Creatinine 1.1 mg/dL 0.6-1.3",
        "Total Cholesterol 212 mg/dL 100-200 H",
        "LDL Cholesterol 138 mg/dL 50-130 H",
        "Pathologist: Dr. S. Gupta",
    ],
    "vitals_sheet_02": [
        "CareFlow Polyclinic - VITAL SIGNS CHART",
        "Name: Anjali Das   Age: 48 Y   Ward: OPD",
        "Recorded on: 12/08/2026",
        "Time 09:00  Pulse: 78 /min   Respiratory rate: 16 /min",
        "Time 09:00  Temperature: 98.4 F   SpO2: 98 %",
        "Time 13:00  Pulse: 80 /min   Respiratory rate: 18 /min",
        "Time 13:00  Temperature: 98.6 F   SpO2: 97 %",
        "Weight: 72 kg   Height: 158 cm",
        "Allergy: Penicillin",
        "Nurse on duty: R. Thomas",
    ],
}


def text_pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, line in enumerate(lines):
        page.insert_text((56, 80 + 26 * i), line, fontsize=13)
    out = doc.tobytes()
    doc.close()
    return out


def scanify(pdf: bytes, seed: int, dpi: int = 150) -> bytes:
    """Rasterise, rotate slightly, add speckle + blur: a PNG that looks like a flatbed scan."""
    rnd = random.Random(seed)
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        png = d[0].get_pixmap(dpi=dpi).tobytes("png")
    im = Image.open(io.BytesIO(png)).convert("L")
    im = im.rotate(rnd.uniform(-4, 4), fillcolor=255, expand=False)
    px = im.load()
    w, h = im.size
    for _ in range(w * h // 400):
        px[rnd.randrange(w), rnd.randrange(h)] = rnd.choice((0, 90, 200))
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def generate(out: Path, *, seed: int = 7) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for i, (name, lines) in enumerate(DOCS.items()):
        pdf = text_pdf(lines)
        (out / f"{name}.pdf").write_bytes(pdf)
        (out / f"{name}.scan.png").write_bytes(scanify(pdf, seed + i))
        written += [out / f"{name}.pdf", out / f"{name}.scan.png"]
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("data/samples"))
    ap.add_argument("--clean", action="store_true", help="delete existing files in --out first")
    args = ap.parse_args()
    if args.clean and args.out.exists():
        for f in args.out.glob("*"):
            f.unlink()
    for p in generate(args.out):
        print(p)


if __name__ == "__main__":
    main()
