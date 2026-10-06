"""Drive S1-S3 (or the full S1-S9 chain) over a folder of documents and print a summary
(ARCHITECTURE §11). Needs PostgreSQL + MinIO (+ the model gateway for extraction).

    python scripts/pipeline_smoke.py data/samples            # S1 ingest -> S3 OCR -> S2 classify
    python scripts/pipeline_smoke.py data/samples --full     # ... -> S4-S6 -> S9 FHIR
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def run(folder: Path, full: bool) -> list[dict]:
    from cdi_adapter.classify.service import classify_document
    from cdi_adapter.extract.service import extract_document
    from cdi_adapter.fhir.service import project_document
    from cdi_adapter.ingest.service import ingest_bytes
    from cdi_adapter.ocr.service import ocr_document
    from cdi_adapter.terminology.service import bind_document
    from cdi_adapter.validate.service import validate_document

    rows, patient = [], None
    for f in sorted(p for p in folder.iterdir() if p.is_file()):
        t0 = time.perf_counter()
        res = ingest_bytes(f.read_bytes(), f.name, source_channel="smoke")
        doc = str(res.document_id)
        ocr = ocr_document(doc, force_engine="rapidocr")
        cls = classify_document(doc)
        row = {"file": f.name, "document_id": doc, "pages": res.page_count, "blocks": ocr["blocks"],
               "doc_type": cls["doc_type"], "method": cls["method"]}  # fmt: skip
        if full:
            ext = extract_document(doc, patient_id=patient)
            patient = patient or ext.get("patient_id")
            bind_document(doc)
            gate = validate_document(doc)
            fhir = project_document(doc)
            row |= {"facts": ext.get("facts", 0), "auto": gate["auto_accepted"],
                    "held": gate["in_review"], "bundle": fhir["status"]}  # fmt: skip
        row["seconds"] = round(time.perf_counter() - t0, 1)
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="pipeline smoke run")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--full", action="store_true", help="run S4-S9 as well")
    args = ap.parse_args()
    rows = run(args.folder, args.full)
    keys = list(rows[0]) if rows else []
    print(" | ".join(keys))
    for r in rows:
        print(" | ".join(str(r.get(k, "")) for k in keys))


if __name__ == "__main__":
    main()
