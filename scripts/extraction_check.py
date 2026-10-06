"""End-to-end prescription extraction check on the live stack (2026-09-25 fix: the hf gateway shows
the model the schema's keys; the repair keeps every value the model read).

Synthetic scanned prescriptions with a known answer (no real patient data, ARCHITECTURE §11) go
through the real web app, queue, worker and model gateway. For each one it reports what the saved
extraction holds against the page: every medicine with strength / frequency / duration / route /
instructions, the diagnoses, BP / weight / pulse and allergies, plus

* the facts the pipeline released,
* nothing lost in repair: every value in the model's raw answer is still in the repaired payload,
  and the saved payload is that repaired payload,
* ``--before OLD_CLIENT_PY``: the same pages through the old path (no schema in the prompt, the old
  repair) for a before/after table.

    python scripts/extraction_check.py [--before /tmp/old/src/cdi_adapter/ml/client.py]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

# (drug key, strength, frequency, duration, route, instructions) as written on the page
Med = tuple[str, str, str | None, str | None, str | None, str | None]
CASES: list[dict[str, Any]] = [
    {"name": "C1 corpus layout, 3 medicines",
     "lines": ["CareFlow Polyclinic - Rx", "Name: Test Patient One   Age/Sex: 45 Y / M",
               "Date: 12/08/2026", "Diagnosis: Type 2 diabetes mellitus, Hypertension", "Rx",
               "Tab Metformin 500 mg 1-0-1 x 30 days after food",
               "Tab Amlodipine 5 mg OD x 30 days", "Tab Atorvastatin 10 mg 0-0-1 x 30 days",
               "BP: 138/86 mmHg   Weight: 72 kg", "Review after 1 month"],
     "meds": [("Metformin", "500", "1-0-1", "30", None, "after food"),
              ("Amlodipine", "5", "OD", "30", None, None),
              ("Atorvastatin", "10", "0-0-1", "30", None, None)],
     "dx": ["diabetes", "Hypertension"], "vitals": {"bp": "138/86", "weight": "72"}},
    {"name": "C2 six medicines, pulse, SOS",
     "lines": ["CareFlow Polyclinic - Rx", "Name: Test Patient Two   Age/Sex: 62 Y / F",
               "Date: 03/09/2026",
               "Diagnosis: Hypothyroidism, Dyslipidaemia, Osteoarthritis knee", "Rx",
               "Tab Levothyroxine 50 mcg 1-0-0 empty stomach x 90 days",
               "Tab Rosuvastatin 10 mg 0-0-1 x 30 days",
               "Tab Aceclofenac 100 mg 1-0-1 after food x 5 days",
               "Cap Pantoprazole 40 mg 1-0-0 before breakfast x 5 days",
               "Tab Calcium + Vitamin D3 500 mg 0-1-0 x 60 days", "Tab Paracetamol 650 mg SOS",
               "BP: 124/78 mmHg   Pulse: 82/min   Weight: 58 kg", "Review after 2 weeks"],
     "meds": [("Levothyroxine", "50", "1-0-0", "90", None, "empty stomach"),
              ("Rosuvastatin", "10", "0-0-1", "30", None, None),
              ("Aceclofenac", "100", "1-0-1", "5", None, "after food"),
              ("Pantoprazole", "40", "1-0-0", "5", None, "before breakfast"),
              ("Calcium", "500", "0-1-0", "60", None, None),
              ("Paracetamol", "650", "SOS", None, None, None)],
     "dx": ["Hypothyroidism", "Dyslipidaemia", "Osteoarthritis"],
     "vitals": {"bp": "124/78", "pulse": "82", "weight": "58"}},
    {"name": "C3 syrups, injection, nebuliser, drops, allergy",
     "lines": ["CareFlow Polyclinic - Rx", "Name: Test Child Three   Age/Sex: 6 Y / M",
               "Date: 18/09/2026", "Diagnosis: Acute bronchitis", "Allergy: Penicillin", "Rx",
               "Syp Ambroxol 15 mg/5 ml 5 ml TDS x 5 days",
               "Syp Paracetamol 250 mg/5 ml 7.5 ml SOS",
               "Inj Ceftriaxone 1 g IV BD x 3 days", "Neb Salbutamol 2.5 mg TDS x 3 days",
               "Saline nasal drops 2 drops each nostril TDS",
               "Weight: 18 kg   Temp: 101.2 F", "Review after 3 days"],
     "meds": [("Ambroxol", "15", "TDS", "5", None, None),
              ("Paracetamol", "250", "SOS", None, None, None),
              ("Ceftriaxone", "1", "BD", "3", "IV", None),
              ("Salbutamol", "2.5", "TDS", "3", None, None),
              ("Saline", None, "TDS", None, None, None)],
     "dx": ["bronchitis"], "vitals": {"weight": "18"}, "allergies": ["Penicillin"]},
    {"name": "C4 table layout, blood sugar",
     "lines": ["CareFlow Polyclinic - Rx", "Name: Test Patient Four   Age/Sex: 51 Y / M",
               "Date: 21/09/2026", "Diagnosis: Type 2 diabetes mellitus",
               "Medicine | Dose | Frequency | Days",
               "Tab Telmisartan 40 mg | 1 tab | OD | 30",
               "Tab Metformin 1000 mg | 1 tab | BD | 30",
               "Tab Glimepiride 2 mg | 1 tab | OD before breakfast | 30",
               "Tab Aspirin 75 mg | 1 tab | OD after lunch | 30",
               "BP: 142/90 mmHg   Weight: 84 kg   RBS: 186 mg/dl", "Review after 1 month"],
     "meds": [("Telmisartan", "40", "OD", "30", None, None),
              ("Metformin", "1000", "BD", "30", None, None),
              ("Glimepiride", "2", "OD", "30", None, "before breakfast"),
              ("Aspirin", "75", "OD", "30", None, "after lunch")],
     "dx": ["diabetes"], "vitals": {"bp": "142/90", "weight": "84"}},
    {"name": "C5 eight cardiac medicines",
     "lines": ["CareFlow Polyclinic - Rx", "Name: Test Patient Five   Age/Sex: 68 Y / M",
               "Date: 24/09/2026", "Diagnosis: Coronary artery disease, Heart failure", "Rx",
               "Tab Aspirin 75 mg 0-1-0 x 30 days", "Tab Clopidogrel 75 mg 0-1-0 x 30 days",
               "Tab Atorvastatin 40 mg 0-0-1 x 30 days", "Tab Metoprolol 25 mg 1-0-1 x 30 days",
               "Tab Ramipril 2.5 mg 1-0-0 x 30 days", "Tab Furosemide 40 mg 1-0-0 x 30 days",
               "Tab Spironolactone 25 mg 0-1-0 x 30 days",
               "Tab Pantoprazole 40 mg 1-0-0 before breakfast x 30 days",
               "BP: 110/70 mmHg   Pulse: 64/min   Weight: 67 kg",
               "Review after 15 days with ECG"],
     "meds": [("Aspirin", "75", "0-1-0", "30", None, None),
              ("Clopidogrel", "75", "0-1-0", "30", None, None),
              ("Atorvastatin", "40", "0-0-1", "30", None, None),
              ("Metoprolol", "25", "1-0-1", "30", None, None),
              ("Ramipril", "2.5", "1-0-0", "30", None, None),
              ("Furosemide", "40", "1-0-0", "30", None, None),
              ("Spironolactone", "25", "0-1-0", "30", None, None),
              ("Pantoprazole", "40", "1-0-0", "30", None, "before breakfast")],
     "dx": ["Coronary artery disease", "Heart failure"],
     "vitals": {"bp": "110/70", "pulse": "64", "weight": "67"}},
]  # fmt: skip


def scan(lines: list[str], seed: int) -> bytes:
    from make_sample_docs import scanify, text_pdf

    return scanify(text_pdf(lines), seed=seed, dpi=150)


def _text(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x


def score(case: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """The saved payload against the page. A field counts when its value is present as written."""
    meds = [m for m in payload.get("medications") or [] if isinstance(m, dict)]
    found, fields_ok, fields_all, misses = 0, 0, 0, []
    for key, strength, freq, days, route, instr in case["meds"]:
        m = next((m for m in meds if key.lower() in str(m.get("drug_name", "")).lower()), None)
        if m is None:
            misses.append(f"{key}: missing")
            continue
        found += 1
        blob = _text(m).lower()
        for label, want in (("strength", strength), ("frequency", freq), ("duration", days),
                            ("route", route), ("instructions", instr)):  # fmt: skip
            if want is None:
                continue
            fields_all += 1
            if want.lower() in blob:
                fields_ok += 1
            else:
                misses.append(f"{key}.{label} ({want})")
    dx = _text(payload.get("diagnoses") or []).lower()
    dx_ok = [d for d in case["dx"] if d.lower() in dx]
    vit = _text(payload.get("vitals") or []).lower()
    vit_ok = [k for k, v in case["vitals"].items() if v in vit]
    alg = _text(payload.get("allergies") or []).lower()
    alg_ok = [a for a in case.get("allergies", []) if a.lower() in alg]
    misses += [f"dx {d}" for d in case["dx"] if d not in dx_ok]
    misses += [f"vital {k}" for k in case["vitals"] if k not in vit_ok]
    misses += [f"allergy {a}" for a in case.get("allergies", []) if a not in alg_ok]
    return {"meds": f"{found}/{len(case['meds'])}", "med_fields": f"{fields_ok}/{fields_all}",
            "dx": f"{len(dx_ok)}/{len(case['dx'])}",
            "vitals": f"{len(vit_ok)}/{len(case['vitals'])}",
            "allergies": f"{len(alg_ok)}/{len(case.get('allergies', []))}",
            "partial": bool(payload.get("_partial")), "misses": misses}  # fmt: skip


def leaves(x: Any, skip: tuple[str, ...] = ("evidence", "confidence")) -> set[str]:
    """Every scalar value (as lowercase text) in a model answer, except evidence / confidence."""
    out: set[str] = set()
    if isinstance(x, dict):
        for k, v in x.items():
            if k not in skip and not k.startswith("_"):
                out |= leaves(v, skip)
    elif isinstance(x, list):
        for v in x:
            out |= leaves(v, skip)
    elif x not in (None, ""):
        out.add(str(x).strip().lower())
    return out


def lost_in_repair(raw: dict[str, Any], repaired: dict[str, Any]) -> list[str]:
    kept = " ".join(leaves(repaired))
    return sorted(v for v in leaves(raw) if v not in kept)


def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch in "/-.")


def not_on_page(payload: dict[str, Any], page_text: str) -> list[str]:
    """Medicine fields and vital values the saved payload holds that are NOT on the page (an
    example copied from the prompt, a value moved to the wrong field, an invented one)."""
    page = _squash(page_text)
    out = []
    for m in payload.get("medications") or []:
        if not isinstance(m, dict):
            continue
        for f in ("strength", "dose", "frequency", "route", "duration", "instructions"):
            v = m.get(f)
            if isinstance(v, dict):
                v = " ".join(str(x) for x in (v.get("value"), v.get("unit")) if x not in (None, ""))
            if v not in (None, "") and _squash(str(v)) and _squash(str(v)) not in page:
                out.append(f"{m.get('drug_name')}.{f}={v!r}")
    for v in payload.get("vitals") or []:
        if isinstance(v, dict) and not isinstance(v.get("value"), dict):
            val = str(v.get("value") or "")
            if val and _squash(val) not in page:
                out.append(f"vital {v.get('name')}={val!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="http://127.0.0.1:8888")
    ap.add_argument("--before", help="the old ml/client.py, for the before/after table")
    args = ap.parse_args()

    import httpx
    from sqlalchemy import text

    from cdi_adapter import repo
    from cdi_adapter.db import session_scope
    from cdi_adapter.extract.prompt import (
        SCHEMA_FOR_DOC_TYPE,
        build_extraction_prompt,
        max_tokens_for,
    )
    from cdi_adapter.ml.client import HttpMLClient, bundled_schema, extract_json, repair_payload
    from cdi_adapter.storage import get_store
    from cdi_adapter.webapp.auth import hash_password

    old = None
    if args.before:
        spec = importlib.util.spec_from_file_location("old_client", args.before)
        assert spec and spec.loader
        old = importlib.util.module_from_spec(spec)
        sys.modules["old_client"] = old  # its dataclasses look their module up
        spec.loader.exec_module(old)

    user, pw = f"rx-check-{uuid.uuid4().hex[:6]}", secrets.token_urlsafe(24)
    with session_scope() as c:
        c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                       "VALUES (:u, 'extraction check', 'admin', :p)"),
                  {"u": user, "p": hash_password(pw)})  # fmt: skip
        repo.write_audit(
            c,
            actor="rx-check",
            action="create",
            entity="web_user",
            entity_id=user,
            detail={"purpose": "extraction check"},
        )
    gw = HttpMLClient(timeout=600)
    report: list[dict[str, Any]] = []
    try:
        web = httpx.Client(base_url=args.host, timeout=120, headers={"Origin": args.host})
        r = web.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        web.headers["Cookie"] = f"cdi_session={r.cookies['cdi_session']}"
        tag = uuid.uuid4().hex[:6]
        for n, case in enumerate(CASES):
            png = scan(case["lines"] + [f"ref {tag}-{n}"], seed=1000 + n)
            t0 = time.time()
            upload = [("files", (f"rx-{tag}-{n}.png", png, "image/png"))]
            job = web.post("/api/jobs", files=upload).json()["job_id"]
            while (s := web.get(f"/api/jobs/{job}").json())["state"] == "running":
                if time.time() - t0 > 900:
                    break
                time.sleep(3)
            d = s["documents"][0]
            doc = d["document_id"]
            with session_scope() as c:
                saved = c.execute(text("SELECT payload FROM extraction WHERE document_id = :d"),
                                  {"d": doc}).scalar_one()  # fmt: skip
                facts = dict(
                    c.execute(
                        text(
                            "SELECT fact_type, count(*) FROM clinical_fact WHERE is_current AND "
                            "CAST(:d AS uuid) = ANY(source_doc_ids) GROUP BY 1"
                        ),
                        {"d": doc},
                    ).all()
                )
                blocks = repo.get_ocr_blocks(c, doc)
                cls = repo.get_latest_classification(c, doc)
                page = repo.get_pages(c, doc)[0]
            dt = cls["doc_type"]
            prompt, _ = build_extraction_prompt(dt, blocks)
            img = get_store().get_bytes(page["storage_key"])
            schema = SCHEMA_FOR_DOC_TYPE[dt]
            raw_text = gw.generate(prompt, img, max_tokens=max_tokens_for(dt),
                                   json_schema=bundled_schema(schema))  # fmt: skip
            raw = extract_json(raw_text)
            repaired = repair_payload(json.loads(json.dumps(raw)), schema)
            row: dict[str, Any] = {
                "case": case["name"], "doc_type": dt, "seconds": round(time.time() - t0),
                "state": s["state"], "ocr_lines": len(blocks),
                "after": score(case, saved), "facts": facts,
                "saved_equals_repaired_raw": {k: v for k, v in saved.items()
                                              if not k.startswith("_")} == repaired,
                "lost_in_repair": lost_in_repair(raw, repaired),
                "not_on_page": not_on_page(saved, " ".join(b["text"] for b in blocks)),
                "raw_top_keys": sorted(raw)}  # fmt: skip
            if old is not None:  # the old path: no schema in the prompt, the old repair
                old_text = gw.generate(prompt, img, max_tokens=max_tokens_for(dt))
                try:
                    old_raw = extract_json(old_text)
                    old_rep = old.repair_payload(old_raw, schema)
                except ValueError:
                    old_raw, old_rep = {}, {}
                row["before"] = score(case, old_rep)
                row["before_raw_top_keys"] = sorted(old_raw)
                row["before_saved"] = old_rep
            row["after_saved"] = saved
            report.append(row)
            print(json.dumps({k: row[k] for k in row if k not in ("after_saved", "before_saved")},
                             ensure_ascii=False), flush=True)  # fmt: skip
    finally:
        with session_scope() as c:
            c.execute(text("UPDATE web_user SET active = false WHERE username = :u"), {"u": user})
            c.execute(
                text(
                    "DELETE FROM web_session WHERE user_id = "
                    "(SELECT id FROM web_user WHERE username = :u)"
                ),
                {"u": user},
            )
            repo.write_audit(
                c,
                actor="rx-check",
                action="update",
                entity="web_user",
                entity_id=user,
                detail={"active": False},
            )
    out = Path("/workspace/logs/extraction_check.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
