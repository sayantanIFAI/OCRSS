"""ABDM / NRCeS bundle conformance check (ARCHITECTURE §4.5, §6, TECH_STACK §10).

If ``java`` is on the PATH **and** the HL7 validator jar exists at ``CDI_FHIR_VALIDATOR_JAR``,
``validate`` runs ``java -jar validator_cli.jar <bundle> -version 4.0.1 -ig nrces.fhir.r4.ndhm``
and parses the OperationOutcome. Otherwise it falls back to the Python conformance checks below.
Deploying the JRE + jar + pinned IG package is backlog E6-S1; making S9 gate on it is E6-S2.

The Python fallback is **not** IG profile validation; it checks the document-bundle rules this
system relies on (§6): Bundle(type=document) with identifier + timestamp, Composition first with
the artifact profile and a coded type, subject/author/custodian present, every reference resolves
inside the bundle, every entry has a ``urn:uuid:`` fullUrl, and the Patient carries identifiers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cdi_adapter.config import get_settings
from cdi_adapter.fhir.resources import PROFILE_BASE, _refs


def hapi_available() -> bool:
    return shutil.which("java") is not None and Path(get_settings().fhir_validator_jar).is_file()


def _issue(sev: str, code: str, msg: str, loc: str = "") -> dict[str, str]:
    return {"severity": sev, "code": code, "location": loc, "message": msg}


def python_checks(bundle: dict[str, Any], artifact: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "document":
        out.append(_issue("error", "bundle-type", "must be Bundle(type=document)"))
    if not (bundle.get("identifier") or {}).get("value"):
        out.append(_issue("error", "bundle-identifier", "Bundle.identifier is required"))
    if not bundle.get("timestamp"):
        out.append(_issue("error", "bundle-timestamp", "Bundle.timestamp is required"))
    entries = bundle.get("entry") or []
    if not entries:
        return out + [_issue("error", "bundle-empty", "no entries")]
    urls = set()
    for i, e in enumerate(entries):
        url = e.get("fullUrl", "")
        if not url.startswith("urn:uuid:"):
            out.append(_issue("error", "fullurl", "fullUrl must be urn:uuid:", f"entry[{i}]"))
        urls.add(url)
    for i, e in enumerate(entries):
        for r in _refs(e.get("resource")):
            if r not in urls:
                out.append(_issue("error", "reference-unresolved", r, f"entry[{i}]"))
    comp = entries[0].get("resource", {})
    if comp.get("resourceType") != "Composition":
        return out + [_issue("error", "composition-first", "first entry must be Composition")]
    if PROFILE_BASE + artifact not in (comp.get("meta") or {}).get("profile", []):
        out.append(_issue("error", "composition-profile", f"Composition must claim {artifact}"))
    if not (comp.get("type") or {}).get("coding") and artifact != "WellnessRecord":
        out.append(_issue("error", "composition-type", "Composition.type must be coded"))
    for key in ("subject", "author", "date", "title", "status"):
        if not comp.get(key):
            out.append(_issue("error", f"composition-{key}", f"Composition.{key} is required"))
    if not comp.get("custodian"):
        out.append(_issue("warning", "composition-custodian", "Composition.custodian missing"))
    patients = [e["resource"] for e in entries if e["resource"].get("resourceType") == "Patient"]
    if len(patients) != 1:
        out.append(_issue("error", "patient-count", "exactly one Patient expected"))
    elif not patients[0].get("identifier"):
        out.append(_issue("error", "patient-identifier", "Patient has no identifier"))
    return out


def _run_hapi(bundle: dict[str, Any]) -> list[dict[str, str]]:  # pragma: no cover - needs a JRE
    s = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        src, oo = Path(tmp) / "bundle.json", Path(tmp) / "outcome.json"
        src.write_text(json.dumps(bundle), encoding="utf-8")
        ig = s.ig_package.split("#")[0]
        subprocess.run(["java", "-jar", s.fhir_validator_jar, str(src), "-version", "4.0.1",
                        "-ig", ig, "-output", str(oo)], capture_output=True, timeout=600,
                       check=False)  # fmt: skip
        outcome = json.loads(oo.read_text(encoding="utf-8"))
    return [_issue(i.get("severity", "error"), i.get("code", ""),
                   (i.get("details") or {}).get("text") or i.get("diagnostics", ""),
                   ",".join(i.get("expression") or i.get("location") or []))
            for i in outcome.get("issue", [])]  # fmt: skip


def validate(bundle: dict[str, Any], artifact: str) -> dict[str, Any]:
    if hapi_available():
        engine, issues = "hapi", _run_hapi(bundle)
    else:
        engine, issues = "python", python_checks(bundle, artifact)
    errors = [i for i in issues if i["severity"] in ("error", "fatal")]
    return {"engine": engine, "valid": not errors, "issues": issues}
