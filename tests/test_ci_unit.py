"""E1-S1: the CI gate turns red on any failing stage, including a deliberately failing test."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_ci():
    spec = importlib.util.spec_from_file_location("ci_gate", ROOT / "scripts" / "ci.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


ci = _load_ci()


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_all_green_stages_pass(tmp_path: Path) -> None:
    stages = [ci.Stage("a", _py("pass")), ci.Stage("b", _py("pass"))]
    assert ci.run_stages(stages, cwd=tmp_path) == 0


def test_one_red_stage_fails_the_gate_and_later_stages_still_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    stages = [
        ci.Stage("red", _py("raise SystemExit(3)")),
        ci.Stage("after", _py(f"open({str(marker)!r}, 'w').close()")),
    ]
    assert ci.run_stages(stages, cwd=tmp_path) == 1
    assert marker.exists()  # every red stage is reported in one run


def test_deliberately_failing_test_turns_the_unit_stage_red(tmp_path: Path) -> None:
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    unit = ci.Stage("unit", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"))
    assert ci.run_stages([unit], cwd=tmp_path) == 1


def test_passing_test_keeps_the_unit_stage_green(tmp_path: Path) -> None:
    (tmp_path / "test_green.py").write_text("def test_green():\n    assert 1 == 1\n")
    unit = ci.Stage("unit", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"))
    assert ci.run_stages([unit], cwd=tmp_path) == 0


def test_gate_covers_lint_format_types_and_unit_tests() -> None:
    by_name = {s.name: s.cmd for s in ci.STAGES}
    assert set(by_name) == {"lint", "format", "types", "secrets", "unit"}
    assert by_name["unit"][by_name["unit"].index("-m", 2) + 1] == "not integration"
    assert "ruff" in by_name["lint"] and "mypy" in by_name["types"]


def test_list_shows_the_stages(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci.main(["--list"]) == 0
    assert "unit:" in capsys.readouterr().out


def test_workflow_runs_the_same_gate_on_push_and_pull_request() -> None:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert "push" in triggers and "pull_request" in triggers
    steps = wf["jobs"]["unit-gate"]["steps"]
    assert any(s.get("run") == "python scripts/ci.py" for s in steps)


def test_stage_env_reaches_the_command(tmp_path: Path) -> None:
    code = "import os, sys; sys.exit(0 if os.environ.get('CI_PROBE') == 'x' else 1)"
    assert ci.run_stages([ci.Stage("env", _py(code), (("CI_PROBE", "x"),))], cwd=tmp_path) == 0


def test_integration_suite_requires_infra_and_audits_pipeline_runs() -> None:
    by_name = {s.name: s for s in ci.SUITES["integration"]}
    assert set(by_name) == {"integration", "pipeline-audit", "golden"}
    assert "--fresh-db" in by_name["golden"].cmd
    assert ("CDI_TEST_REQUIRE_INFRA", "1") in by_name["integration"].env
    assert ("CDI_MLSERVE_BACKEND", "stub") in by_name["integration"].env
    assert "integration" in by_name["integration"].cmd
    assert by_name["pipeline-audit"].cmd[-1] == "scripts/ci_pipeline_audit.py"


MINIO_TEST = "tests/test_classify_integration.py::test_full_chain_ingest_ocr_classify"


def _run_minio_test(require: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CDI_TEST_S3_ENDPOINT"}
    env.pop("CDI_TEST_REQUIRE_INFRA", None)
    if require:
        env["CDI_TEST_REQUIRE_INFRA"] = require
    cmd = [sys.executable, "-m", "pytest", MINIO_TEST, "-q", "-p", "no:cacheprovider", "-rs"]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)


def test_missing_minio_skips_by_default() -> None:
    r = _run_minio_test(None)
    assert r.returncode == 0 and "skipped" in r.stdout, r.stdout[-800:]


def test_missing_minio_fails_when_ci_requires_infra() -> None:
    r = _run_minio_test("1")
    assert r.returncode != 0 and "CDI_TEST_REQUIRE_INFRA=1 but" in r.stdout, r.stdout[-800:]
