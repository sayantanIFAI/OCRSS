"""E1-S4: idempotent, version-pinned pod bootstrap + drift detection."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from cdi_adapter import podmanifest as pm
from tests.test_ops_unit import _bash

ROOT = Path(__file__).resolve().parents[1]
RUNPOD = ROOT / "infra" / "runpod"
BOOT = (RUNPOD / "bootstrap_pod.sh").read_text(encoding="utf-8")
VERSIONS_TEXT = (RUNPOD / "versions.env").read_text(encoding="utf-8")
VERSIONS = pm.parse_versions(VERSIONS_TEXT)
LOCK_TEXT = (RUNPOD / "requirements.lock").read_text(encoding="utf-8")
LOCK = pm.parse_lock(LOCK_TEXT)
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "scripts"))
import lock_pod_deps  # noqa: E402


def _code(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------- versions.env


def test_bash_and_python_read_versions_env_identically() -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    src = (RUNPOD / "versions.env").as_posix()
    script = f". '{src}'; for k in {' '.join(VERSIONS)}; do echo \"$k=${{!k}}\"; done"
    r = subprocess.run([bash, "-c", script], capture_output=True, text=True, env={})
    assert r.returncode == 0, r.stderr
    from_bash = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert from_bash == VERSIONS


def test_every_pin_is_exact() -> None:
    assert re.fullmatch(r"\d{8}T\d{6}Z", VERSIONS["UBUNTU_SNAPSHOT"])
    assert VERSIONS["UBUNTU_SERIES"] == "noble"  # Ubuntu 24.04 (ARCHITECTURE §3.3)
    assert re.fullmatch(r"[0-9a-f]{64}", VERSIONS["GO_SHA256"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSIONS["GO_VERSION"])
    assert re.fullmatch(r"RELEASE\.\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ", VERSIONS["MINIO_TAG"])
    assert re.fullmatch(r"[0-9a-f]{40}", VERSIONS["MINIO_COMMIT"])
    assert VERSIONS["PYTHON_SERIES"] == "3.12"  # App. B
    for pkg, ver in pm.apt_pins(VERSIONS).items():  # "~" is plain Debian version syntax
        assert not any(c in ver for c in "*<> "), (pkg, ver)


def test_apt_pins_cover_the_packages_the_bootstrap_needs() -> None:
    need = {"postgresql-16", "postgresql-client-16", "redis-server", "cron", "git", "curl",
            "libcurl4t64", "libgl1", "libglib2.0-0t64", "python3-venv", "autossh",
            "openjdk-21-jre-headless"}  # fmt: skip
    assert set(pm.apt_pins(VERSIONS)) == need
    # curl needs its library at exactly its own version; an image with a newer one held it back
    assert pm.apt_pins(VERSIONS)["libcurl4t64"] == pm.apt_pins(VERSIONS)["curl"]
    assert pm.apt_pins(VERSIONS)["postgresql-16"].startswith("16.")  # PostgreSQL 16 (App. B)
    assert pm.apt_pins(VERSIONS)["redis-server"].startswith("5:7.")  # Redis 7 (App. B)


def test_app_b_versions_are_the_pins() -> None:
    assert f"transformers | {VERSIONS['TRANSFORMERS_VERSION']} " in ARCH
    assert f"| {VERSIONS['TORCH_VERSION']} / {VERSIONS['TORCHVISION_VERSION']} " in ARCH
    assert f"Python | {VERSIONS['PYTHON_SERIES']} |" in ARCH


# ---------------------------------------------------------------- bootstrap_pod.sh


def test_pins_cannot_be_overridden_by_the_environment() -> None:
    assert BOOT.index('. "$HERE/versions.env"') > BOOT.index('. "$REPO/.env"')
    for key in VERSIONS:
        assert not re.search(rf"\b{key}=\$\{{{key}:-", BOOT), key  # no ${X:-default} for a pin
    assert "${GO_VERSION:-" not in BOOT and "${MINIO_TAG:-" not in BOOT


def test_apt_installs_only_the_pins_from_the_snapshot() -> None:
    installs = [ln for ln in _code(BOOT) if "apt-get" in ln and " install" in ln]
    assert installs and all("$APT_PINS" in ln and "${APT_SNAP[@]}" in ln for ln in installs)
    updates = [ln for ln in _code(BOOT) if "apt-get" in ln and " update" in ln]
    assert updates and all("${APT_SNAP[@]}" in ln for ln in updates)
    assert "https://snapshot.ubuntu.com/ubuntu/$UBUNTU_SNAPSHOT" in BOOT
    assert '"Dir::Etc::SourceParts=-"' in BOOT  # the image's own (moving) sources are not used
    assert "signed-by=$KEYRING" in BOOT


def test_pip_never_resolves_on_the_pod() -> None:
    for ln in _code(BOOT):
        if "pip install" not in ln:
            continue
        pinned = "--require-hashes" in ln or "--no-deps" in ln or '"torch==$TORCH_VERSION"' in ln
        assert pinned, ln
        assert "--upgrade" not in ln, ln
    assert '--require-hashes --no-deps -r "$LOCK"' in BOOT
    assert '--no-deps --no-build-isolation -e "$REPO"' in BOOT
    assert '"$REPO[ocr,ml]"' not in BOOT  # the old unpinned install


def test_go_and_minio_are_pinned_end_to_end() -> None:
    assert 'echo "$GO_SHA256  /tmp/go.tgz" | sha256sum -c -' in BOOT
    assert 'if [ -n "$GO_SHA256" ]' not in BOOT  # the checksum is no longer optional
    assert '!= "go$GO_VERSION"' in BOOT  # a toolchain of another version is replaced
    assert '"$HEAD_COMMIT" != "$MINIO_COMMIT"' in BOOT
    assert "GOTOOLCHAIN=local" in BOOT and "-mod=readonly" in BOOT and "-trimpath" in BOOT
    assert 'mv "$MINIO_BIN.partial" "$MINIO_BIN"' in BOOT  # a failed build leaves no binary


def test_drift_check_runs_last_and_stops_the_boot() -> None:
    step = BOOT.index("cdi_adapter.podmanifest write")
    assert step > BOOT.index("crontab -") and step < BOOT.index("=== bootstrap done")
    tail = BOOT[step:]
    assert "exit 1" in tail and '"${CDI_ALLOW_DRIFT:-0}" = 1' in tail
    assert '--dir "$WS/logs/manifests"' in BOOT
    for other in ("start_all.sh", "start_mlserve.sh", "golden_nightly.sh"):
        assert "CDI_ALLOW_DRIFT" not in (RUNPOD / other).read_text(encoding="utf-8")
    assert "CDI_ALLOW_DRIFT" not in (ROOT / ".env.runpod").read_text(encoding="utf-8")


def _apt_pinned(installed: dict[str, str]) -> int:
    """Run bootstrap_pod.sh's own apt_pinned() against a fake dpkg-query."""
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    fn = re.search(r"^apt_pinned\(\) \{\n.*?^\}\n", BOOT, re.S | re.M)
    assert fn
    table = "\n".join(f"{k}) printf %s '{v}';;" for k, v in installed.items())
    fake = f'dpkg-query() {{ case "${{@: -1}}" in\n{table}\n*) return 1;;\nesac; }}\n'
    pins = VERSIONS["APT_PINS"]
    r = subprocess.run([bash, "-c", f"{fake}{fn.group(0)}APT_PINS='{pins}'\napt_pinned"],
                       capture_output=True, text=True)  # fmt: skip
    return r.returncode


def test_apt_step_is_skipped_only_when_every_pin_holds() -> None:
    pins = pm.apt_pins(VERSIONS)
    assert _apt_pinned(pins) == 0  # a second boot on the same overlay: nothing to do
    assert _apt_pinned({k: v for k, v in pins.items() if k != "cron"}) == 1  # missing
    drifted = {**pins, "postgresql-16": "16.2-1ubuntu4"}
    assert _apt_pinned(drifted) == 1  # another version installed -> reinstall the pin


# ---------------------------------------------------------------- requirements.lock


def test_lock_is_exact_and_hashed() -> None:
    blocks = re.split(r"\n(?=[a-z0-9])", LOCK_TEXT.split("\n\n", 1)[1].strip())
    assert len(blocks) == len(LOCK) > 50
    for block in blocks:
        first = block.splitlines()[0]
        if " @ " in first:  # E11-S4: the one wheel not on PyPI, pinned by URL + hash
            assert first == f"en-core-web-lg @ {VERSIONS['SPACY_MODEL_WHEEL']} \\", first
            assert f"--hash=sha256:{VERSIONS['SPACY_MODEL_SHA256']}" in block
            continue
        assert re.match(r"^[a-z0-9-]+==\S+ \\$", first), block
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", block), block


def test_lock_covers_every_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    reqs = [*project["dependencies"], *extras["ocr"], *extras["ml"], *extras["deid"]]
    for req in reqs:
        name = pm.normalize(re.match(r"[A-Za-z0-9._-]+", req).group(0))  # type: ignore[union-attr]
        assert name in LOCK, req
    for tool in ("pip", "setuptools", "wheel"):
        assert tool in LOCK, tool


def test_lock_matches_the_pins_and_the_target() -> None:
    assert LOCK["transformers"] == VERSIONS["TRANSFORMERS_VERSION"]
    assert not [n for n in LOCK if pm.TORCH_FAMILY.match(n)]  # image / cu128 build supplies it
    for windows_only in ("colorama", "pywin32", "pywin32-ctypes"):
        assert windows_only not in LOCK, windows_only
    header = LOCK_TEXT.split("\n\n", 1)[0]
    for con in lock_pod_deps.constraints(VERSIONS):
        assert con in header, con


# ---------------------------------------------------------------- lock_pod_deps.py


def test_constraints_drop_the_local_label() -> None:
    assert lock_pod_deps.constraints(VERSIONS) == [
        f"torch=={VERSIONS['TORCH_VERSION'].split('+')[0]}",
        f"torchvision=={VERSIONS['TORCHVISION_VERSION'].split('+')[0]}",
        f"transformers=={VERSIONS['TRANSFORMERS_VERSION']}",
    ]


def test_markers_are_the_pods_not_this_machines() -> None:
    env = lock_pod_deps.LINUX_MARKERS
    assert env["sys_platform"] == "linux" and env["platform_system"] == "Linux"
    assert env["platform_machine"] == "x86_64" and env["python_version"] == "3.12"
    from pip._vendor.packaging.markers import Marker

    assert not Marker('sys_platform == "win32"').evaluate(env)
    assert Marker('platform_system == "Linux" and python_version >= "3.12"').evaluate(env)


def test_compatible_hashes_keep_only_wheels_the_pod_can_install() -> None:
    def f(name: str, h: str, **kw: Any) -> dict[str, Any]:
        return {"filename": name, "digests": {"sha256": h}, **kw}

    files = [
        f("numpy-2.5.3-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl", "a"),
        f("numpy-2.5.3-cp312-cp312-win_amd64.whl", "b"),
        f("numpy-2.5.3-cp312-cp312-macosx_14_0_arm64.whl", "c"),
        f("numpy-2.5.3-cp313-cp313-manylinux_2_28_x86_64.whl", "d"),
        f("numpy-2.5.3-cp312-cp312-manylinux_2_28_aarch64.whl", "e"),
        f("numpy-2.5.3.tar.gz", "f"),
        f("pure-1.0-py3-none-any.whl", "g"),
        f("abi-1.0-cp310-abi3-manylinux_2_17_x86_64.whl", "h"),
        f("new-1.0-cp312-cp312-manylinux_2_40_x86_64.whl", "i"),  # needs glibc newer than 24.04
        f("gone-1.0-py3-none-any.whl", "j", yanked=True),
    ]
    got = lock_pod_deps.compatible_hashes(files, lock_pod_deps.supported_tags())
    assert got == ["a", "g", "h"]


def test_render_round_trips_through_the_parser() -> None:
    entries = [("alpha", "1.0", ["a" * 64]), ("beta-x", "2.0.post1", ["b" * 64, "c" * 64])]
    text = lock_pod_deps.render(entries, ["torch==2.8.0"])
    assert pm.parse_lock(text) == {"alpha": "1.0", "beta-x": "2.0.post1"}
    assert "beta-x==2.0.post1 \\\n    --hash=sha256:" + "b" * 64 + " \\\n" in text


# ---------------------------------------------------------------- podmanifest


def test_parsers_reject_loose_input() -> None:
    with pytest.raises(ValueError):
        pm.parse_versions("lowercase=1\n")
    with pytest.raises(ValueError):
        pm.apt_pins({"APT_PINS": "postgresql-16"})
    with pytest.raises(ValueError):
        pm.parse_lock("numpy>=2\n")
    assert pm.parse_versions('A="x y" # note\nB=1\n') == {"A": "x y", "B": "1"}
    # as bash: a mid-word "#" is data (id#version), a "#" starting a word is a comment
    assert pm.parse_versions("FHIR_IG=ndhm.in#6.5.0  # the IG\n") == {"FHIR_IG": "ndhm.in#6.5.0"}
    assert pm.normalize("PyYAML") == pm.normalize("pyyaml") == "pyyaml"
    assert pm.normalize("typing_extensions") == "typing-extensions"


def _good_manifest() -> dict[str, Any]:
    return {
        "generated_at": "2026-09-22T00:00:00+00:00",
        "host": {"kind": "runpod", "id": "pod-a"},
        "os": {"id": "ubuntu", "version_id": "24.04"},
        "python": "3.12.3",
        "apt": {**pm.apt_pins(VERSIONS), "libc6": "2.39-0ubuntu8.6"},
        "pip": {
            "venv": {**LOCK, "cdi-adapter": "0.1.0"},
            "system": {
                "torch": VERSIONS["TORCH_VERSION"],
                "torchvision": VERSIONS["TORCHVISION_VERSION"],
            },
        },
        "go": f"go{VERSIONS['GO_VERSION']}",
        "minio": {"tag": VERSIONS["MINIO_TAG"], "sha256": "0" * 64},
        "models": {"Qwen/Qwen2.5-VL-7B-Instruct": "cc594898137f460bfe9f0759e9844b3ce807cfb5"},
    }


def test_a_boot_that_matches_every_pin_has_no_drift() -> None:
    assert pm.check_pins(_good_manifest(), VERSIONS, LOCK, torch_required=True) == []


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (
            lambda m: m["apt"].update({"postgresql-16": "16.2-1ubuntu4"}),
            "apt: postgresql-16 is 16.2",
        ),
        (lambda m: m["apt"].pop("redis-server"), "apt: redis-server is not installed"),
        (lambda m: m.update(apt=None), "dpkg is not available"),
        (lambda m: m["pip"]["venv"].update(numpy="2.4.0"), "pip: numpy is 2.4.0 in the venv"),
        (lambda m: m["pip"]["venv"].pop("fastapi"), "pip: fastapi is not installed"),
        (
            lambda m: m["pip"]["venv"].update(requests2="1.0"),
            "requests2==1.0 is in the venv but not",
        ),
        (lambda m: m["pip"]["system"].update(torch="2.9.0+cu128"), "pip: torch is 2.9.0+cu128"),
        (lambda m: m["pip"].update(system={}), "torch is not installed, pinned"),
        (lambda m: m.update(go="go1.26.8"), "go: toolchain is go1.26.8"),
        (lambda m: m.update(python="3.13.1"), "python: 3.13.1 is not the pinned 3.12"),
        (lambda m: m.update(minio=None), "minio: no binary built"),
    ],
)
def test_each_kind_of_drift_is_reported(mutate: Any, expect: str) -> None:
    m = _good_manifest()
    mutate(m)
    drift = pm.check_pins(m, VERSIONS, LOCK, torch_required=True)
    assert len(drift) >= 1 and any(expect in line for line in drift), drift


def test_torch_local_label_and_stub_hosts() -> None:
    m = _good_manifest()
    m["pip"]["system"] = {"torch": "2.8.0", "torchvision": "0.23.0"}  # same release, no +cu128
    assert pm.check_pins(m, VERSIONS, LOCK, torch_required=True) == []
    m["pip"]["system"] = {}  # stub backend: no torch needed
    assert pm.check_pins(m, VERSIONS, LOCK, torch_required=False) == []
    m["pip"]["venv"]["nvidia-cudnn-cu12"] = "9.10.2.21"  # the cu128 build's own deps are allowed
    assert pm.check_pins(m, VERSIONS, LOCK, torch_required=False) == []


def test_diff_ignores_when_and_where_but_not_what() -> None:
    a = _good_manifest()
    b = json.loads(json.dumps(a))
    b["generated_at"] = "2026-09-23T00:00:00+00:00"
    b["host"] = {"kind": "runpod", "id": "pod-b"}
    assert pm.diff(a, b) == []  # two fresh pods, same versions
    b["apt"]["libc6"] = "2.39-0ubuntu8.7"
    b["models"]["Qwen/Qwen2.5-VL-7B-Instruct"] = "f" * 40
    del b["pip"]["venv"]["six"]
    changes = pm.diff(a, b)
    assert "~ apt.libc6: 2.39-0ubuntu8.6 -> 2.39-0ubuntu8.7" in changes
    assert any(c.startswith("~ models.Qwen/Qwen2.5-VL-7B-Instruct") for c in changes)
    assert "- pip.venv.six: 1.17.0" in changes and len(changes) == 3


def test_compare_cli_is_the_two_boot_acceptance_check(tmp_path: Path, capsys: Any) -> None:
    a = _good_manifest()
    b = {**a, "generated_at": "later", "host": {"kind": "runpod", "id": "pod-b"}}
    pa, pb = tmp_path / "boot1.json", tmp_path / "boot2.json"
    pa.write_text(json.dumps(a), encoding="utf-8")
    pb.write_text(json.dumps(b), encoding="utf-8")
    assert pm.main(["compare", str(pa), str(pb)]) == 0
    assert "identical versions" in capsys.readouterr().out
    b["apt"] = {**a["apt"], "curl": "8.5.0-2ubuntu10.14"}
    pb.write_text(json.dumps(b), encoding="utf-8")
    assert pm.main(["compare", str(pa), str(pb)]) == 1
    assert "~ apt.curl" in capsys.readouterr().out


def test_write_cli_records_compares_and_fails_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    boots = [_good_manifest(), _good_manifest(), _good_manifest()]
    boots[2]["pip"]["venv"]["numpy"] = "2.4.0"
    monkeypatch.setattr(pm, "collect", lambda ws, versions: boots.pop(0))
    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "hf")
    out = tmp_path / "manifests"
    args = ["write", "--dir", str(out), "--versions", str(RUNPOD / "versions.env"),
            "--lock", str(RUNPOD / "requirements.lock"), "--ws", str(tmp_path)]  # fmt: skip

    assert pm.main(args) == 0  # first boot: recorded, every pin holds
    first = capsys.readouterr().out
    assert "every pin holds" in first and "previous boot" not in first
    assert pm.main(args) == 0  # second boot: identical
    assert "identical versions to the previous boot" in capsys.readouterr().out
    assert pm.main(args) == 1  # third boot drifted
    third = capsys.readouterr().out
    assert "DRIFT: pip: numpy is 2.4.0" in third and "WARNING: changed since" in third
    assert len(list(out.glob("manifest.*.json"))) >= 1 and (out / "last.json").is_file()
    assert "numpy is 2.4.0" in (out / "drift.log").read_text(encoding="utf-8")


def test_collect_reads_go_minio_and_models_from_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "tools" / "go").mkdir(parents=True)
    (tmp_path / "tools" / "go" / "VERSION").write_text("go1.27.1\ntime 2026-08-01\n")
    (tmp_path / "tools" / f"minio-{VERSIONS['MINIO_TAG']}").write_bytes(b"minio-binary")
    ref = tmp_path / "hf-cache" / "hub" / "models--Qwen--Qwen2.5-VL-7B-Instruct" / "refs"
    ref.mkdir(parents=True)
    (ref / "main").write_text("abc123\n")
    env = {"CDI_VLM_MODEL_ID": "Qwen/Qwen2.5-VL-7B-Instruct", "RUNPOD_POD_ID": "pod-a"}

    m = pm.collect(tmp_path, VERSIONS, env)
    assert m["go"] == "go1.27.1"
    assert m["minio"]["sha256"] == __import__("hashlib").sha256(b"minio-binary").hexdigest()
    assert m["models"] == {"Qwen/Qwen2.5-VL-7B-Instruct": "abc123"}
    assert m["host"] == {"kind": "runpod", "id": "pod-a"}
    assert m["python"].startswith("3.")
    assert "pytest" in m["pip"]["venv"] or "pytest" in m["pip"]["system"]  # this interpreter
    # collecting twice on the same machine gives identical versions
    assert pm.diff(m, pm.collect(tmp_path, VERSIONS, env)) == []
