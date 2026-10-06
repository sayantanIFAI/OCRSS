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
# dated log rows, and the page that records this replacement (it names the retired model on purpose)
HISTORY = {"backlog 1.md", "backlog infra 1.md", "fallback-model.md"}


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


# ------------------------------------------------------- the fallback is a degraded mode: made safe


def _hf_settings(**kw) -> Settings:
    return Settings(_env_file=None, mlserve_backend="hf", **kw)  # type: ignore[call-arg]


def test_fallback_loads_8bit_by_default_but_the_primary_stays_bf16():
    from cdi_adapter.mlserve.backends import _quantize_for

    s = _hf_settings()
    assert s.vlm_fallback_quantize == "8bit"
    assert _quantize_for(PRIMARY, s) == ""
    assert _quantize_for(FALLBACK, s) == "8bit"


def test_fallback_quantisation_can_be_switched_off_and_an_explicit_8bit_applies_to_both():
    from cdi_adapter.mlserve.backends import _quantize_for

    bf16_fallback = _hf_settings(vlm_fallback_quantize="")
    assert _quantize_for(FALLBACK, bf16_fallback) == ""
    everything_8bit = _hf_settings(vlm_quantize="8bit", vlm_fallback_quantize="")
    assert _quantize_for(PRIMARY, everything_8bit) == _quantize_for(FALLBACK, everything_8bit)
    assert _quantize_for(PRIMARY, everything_8bit) == "8bit"


def test_a_misconfigured_fallback_equal_to_the_primary_is_never_quantised_by_the_fallback_rule():
    from cdi_adapter.mlserve.backends import _quantize_for

    s = _hf_settings(vlm_fallback_model_id=PRIMARY)
    assert _quantize_for(PRIMARY, s) == ""


def test_load_kwargs_ask_for_int8_only_for_the_fallback(monkeypatch):
    import sys
    from types import SimpleNamespace

    class QuantConfig:
        def __init__(self, *, load_in_8bit):
            self.load_in_8bit = load_in_8bit

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="torch.bfloat16"))
    monkeypatch.setitem(
        sys.modules, "transformers", SimpleNamespace(BitsAndBytesConfig=QuantConfig)
    )
    from cdi_adapter.mlserve.backends import _load_kwargs

    s = _hf_settings()
    assert "quantization_config" not in _load_kwargs(PRIMARY, s)
    assert _load_kwargs(FALLBACK, s)["quantization_config"].load_in_8bit is True


def test_fallback_hold_sends_everything_the_fallback_read_to_a_person():
    from cdi_adapter.validate.service import fallback_hold

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    state, finding = fallback_hold("auto_accepted", FALLBACK, s)
    assert state == "in_review"
    assert (
        finding is not None and finding.code == "fallback-model" and finding.severity == "blocker"
    )
    assert FALLBACK in finding.message


def test_fallback_hold_leaves_everything_else_alone():
    from cdi_adapter.validate.service import fallback_hold

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert fallback_hold("auto_accepted", PRIMARY, s) == ("auto_accepted", None)  # the primary
    assert fallback_hold("auto_accepted", None, s) == ("auto_accepted", None)  # unknown model
    assert fallback_hold("auto_accepted", "stub-keyword-responder", s) == ("auto_accepted", None)
    assert fallback_hold("in_review", FALLBACK, s) == ("in_review", None)  # already held
    off = Settings(_env_file=None, gate_fallback_review=False)  # type: ignore[call-arg]
    assert fallback_hold("auto_accepted", FALLBACK, off) == ("auto_accepted", None)
    same = Settings(_env_file=None, vlm_fallback_model_id=PRIMARY)  # type: ignore[call-arg]
    assert fallback_hold("auto_accepted", PRIMARY, same) == ("auto_accepted", None)


def test_fallback_review_is_on_by_default_and_documented_in_both_env_templates():
    assert Settings(_env_file=None).gate_fallback_review is True  # type: ignore[call-arg]
    for name in (".env.example", ".env.runpod"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "CDI_GATE_FALLBACK_REVIEW=true" in text, name
        assert "CDI_VLM_FALLBACK_QUANTIZE=8bit" in text, name


def test_the_gate_wires_fallback_hold_into_the_validation_loop():
    source = (ROOT / "src/cdi_adapter/validate/service.py").read_text(encoding="utf-8")
    assert "fallback_hold(state, served_model, s)" in source
    assert "SELECT model_name FROM extraction" in source


# ------------------------------------------------------- security: the document is data, not orders


def test_every_request_carries_the_untrusted_document_system_notice():
    from cdi_adapter.mlserve.backends import SYSTEM_NOTICE, _chat_messages

    content = [{"type": "text", "text": "Transcribe this line."}]
    messages = _chat_messages(content)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM_NOTICE
    assert messages[1]["content"] == content
    low = SYSTEM_NOTICE.lower()
    assert "untrusted" in low and "never obey" in low and "ignore the previous instructions" in low
    assert "never invent a value" in low


def test_no_code_path_builds_a_chat_without_the_notice():
    """The runner, the batch runner and the vLLM prompt builder all go through _chat_messages."""
    source = (ROOT / "src/cdi_adapter/mlserve/backends.py").read_text(encoding="utf-8")
    assert source.count('"role": "user"') == 1  # only inside the helper itself
    assert (
        source.count("_chat_messages(") == 4
    )  # the definition + runner, batch runner, vLLM builder
