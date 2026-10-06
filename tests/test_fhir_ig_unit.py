"""E6-S1 / E6-S4 / E6-S5: pinned HL7 validator + ABDM IG, golden bundles, IG upgrade procedure."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from cdi_adapter.config import Settings
from cdi_adapter.fhir import package_cache as pc
from cdi_adapter.fhir import validate_abdm as va
from cdi_adapter.podmanifest import parse_versions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fhir_golden as fg  # noqa: E402
import fhir_ig  # noqa: E402

PINS = parse_versions((ROOT / "infra" / "runpod" / "versions.env").read_text(encoding="utf-8"))
LOCK = json.loads((ROOT / "infra" / "fhir" / "packages.lock").read_text(encoding="utf-8"))
BOOT = (ROOT / "infra" / "runpod" / "bootstrap_pod.sh").read_text(encoding="utf-8")


# ---------------------------------------------------------------- E6-S1: one pinned IG everywhere


def test_the_ig_is_the_registry_package_pinned_in_every_place() -> None:
    ig = PINS["FHIR_IG"]
    assert ig == "ndhm.in#6.5.0"  # the NRCeS ABDM IG's registry id; nrces.fhir.r4.ndhm is not one
    assert Settings.model_fields["ig_package"].default == ig
    assert LOCK["ig"] == ig and fg.read_baseline()["ig"] == ig
    for env in (".env.runpod", ".env.example"):
        assert f"\nCDI_IG_PACKAGE={ig}\n" in (ROOT / env).read_text(encoding="utf-8"), env
    assert f"`CDI_IG_PACKAGE` | `{ig}`" in (ROOT / "docs" / "ARCHITECTURE.md").read_text("utf-8")
    assert re.fullmatch(r"[0-9a-f]{64}", PINS["FHIR_IG_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", PINS["FHIR_VALIDATOR_SHA256"])
    assert LOCK["validator"] == PINS["FHIR_VALIDATOR_VERSION"]


def test_the_validator_honours_the_pinned_version_and_never_goes_online() -> None:
    cmd = va.hapi_command([Path("a.json"), Path("b.json")], Path("oo.json"), ig="ndhm.in#6.5.0",
                          package_home="/ws/fhir", java="java", jar="/ws/v.jar")  # fmt: skip
    ig = cmd[cmd.index("-ig") + 1]
    assert Path(ig).as_posix() == "/ws/fhir/ig/ndhm.in-6.5.0.tgz"  # the old code dropped '#6.5.0'
    assert cmd[cmd.index("-tx") + 1] == "n/a" and "-no-http-access" in cmd  # §12: no calls out
    assert "-Dhttps.proxyHost=127.0.0.1" in cmd and "-Dhttps.proxyPort=9" in cmd
    assert f"-Duser.home={Path('/ws/fhir')}" in cmd
    assert cmd[cmd.index("-jar") + 1 : cmd.index("-jar") + 4] == ["/ws/v.jar", "a.json", "b.json"]
    with pytest.raises(ValueError):
        va.ig_tarball("ndhm.in", "/ws")  # an unpinned IG is refused


def test_parse_outcomes_single_and_batched() -> None:
    issue = {"severity": "error", "code": "structure", "details": {"text": "boom"},
             "expression": ["Bundle.entry[0]"]}  # fmt: skip
    one = va.parse_outcomes({"resourceType": "OperationOutcome", "issue": [issue]}, [Path("x")])
    assert one == {"x": [va._issue("error", "structure", "boom", "Bundle.entry[0]")]}
    files = [Path("/g/A.json"), Path("/g/B.json")]
    batch = {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "OperationOutcome", "issue": [issue],
                      "extension": [{"url": va._FILE_EXT, "valueString": "C:\\x\\A.json"}]}},
        {"resource": {"resourceType": "OperationOutcome", "issue": [],
                      "extension": [{"url": va._FILE_EXT, "valueString": "/g/B.json"}]}},
    ]}  # fmt: skip
    got = va.parse_outcomes(batch, files)
    assert len(got[str(files[0])]) == 1 and got[str(files[1])] == []
    with pytest.raises(ValueError):
        va.parse_outcomes({"resourceType": "Bundle", "entry": batch["entry"][:1]}, files)


def test_real_validator_only_when_jar_java_and_pinned_ig_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "v.jar"
    jar.write_bytes(b"jar")
    s = Settings(_env_file=None, fhir_validator_jar=str(jar), fhir_package_home=str(tmp_path),
                 fhir_java=sys.executable)  # type: ignore[call-arg]  # fmt: skip
    monkeypatch.setattr(va, "get_settings", lambda: s)
    assert va.hapi_available() is False  # no IG tarball yet: Python fallback
    va.ig_tarball(s.ig_package, tmp_path).parent.mkdir(parents=True)
    va.ig_tarball(s.ig_package, tmp_path).write_bytes(b"tgz")
    assert va.hapi_available() is True
    monkeypatch.setattr(va, "_run_hapi", lambda b: [va._issue("error", "x", "IG says no")])
    out = va.validate({"resourceType": "Bundle"}, "PrescriptionRecord")
    assert out == {
        "engine": "hapi",
        "valid": False,
        "issues": [va._issue("error", "x", "IG says no")],
    }


def test_bootstrap_installs_and_proves_the_validator_stack() -> None:
    assert "openjdk-21-jre-headless=" in PINS["APT_PINS"]
    step = BOOT[BOOT.index("5b. HL7 validator") :]
    assert '"${CDI_IG_PACKAGE:-}" != "$FHIR_IG"' in step and "exit 1" in step
    assert 'echo "$FHIR_VALIDATOR_SHA256  $FHIR_JAR.partial" | sha256sum -c -' in step
    assert "releases/download/$FHIR_VALIDATOR_VERSION/validator_cli.jar" in step
    assert 'scripts/fhir_ig.py" seed --home "$FHIR_HOME"' in step
    assert BOOT.index("5b. HL7 validator") < BOOT.index("7. version manifest")


# ---------------------------------------------------------------- package cache pins


def _pkg(home: Path, name: str, files: dict[str, str]) -> None:
    for rel, body in files.items():
        f = pc.packages_dir(home) / name / "package" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")


def test_digest_ignores_generated_indexes_and_temp_folders(tmp_path: Path) -> None:
    _pkg(tmp_path, "a.b#1.0.0", {"package.json": "{}", "X.json": "1"})
    first = pc.manifest(tmp_path)
    _pkg(tmp_path, "a.b#1.0.0", {".index.json": "generated by the validator"})
    _pkg(tmp_path, "0f7b9ea0-e86a-4088-918c-b64ba45551fe", {"package.json": "{}"})  # temp dir
    (pc.packages_dir(tmp_path) / ".a.b#2.0.0.partial").mkdir()
    assert pc.manifest(tmp_path) == first and list(first) == ["a.b#1.0.0"]
    _pkg(tmp_path, "a.b#1.0.0", {"X.json": "2"})
    assert pc.manifest(tmp_path)["a.b#1.0.0"] != first["a.b#1.0.0"]  # any content change shows


def test_check_names_every_kind_of_drift() -> None:
    pinned = {"a#1": "d1", "b#1": "d2", "c#1": "d3"}
    found = {"a#1": "d1", "b#1": "XX", "z#9": "d9"}
    assert pc.check(found, pinned) == [
        "missing: c#1", "not pinned: z#9", "changed: b#1 (digest XX..., pinned d2...)"]  # fmt: skip
    assert pc.check(pinned, pinned) == []


def test_the_lock_names_exact_versions_with_digests() -> None:
    pkgs = LOCK["packages"]
    assert "hl7.fhir.r4.core#4.0.1" in pkgs  # the IG's own dependency (package.json)
    for name, digest in pkgs.items():
        assert re.fullmatch(r"[a-z0-9.-]+#\d+\.\d+\.\d+", name) and re.fullmatch(
            "[0-9a-f]{64}", digest
        ), name
    assert not any(n.startswith("ndhm.in#") for n in pkgs)  # the IG itself is the pinned tarball


# ---------------------------------------------------------------- seed (install exactly the lock)


def _tgz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_unpacks_a_package_and_refuses_unsafe_ones(tmp_path: Path) -> None:
    dest = fhir_ig.install(_tgz({"package/package.json": "{}", "package/A.json": "1"}), tmp_path,
                           "a.b#1.0.0")  # fmt: skip
    assert (dest / "package" / "A.json").read_text() == "1"
    assert (pc.packages_dir(tmp_path) / "packages.ini").read_text().startswith("[cache]")
    with pytest.raises(ValueError, match="unsafe"):
        fhir_ig.install(_tgz({"package/../../evil.json": "x"}), tmp_path, "e#1")
    with pytest.raises(ValueError, match="not a FHIR package"):
        fhir_ig.install(_tgz({"readme.txt": "x"}), tmp_path, "n#1")


def test_seed_installs_exactly_the_lock_and_proves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tarballs = {("a.b", "1.0.0"): _tgz({"package/package.json": '{"name":"a.b"}'}),
                ("c.d", "2.0.0"): _tgz({"package/package.json": '{"name":"c.d"}'})}  # fmt: skip
    src = tmp_path / "src"
    for (p, v), t in tarballs.items():
        fhir_ig.install(t, src, f"{p}#{v}")
    lock = tmp_path / "packages.lock"
    pc.write_lock(lock, pc.manifest(src), ig="x#1", validator="6.10.4")
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(fhir_ig, "fetch", lambda p, v: fetched.append((p, v)) or tarballs[(p, v)])

    home = tmp_path / "home"
    assert fhir_ig.seed(home, lock) == [] and sorted(fetched) == sorted(tarballs)
    fetched.clear()
    assert fhir_ig.seed(home, lock) == [] and fetched == []  # a second boot downloads nothing

    tarballs[("c.d", "2.0.0")] = _tgz({"package/package.json": '{"name":"tampered"}'})
    _pkg(home, "c.d#2.0.0", {"package.json": "changed on disk"})
    assert fhir_ig.seed(home, lock) == ["changed: c.d#2.0.0 (digest " +
                                        pc.manifest(home)["c.d#2.0.0"][:12] + "..., pinned " +
                                        pc.read_lock(lock)["c.d#2.0.0"][:12] + "...)"]  # fmt: skip


def test_put_ig_refuses_a_tarball_that_is_not_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fhir_ig, "fetch", lambda p, v: b"not the real IG")
    with pytest.raises(SystemExit, match="DRIFT"):
        fhir_ig.put_ig("ndhm.in#6.5.0", tmp_path, PINS["FHIR_IG_SHA256"])
    path = fhir_ig.put_ig("ndhm.in#6.5.0", tmp_path, fhir_ig.sha256_bytes(b"not the real IG"))
    assert path.name == "ndhm.in-6.5.0.tgz" and path.parent.name == "ig"


# ---------------------------------------------------------------- E6-S4: golden bundles


def test_goldens_are_committed_and_match_the_builders() -> None:
    assert fg.check() == []  # the real assembly code still produces every golden byte-for-byte
    for a in fg.ARTIFACTS:
        b = json.loads(fg.path_for(a).read_text(encoding="utf-8"))
        comp = b["entry"][0]["resource"]
        assert b["type"] == "document" and comp["resourceType"] == "Composition"
        profile = a.split("-")[0]  # "<artifact>-<variant>": the same ABDM record type
        assert comp["meta"]["profile"] == [
            f"https://nrces.in/ndhm/fhir/r4/StructureDefinition/{profile}"
        ]


def test_every_kind_of_upload_has_a_golden_that_the_real_validator_passes() -> None:
    """2026-09-28 (owner): every upload type must give a valid ABDM FHIR record. The goldens cover
    every artifact S9 emits, lab and imaging reports and the HealthDocumentRecord fallback, and
    the HL7 validator reports NO error on any of them: the baseline is empty and stays empty."""
    from cdi_adapter.fhir.resources import ARTIFACT, DEFAULT_ARTIFACT

    emitted = {a for a, _, _ in ARTIFACT.values()} | {DEFAULT_ARTIFACT[0]}
    assert emitted == {a.split("-")[0] for a in fg.ARTIFACTS}
    assert "DiagnosticReportRecord-Imaging" in fg.ARTIFACTS
    assert fg.read_baseline()["errors"] == {a: [] for a in fg.ARTIFACTS}


def test_goldens_are_deterministic_and_carry_no_real_data() -> None:
    for a in fg.ARTIFACTS:
        assert fg.dumps(fg.build(a)) == fg.dumps(fg.build(a))
    text = "".join(fg.path_for(a).read_text(encoding="utf-8") for a in fg.ARTIFACTS)
    assert "91-1234-5678-9012" in text  # the synthetic ABHA number, never a real one


def test_a_builder_change_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    from cdi_adapter.fhir import resources as R

    real = R.practitioner
    monkeypatch.setattr(R, "practitioner", lambda name, ref: {**real(name, ref), "gender": "x"})
    assert fg.check() == list(fg.ARTIFACTS)  # every golden now differs: the build fails


def test_error_signatures_are_stable_and_the_baseline_only_shrinks() -> None:
    i1 = {"severity": "error", "location": "Bundle.entry[0].resource/*Composition/"
          "e2ff5422-751e-54f6-831c-f6c58a5b929b*/.subject",
          "message": "no match for urn:uuid:3b373192-2a50-5879-b4f4-d35451854e76"}  # fmt: skip
    i2 = {**i1, "location": i1["location"].replace("e2ff5422", "aaaaaaaa"),
          "message": i1["message"].replace("3b373192", "bbbbbbbb")}  # fmt: skip
    assert fg.signature(i1) == fg.signature(i2) == (
        "Bundle.entry[0].resource<id>.subject :: no match for <id>")  # fmt: skip
    base = {"PrescriptionRecord": ["e1", "e1", "e2"]}
    assert fg.compare({"PrescriptionRecord": ["e1", "e1", "e2"]}, base) == ({}, {})
    new, fixed = fg.compare({"PrescriptionRecord": ["e1", "e2", "e3"]}, base)
    assert new == {"PrescriptionRecord": ["e3"]} and fixed == {"PrescriptionRecord": ["e1"]}


def test_the_baseline_records_every_golden_under_the_pinned_ig() -> None:
    base = fg.read_baseline()
    assert set(base["errors"]) == set(fg.ARTIFACTS) and base["validator"] == LOCK["validator"]
    assert all(sig.count(" :: ") == 1 and "urn:uuid:" not in sig
               for sigs in base["errors"].values() for sig in sigs)  # fmt: skip


# ---------------------------------------------------------------- E6-S5: stage -> promote


@pytest.fixture
def repo_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copies of the pin files, so promote can rewrite them without touching the repo."""
    root = tmp_path / "repo"
    for rel in ("infra/fhir/packages.lock", "infra/runpod/versions.env", ".env.runpod",
                ".env.example"):  # fmt: skip
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(fhir_ig, "LOCK", root / "infra/fhir/packages.lock")
    monkeypatch.setattr(fhir_ig, "VERSIONS", root / "infra/runpod/versions.env")
    monkeypatch.setattr(fhir_ig, "ENV_FILES", (root / ".env.runpod", root / ".env.example"))
    return root


def _staged(home: Path, *, ig: str, passed: bool, new: list[str] | None = None) -> None:
    _pkg(home, "hl7.fhir.r4.core#4.0.1", {"package.json": "{}"})
    report: dict[str, Any] = {
        "ig": ig, "ig_sha256": "a" * 64, "passed": passed, "goldens_match_builders": True,
        "results": {a: {"errors": 0, "new_errors": new or [], "fixed_errors": []}
                    for a in fg.ARTIFACTS}, "packages": pc.manifest(home)}  # fmt: skip
    (home / "stage-report.json").write_text(json.dumps(report), encoding="utf-8")


def test_promote_refuses_without_a_passing_stage_for_that_exact_ig(
    tmp_path: Path, repo_pins: Path
) -> None:
    home = tmp_path / "staging"
    home.mkdir()
    with pytest.raises(SystemExit, match="no stage report"):
        fhir_ig.promote("ndhm.in#6.6.0", home, validator="6.10.4")
    _staged(home, ig="ndhm.in#6.6.0", passed=False, new=["x :: new error"])
    with pytest.raises(SystemExit, match="failed staging"):
        fhir_ig.promote("ndhm.in#6.6.0", home, validator="6.10.4")
    _staged(home, ig="ndhm.in#6.6.0", passed=True)
    with pytest.raises(SystemExit, match="staged IG is ndhm.in#6.6.0"):
        fhir_ig.promote("ndhm.in#7.0.0", home, validator="6.10.4")
    _pkg(home, "hl7.fhir.r4.core#4.0.1", {"package.json": "edited after staging"})
    with pytest.raises(SystemExit, match="changed after staging"):
        fhir_ig.promote("ndhm.in#6.6.0", home, validator="6.10.4")
    assert json.loads((repo_pins / "infra/fhir/packages.lock").read_text())["ig"] == "ndhm.in#6.5.0"


def test_promote_rewrites_every_pin_together(tmp_path: Path, repo_pins: Path) -> None:
    home = tmp_path / "staging"
    _staged(home, ig="ndhm.in#6.6.0", passed=True)
    fhir_ig.promote("ndhm.in#6.6.0", home, validator="6.10.4")
    lock = json.loads((repo_pins / "infra/fhir/packages.lock").read_text())
    assert lock["ig"] == "ndhm.in#6.6.0" and lock["packages"] == pc.manifest(home)
    pins = parse_versions((repo_pins / "infra/runpod/versions.env").read_text(encoding="utf-8"))
    assert pins["FHIR_IG"] == "ndhm.in#6.6.0" and pins["FHIR_IG_SHA256"] == "a" * 64
    for env in (".env.runpod", ".env.example"):
        assert "\nCDI_IG_PACKAGE=ndhm.in#6.6.0\n" in (repo_pins / env).read_text(), env
    assert pins["FHIR_VALIDATOR_VERSION"] == PINS["FHIR_VALIDATOR_VERSION"]  # untouched


def test_promote_dry_run_lists_the_changes_and_writes_nothing(
    tmp_path: Path, repo_pins: Path
) -> None:
    before = {p: p.read_bytes() for p in repo_pins.rglob("*") if p.is_file()}
    home = tmp_path / "staging"
    _staged(home, ig="ndhm.in#6.6.0", passed=True)
    changes = fhir_ig.promote("ndhm.in#6.6.0", home, validator="6.10.4", dry_run=True)
    assert "versions.env: FHIR_IG=ndhm.in#6.6.0" in changes
    assert ".env.runpod: CDI_IG_PACKAGE=ndhm.in#6.6.0" in changes
    assert any(
        c.startswith("packages.lock: not pinned: hl7.fhir.r4.core#4.0.1")
        or c.startswith("packages.lock: changed: hl7.fhir.r4.core#4.0.1")
        for c in changes
    )
    assert {p: p.read_bytes() for p in repo_pins.rglob("*") if p.is_file()} == before


def test_staging_never_targets_the_production_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from cdi_adapter import config

    prod = str(ROOT / ".tools" / "prod-fhir-home")
    monkeypatch.setattr(
        config, "get_settings", lambda: Settings(_env_file=None, fhir_package_home=prod)
    )  # type: ignore[call-arg]  # noqa: E501
    with pytest.raises(SystemExit, match="never production"):
        fhir_ig.main(["stage", "--ig", "ndhm.in#6.6.0", "--home", prod, "--jar", "v.jar"])


# ---------------------------------------------------------------- the real validator (opt-in)


@pytest.mark.skipif(not os.environ.get("CDI_TEST_FHIR_VALIDATOR"),
                    reason="set CDI_TEST_FHIR_VALIDATOR=1 with the E6-S1 stack (JVM start-up: minutes)")  # fmt: skip  # noqa: E501
def test_goldens_gain_no_error_under_the_real_validator() -> None:
    assert fg.main(["--validate"]) == 0


def test_digest_order_is_identical_on_windows_and_linux(tmp_path):
    import hashlib

    root = tmp_path / "package"
    root.mkdir()
    for name in ("a.json", "B.json", "C.json"):
        (root / name).write_bytes(name.encode())
    expected = hashlib.sha256()
    for name in ("a.json", "B.json", "C.json"):
        expected.update(name.encode() + b"\0")
        expected.update(hashlib.sha256(name.encode()).digest())
    assert pc.digest_package(tmp_path) == expected.hexdigest()
