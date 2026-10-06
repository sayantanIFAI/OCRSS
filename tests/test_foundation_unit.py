"""E0-S1: settings, CPU thread caps, structured logging."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cdi_adapter import _cpu
from cdi_adapter.config import Settings
from cdi_adapter.logging import configure, get_logger

# ---------------------------------------------------------------- settings (ARCHITECTURE §9)


def test_settings_defaults_match_architecture_section_9(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("CDI_"):
            monkeypatch.delenv(k)
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi"
    assert s.redis_url == "redis://127.0.0.1:6379/0"
    assert s.s3_endpoint_url == "http://127.0.0.1:9000"
    assert s.s3_bucket == "cdi-documents"
    assert s.page_dpi == 200
    assert s.webapp_port == 8888
    assert (s.mlserve_url, s.mlserve_port) == ("http://127.0.0.1:8077", 8077)
    assert s.mlserve_backend == "stub"  # code default; the pod sets hf (TECH_STACK §8)
    assert s.vlm_model_id == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert s.vlm_fallback_model_id == "Qwen/Qwen2.5-VL-3B-Instruct"
    assert s.vlm_max_pixels_ocr == 2_000_000
    assert s.fast_classify is True
    assert s.extract_retries == 1
    assert s.job_max_workers == 6
    assert (s.ocr_engine, s.handwritten_uses_vlm) == ("rapidocr", True)
    assert s.ig_package == "ndhm.in#6.5.0"  # E6-S1: the registry id of the NRCeS ABDM IG
    assert s.terminology_package == "in-snomed-loinc-icd10"
    assert s.inbox_dir.endswith("inbox")


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "hf")
    monkeypatch.setenv("CDI_JOB_MAX_WORKERS", "3")
    monkeypatch.setenv("CDI_FAST_CLASSIFY", "false")
    s = Settings(_env_file=None)
    assert (s.mlserve_backend, s.job_max_workers, s.fast_classify) == ("hf", 3, False)


def test_settings_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CDI_PAGE_DPI", raising=False)
    env = tmp_path / ".env"
    env.write_text("CDI_PAGE_DPI=300\n", encoding="utf-8")
    assert Settings(_env_file=env).page_dpi == 300


@pytest.mark.parametrize(
    "var,value",
    [("CDI_MLSERVE_BACKEND", "gpt"), ("CDI_JOB_MAX_WORKERS", "0"), ("CDI_PAGE_DPI", "5")],
)
def test_settings_reject_invalid_values(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# ---------------------------------------------------------------- CPU caps (§7.4 #4)


def test_parse_cgroup_v2():
    assert _cpu.parse_cgroup_v2("1360000 100000") == pytest.approx(13.6)
    assert _cpu.parse_cgroup_v2("max 100000") is None
    assert _cpu.parse_cgroup_v2("garbage") is None


def test_parse_cgroup_v1():
    assert _cpu.parse_cgroup_v1("200000\n", "100000\n") == 2.0
    assert _cpu.parse_cgroup_v1("-1", "100000") is None


def test_cpu_budget_prefers_cgroup_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(_cpu.os, "cpu_count", lambda: 128)
    v2 = tmp_path / "cpu.max"
    v2.write_text("1360000 100000")
    assert _cpu.cpu_budget(v2=v2) == pytest.approx(13.6)  # the pod: 13.6, not 128


def test_cpu_budget_v1_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(_cpu.os, "cpu_count", lambda: 8)
    missing = tmp_path / "nope"
    q, p = tmp_path / "q", tmp_path / "p"
    q.write_text("400000")
    p.write_text("100000")
    assert _cpu.cpu_budget(v2=missing, v1_quota=q, v1_period=p) == 4.0
    assert _cpu.cpu_budget(v2=missing, v1_quota=missing, v1_period=missing) == 8.0
    unlimited = tmp_path / "cpu.max"
    unlimited.write_text("max 100000")
    assert _cpu.cpu_budget(v2=unlimited) == 8.0


def test_cpu_budget_never_exceeds_host(tmp_path, monkeypatch):
    monkeypatch.setattr(_cpu.os, "cpu_count", lambda: 4)
    v2 = tmp_path / "cpu.max"
    v2.write_text("1600000 100000")
    assert _cpu.cpu_budget(v2=v2) == 4.0


@pytest.mark.parametrize(
    "budget,workers,expected", [(13.6, 6, 2), (128, 6, 21), (2, 6, 1), (1, 1, 1), (8, 1, 7)]
)
def test_per_worker_threads(budget, workers, expected):
    assert _cpu.per_worker_threads(budget, workers) == expected


def test_apply_thread_caps_sets_unset_vars_only(monkeypatch):
    for var in _cpu.THREAD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OMP_NUM_THREADS", "9")  # operator override must win
    n = _cpu.apply_thread_caps(6, budget=13.6)
    env = __import__("os").environ
    assert n == 2
    assert env["OMP_NUM_THREADS"] == "9"
    assert all(env[v] == "2" for v in _cpu.THREAD_ENV_VARS if v != "OMP_NUM_THREADS")


# ---------------------------------------------------------------- structured logging (§12)


def test_logger_emits_structured_json(capsys):
    configure("INFO", json=True)
    get_logger("test", document_id="d1").info("classified", doc_type="prescription")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    event = json.loads(line)
    assert event["event"] == "classified"
    assert event["document_id"] == "d1"
    assert event["doc_type"] == "prescription"
    assert event["level"] == "info"
    assert "timestamp" in event


def test_logger_level_filtering(capsys):
    configure("WARNING", json=True)
    get_logger().info("hidden")
    get_logger().warning("shown")
    out = capsys.readouterr().out
    assert "hidden" not in out and "shown" in out
    configure("INFO", json=True)
