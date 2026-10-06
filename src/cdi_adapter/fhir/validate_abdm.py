"""ABDM / NRCeS bundle conformance check (ARCHITECTURE §4.5, §6, TECH_STACK §10).

If ``CDI_FHIR_JAVA`` is on the PATH, the HL7 validator jar exists at ``CDI_FHIR_VALIDATOR_JAR`` and
the pinned IG tarball is in ``CDI_FHIR_PACKAGE_HOME`` (backlog E6-S1), ``validate`` runs the real
validator against ``CDI_IG_PACKAGE`` (id#version, the version is honoured) fully offline and
parses the OperationOutcome. Otherwise it falls back to the Python conformance checks below.
Making S9 gate on the real validator is E6-S2.

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
from pathlib import Path, PureWindowsPath
from typing import Any

from cdi_adapter.config import get_settings
from cdi_adapter.fhir.resources import PROFILE_BASE, _refs


def hapi_available() -> bool:
    s = get_settings()
    try:
        tarball = ig_tarball(s.ig_package, s.fhir_package_home)
    except ValueError:
        return False
    return (shutil.which(s.fhir_java) is not None and Path(s.fhir_validator_jar).is_file()
            and tarball.is_file())  # fmt: skip


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


_FILE_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-file"


def ig_tarball(ig: str, package_home: str | Path) -> Path:
    """Where the pinned IG tarball lives: ``<package home>/ig/<id>-<version>.tgz`` (E6-S1)."""
    pkg, sep, version = ig.partition("#")
    if not sep or not version:
        raise ValueError(f"IG package must be pinned as id#version, got {ig!r}")
    return Path(package_home) / "ig" / f"{pkg}-{version}.tgz"


def hapi_command(files: list[Path], output: Path, *, ig: str, package_home: str | Path,
                 java: str, jar: str) -> list[str]:  # fmt: skip
    """The validator call: the pinned IG tarball, the frozen package cache, never the network.

    ``-tx n/a`` + ``-no-http-access``: no terminology server and no package download (§12: no
    external calls in the hot path), and a proxy on a closed port as a second fence.
    ``-Duser.home`` points the validator at our package cache instead of ``~/.fhir``.
    """
    home = Path(package_home)
    return [java, "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=9",
            "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=9", f"-Duser.home={home}",
            "-jar", jar, *[str(f) for f in files], "-version", "4.0.1",
            "-ig", str(ig_tarball(ig, home)), "-tx", "n/a", "-no-http-access",
            "-txCache", str(home / "txcache"), "-output", str(output)]  # fmt: skip


def _issues(outcome: dict[str, Any]) -> list[dict[str, str]]:
    return [_issue(i.get("severity", "error"), i.get("code", ""),
                   (i.get("details") or {}).get("text") or i.get("diagnostics", ""),
                   ",".join(i.get("expression") or i.get("location") or []))
            for i in outcome.get("issue", [])]  # fmt: skip


def parse_outcomes(result: dict[str, Any], files: list[Path]) -> dict[str, list[dict[str, str]]]:
    """Issues per input file: one OperationOutcome, or a Bundle of them tagged with their file."""
    if result.get("resourceType") == "OperationOutcome":
        return {str(files[0]): _issues(result)}
    out: dict[str, list[dict[str, str]]] = {}
    by_name = {PureWindowsPath(f).name: str(f) for f in files}
    for entry in result.get("entry", []):
        oo = entry.get("resource", {})
        tag = next((e.get("valueString") for e in oo.get("extension", [])
                    if e.get("url") == _FILE_EXT), None)  # fmt: skip
        # the validator reports paths the way its OS writes them; PureWindowsPath splits a file
        # name off both "/" and "\" paths, so this works whichever OS produced the output
        key = by_name.get(PureWindowsPath(tag).name, tag) if tag else None
        if key is None:
            raise ValueError("validator output entry without its source file")
        out[key] = _issues(oo)
    missing = [str(f) for f in files if str(f) not in out]
    if missing:
        raise ValueError(f"validator gave no outcome for {missing}")
    return out


def validate_files(
    files: list[Path],
    *,
    ig: str | None = None,
    package_home: str | Path | None = None,
    timeout_s: int = 3600,
) -> dict[str, Any]:  # pragma: no cover - needs a JRE
    """Run the real HL7 validator once over ``files`` (JVM start-up dominates, so batch)."""
    s = get_settings()
    ig = ig or s.ig_package
    home = Path(package_home or s.fhir_package_home)
    with tempfile.TemporaryDirectory() as tmp:
        oo = Path(tmp) / "outcome.json"
        cmd = hapi_command(files, oo, ig=ig, package_home=home, java=s.fhir_java,
                           jar=s.fhir_validator_jar)  # fmt: skip
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        if not oo.is_file():
            raise RuntimeError(f"validator produced no output (exit {run.returncode}): "
                               f"{run.stdout[-800:]}{run.stderr[-800:]}")  # fmt: skip
        result = json.loads(oo.read_text(encoding="utf-8"))
    version = next((ln.split("Version ", 1)[1].split()[0] for ln in run.stdout.splitlines()
                    if "FHIR Validation tool Version" in ln), "?")  # fmt: skip
    return {"validator": version, "ig": ig, "files": parse_outcomes(result, files)}


def _run_hapi(bundle: dict[str, Any]) -> list[dict[str, str]]:  # pragma: no cover - needs a JRE
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "bundle.json"
        src.write_text(json.dumps(bundle), encoding="utf-8")
        return validate_files([src], timeout_s=600)["files"][str(src)]


def validate(bundle: dict[str, Any], artifact: str) -> dict[str, Any]:
    if hapi_available():
        engine, issues = "hapi", _run_hapi(bundle)
    else:
        engine, issues = "python", python_checks(bundle, artifact)
    errors = [i for i in issues if i["severity"] in ("error", "fatal")]
    return {"engine": engine, "valid": not errors, "issues": issues}
