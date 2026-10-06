"""The OOM fallback model is Qwen2-VL-7B-Instruct (Apache-2.0, LICENSE file in its repository).

It replaced Qwen2.5-VL-3B-Instruct, whose Qwen research licence forbids commercial use. This guard
pins the default, keeps the env templates in step with it, and fails if the old model is named again
in code, config, scripts, tests, infra or the living docs. The dated history rows of the backlog
logs (``docs/backlog*.md``) record what ran on those dates and are not rewritten.
"""

from __future__ import annotations

import re
from pathlib import Path

from cdi_adapter.config import Settings

ROOT = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()

PRIMARY = "Qwen/Qwen2.5-VL-7B-Instruct"
FALLBACK = "Qwen/Qwen2-VL-7B-Instruct"
# the retired model, built from parts so this file does not name it
RETIRED = re.compile("Qwen2" + r"\.5-VL-3B")
SCAN = (
    "src/**/*.py",
    "scripts/**/*",
    "tests/**/*",
    "infra/**/*",
    ".github/**/*",
    ".env*",
    "pyproject.toml",
    "docs/*.md",
)
HISTORY = {"backlog 1.md", "backlog infra 1.md"}  # dated log rows, kept as written


def _files() -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in SCAN:
        for path in ROOT.glob(pattern):
            if (
                path.is_file()
                and path.resolve() != THIS
                and "__pycache__" not in path.parts
                and not (path.parent.name == "docs" and path.name in HISTORY)
            ):
                seen[path] = None
    return list(seen)


def test_default_fallback_is_qwen2_vl_7b_and_differs_from_the_primary():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.vlm_fallback_model_id == FALLBACK
    assert s.vlm_model_id == PRIMARY
    assert s.vlm_fallback_model_id != s.vlm_model_id


def test_env_templates_name_the_same_fallback():
    for name in (".env.example", ".env.runpod"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert f"CDI_VLM_FALLBACK_MODEL_ID={FALLBACK}" in text, name


def test_the_retired_model_is_named_nowhere_in_code_config_tests_or_living_docs():
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary fixture
        for number, line in enumerate(text.splitlines(), start=1):
            if RETIRED.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert not hits, "retired fallback model referenced: " + ", ".join(hits)


def test_no_living_text_still_describes_a_7b_to_3b_fallback():
    pattern = re.compile(r"7B\s*(->|→)\s*3B|falls? back to the 3B|3B fallback", re.IGNORECASE)
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert not hits, "stale 7B->3B wording: " + ", ".join(hits)


def test_no_test_tells_primary_from_fallback_by_the_text_7b():
    """Both models are 7B: a stub that raises 'out of memory' for any id containing '7B' would
    fail the fallback too. Stubs must compare the exact primary id."""
    offenders = []
    for path in (ROOT / "tests").glob("test_*.py"):
        if path.resolve() == THIS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"""["']7B["']\s+(not\s+)?in\s+\w+""", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, "id-text check for '7B': " + ", ".join(offenders)


def test_scan_covers_the_files_that_used_to_name_the_old_model():
    names = {p.relative_to(ROOT).as_posix() for p in _files()}
    for expected in (
        "src/cdi_adapter/config.py",
        ".env.example",
        ".env.runpod",
        "scripts/model_rollout_drill.py",
        "tests/test_ml_unit.py",
        "docs/alerts.md",
        "docs/TECH_STACK.md",
    ):
        assert expected in names, expected
