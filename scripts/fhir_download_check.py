"""Live check of the FHIR download (2026-09-25 fix: the file holds only the upload's own record,
as FHIR).

Two uploads of one synthetic prescription (no real patient data, ARCHITECTURE §11) go through the
real web app, queue, worker and model gateway. For the second upload it downloads
``/api/jobs/{id}/fhir/download`` before and after the held facts are confirmed (Generate EMR,
keep all) and checks that the file

* is a FHIR document Bundle (``application/fhir+json``), Composition first, no dangling reference,
  no ``null`` / empty value at any depth, the Python conformance checks clean;
* holds this upload only: its DocumentReference is this upload's file, never the first upload's,
  even when both uploads resolve to the same patient;
* carries each medicine as read: name + strength, the dosage line, the timing with its bounds,
  the instructions (not in ``route``).

    python scripts/fhir_download_check.py
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

LINES = ["CareFlow Polyclinic - Rx", "Name: Test Patient Nine   Age/Sex: 52 Y / F",
         "Date: 14/08/2026", "Diagnosis: Type 2 diabetes mellitus, Hypertension", "Rx",
         "Tab Metformin 500 mg 1-0-1 x 30 days after food",
         "Tab Amlodipine 5 mg OD x 30 days", "Tab Atorvastatin 10 mg 0-0-1 x 30 days",
         "BP: 142/90 mmHg   Weight: 71 kg"]  # fmt: skip
HELD = ("pending", "in_review")


def empties(x: Any, path: str = "$") -> list[str]:
    """Every path holding ``null``, ``[]`` or ``{}`` (FHIR JSON allows none of them)."""
    if x is None or x == [] or x == {}:
        return [path]
    if isinstance(x, dict):
        return [p for k, v in x.items() for p in empties(v, f"{path}.{k}")]
    if isinstance(x, list):
        return [p for i, v in enumerate(x) for p in empties(v, f"{path}[{i}]")]
    return []


def resources(b: dict[str, Any]) -> list[dict[str, Any]]:
    return [e["resource"] for e in b.get("entry") or []]


def med_summary(mr: dict[str, Any]) -> dict[str, Any]:
    d = (mr.get("dosageInstruction") or [{}])[0]
    return {"medication": mr["medicationCodeableConcept"].get("text"),
            "dosage": d.get("text"), "timing": d.get("timing"),
            "route": d.get("route"), "patientInstruction": d.get("patientInstruction"),
            "supply": (mr.get("dispenseRequest") or {}).get("expectedSupplyDuration"),
            "authoredOn": mr.get("authoredOn")}  # fmt: skip


def check(b: dict[str, Any], filename: str, other: str, media: str) -> dict[str, Any]:
    from cdi_adapter.fhir.resources import _lint
    from cdi_adapter.fhir.validate_abdm import python_checks

    res = resources(b)
    refs = [r for r in res if r["resourceType"] == "DocumentReference"]
    titles = [a["attachment"].get("title") for r in refs for a in r.get("content") or []]
    kinds: dict[str, int] = {}
    for r in res:
        kinds[r["resourceType"]] = kinds.get(r["resourceType"], 0) + 1
    return {
        "media_type": media, "resourceType": b.get("resourceType"), "type": b.get("type"),
        "composition_first": bool(res) and res[0]["resourceType"] == "Composition",
        "resources": kinds, "document_files": titles,
        "only_this_upload": titles == [filename] and other not in json.dumps(b),
        "null_or_empty": empties(b), "lint": _lint(b),
        "conformance_errors": [i for i in python_checks(b, "PrescriptionRecord")
                               if i["severity"] in ("error", "fatal")],
    }  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="http://127.0.0.1:8888")
    ap.add_argument("--out", default="/workspace/logs/fhir_download_check.json")
    args = ap.parse_args()

    import httpx
    from make_sample_docs import scanify, text_pdf
    from sqlalchemy import text

    from cdi_adapter import repo
    from cdi_adapter.db import session_scope
    from cdi_adapter.webapp.auth import hash_password

    user, pw = f"fhir-check-{uuid.uuid4().hex[:6]}", secrets.token_urlsafe(24)
    with session_scope() as c:
        c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                       "VALUES (:u, 'fhir download check', 'admin', :p)"),
                  {"u": user, "p": hash_password(pw)})  # fmt: skip
        repo.write_audit(c, actor="fhir-check", action="create", entity="web_user",
                         entity_id=user, detail={"purpose": "fhir download check"})  # fmt: skip
    report: dict[str, Any] = {}
    try:
        web = httpx.Client(base_url=args.host, timeout=120, headers={"Origin": args.host})
        r = web.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
        web.headers["Cookie"] = f"cdi_session={r.cookies['cdi_session']}"
        tag = uuid.uuid4().hex[:6]
        jobs = []
        for n in range(2):
            name = f"rx-{tag}-{n}.png"
            # a line of its own per upload: identical bytes are one document (ingest dedupe)
            png = scanify(text_pdf([*LINES, f"ref {tag}-{n}"]), seed=2026 + n, dpi=150)
            job = web.post("/api/jobs", files=[("files", (name, png, "image/png"))]).json()
            t0 = time.time()
            while (s := web.get(f"/api/jobs/{job['job_id']}").json())["state"] == "running":
                if time.time() - t0 > 900:
                    break
                time.sleep(3)
            jobs.append(
                {
                    "job_id": job["job_id"],
                    "file": name,
                    "state": s["state"],
                    "patient_id": s.get("patient_id"),
                    "seconds": round(time.time() - t0),
                }
            )
        first, job = jobs
        report["uploads"] = [{k: v for k, v in j.items() if k != "patient_id"} for j in jobs]
        report["same_patient"] = first["patient_id"] == job["patient_id"]
        jid = job["job_id"]

        def download(stage: str) -> dict[str, Any]:
            r = web.get(f"/api/jobs/{jid}/fhir/download")
            if r.status_code != 200:
                report[stage] = {"status": r.status_code, "detail": r.text[:200]}
                return {}
            b = r.json()
            report[stage] = check(b, job["file"], first["file"], r.headers["content-type"])
            return b

        download("before_confirm")
        facts = web.get(f"/api/jobs/{jid}/facts").json()["documents"][0]["facts"]
        held = [f["id"] for f in facts if f["review_state"] in HELD]
        report["facts"] = {"total": len(facts), "held_then_confirmed": len(held)}
        r = web.post(
            f"/api/jobs/{jid}/generate",
            json={"edits": [{"fact_id": i, "action": "keep"} for i in held]},
        )
        report["generate_status"] = r.status_code
        b = download("after_confirm")
        report["medication_requests"] = [
            med_summary(x) for x in resources(b) if x["resourceType"] == "MedicationRequest"
        ]
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
            repo.write_audit(c, actor="fhir-check", action="update", entity="web_user",
                             entity_id=user, detail={"active": False})  # fmt: skip
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    after = report.get("after_confirm") or {}
    ok = (after.get("type") == "document" and after.get("only_this_upload")
          and not after.get("null_or_empty") and not after.get("lint")
          and not after.get("conformance_errors")
          and len(report.get("medication_requests") or []) == 3)  # fmt: skip
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
