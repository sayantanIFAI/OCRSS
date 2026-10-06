"""E10-S2: alert rules, their synthetic triggers, the Grafana dashboard and the monitoring stack.

The synthetic triggers themselves run in ``promtool test rules`` (the pinned Prometheus build,
Linux): ``test_promtool_*`` run it when ``CDI_PROMTOOL`` or a ``promtool`` on PATH exists (the pod,
Colab), and skip on this Windows dev box, where Smart App Control blocks new executables. Everything
else here runs everywhere."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from cdi_adapter import metrics, secretstore
from tests.test_metrics_unit import value

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "infra" / "monitoring"
RULES = MON / "rules" / "cdi-alerts.yml"
RULE_TESTS = MON / "rules" / "cdi-alerts.test.yml"
DASHBOARD = MON / "grafana" / "dashboards" / "cdi-overview.json"
ALERTS_DOC = ROOT / "docs" / "alerts.md"
START = MON / "start_monitoring.sh"
ALERTS = (
    "CdiSlaWindowBreach",
    "CdiGpuOom",
    "CdiQueueBacklog",
    "CdiPartialRateAboveBaseline",
    "CdiConfidenceDrift",
    "CdiPgReplicationLag",
    "CdiPgStandbyDisconnected",
    "CdiMinioDegraded",
    "CdiBackupStale",
    "CdiRestoreDrillFailed",
    "CdiRestoreDrillOverdue",
    "CdiWebappInstanceDown",
    "CdiJobsOrphaned",
)


def _rules() -> list[dict]:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"][0]["rules"]


def _exposed_names() -> set[str]:
    """Every sample name the two processes can expose, collectors included."""
    reg = CollectorRegistry()
    job = SimpleNamespace(state="running", started_at=0.0)
    reg.register(metrics.JobsCollector(lambda: [job], clock=lambda: 10.0))
    reg.register(metrics.ReviewBacklogCollector(lambda: [("workbasket", 1)]))
    reg.register(metrics.GpuCollector(lambda: "0, 50, 1000, 2000\n"))
    reg.register(metrics.PgReplicationCollector(lambda: (False, 1, 0.2, 0)))
    reg.register(metrics.MinioNodesCollector(["http://a", "http://b"], lambda urls: 2))
    drives = 'minio_cluster_drive_total{server="x"} 4\nminio_cluster_drive_offline_total 0\n'
    reg.register(metrics.MinioDrivesCollector(lambda: drives))
    backup = {"ok": True, "last_success_at": "2026-09-24T10:00:00+00:00"}
    drill = {"ok": True, "finished_at": "2026-09-24T11:00:00+00:00", "rpo_s": 60.0, "rto_s": 90.0}
    reg.register(metrics.DrCollector(lambda: (backup, drill)))
    text = generate_latest(reg).decode() + generate_latest().decode()
    return {s.name for f in text_string_to_metric_families(text) for s in f.samples}


def _metric_names(expr: str) -> set[str]:
    return set(re.findall(r"\b(cdi_[a-z_]+)", expr))


# ------------------------------------------------------------------ the rules (E10-S2 + S4)


def test_exactly_the_alerts_of_the_blocks():
    rules = _rules()
    assert [r["alert"] for r in rules] == list(ALERTS)
    sev = {r["alert"]: r["labels"]["severity"] for r in rules}
    assert sev == {"CdiSlaWindowBreach": "page", "CdiGpuOom": "page", "CdiQueueBacklog": "page",
                   "CdiPartialRateAboveBaseline": "ticket",
                   "CdiConfidenceDrift": "ticket", "CdiPgReplicationLag": "page",
                   "CdiPgStandbyDisconnected": "ticket", "CdiMinioDegraded": "ticket",
                   "CdiBackupStale": "page", "CdiRestoreDrillFailed": "page",
                   "CdiRestoreDrillOverdue": "ticket", "CdiWebappInstanceDown": "ticket",
                   "CdiJobsOrphaned": "page"}  # fmt: skip


def test_the_sla_rule_uses_the_adr_0002_window():
    sla = next(r for r in _rules() if r["alert"] == "CdiSlaWindowBreach")["expr"]
    assert metrics.SLA_SECONDS == 300 and "> 300" in sla
    assert 'le="300.0"' in sla and 300 in metrics.JOB_BUCKETS  # the bucket the rule reads exists


def test_every_metric_a_rule_or_panel_uses_is_exposed():
    exposed = _exposed_names()
    used = set().union(*(_metric_names(r["expr"]) for r in _rules()))
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    for p in dash["panels"]:
        for t in p.get("targets", []):
            used |= _metric_names(t["expr"])
    missing = sorted(u for u in used if u not in exposed)
    assert not missing, f"not exposed by cdi_adapter.metrics: {missing}"
    assert len(used) >= 12


def test_every_alert_has_a_documented_on_call_response():
    doc = ALERTS_DOC.read_text(encoding="utf-8")
    headings = {h.strip().lower() for h in re.findall(r"^## (.+)$", doc, re.M)}
    for r in _rules():
        name = r["alert"]
        assert name.lower() in headings, name
        assert r["annotations"]["runbook_url"] == f"docs/alerts.md#{name.lower()}"
        section = doc.split(f"## {name}", 1)[1].split("\n## ", 1)[0]
        assert re.search(r"^1\. ", section, re.M), f"{name}: no numbered response steps"


def test_every_alert_has_a_firing_and_a_quiet_synthetic_case():
    tests = yaml.safe_load(RULE_TESTS.read_text(encoding="utf-8"))["tests"]
    fires: dict[str, int] = {a: 0 for a in ALERTS}
    quiet: dict[str, int] = {a: 0 for a in ALERTS}
    for t in tests:
        for case in t["alert_rule_test"]:
            (fires if case["exp_alerts"] else quiet)[case["alertname"]] += 1
    assert all(fires.values()) and all(quiet.values()), (fires, quiet)
    # the expected annotations are the rule's own, word for word
    ann = {r["alert"]: r["annotations"] for r in _rules()}
    for t in tests:
        for case in t["alert_rule_test"]:
            for exp in case["exp_alerts"]:
                assert exp["exp_annotations"] == ann[case["alertname"]]


def _promtool() -> str | None:
    return os.environ.get("CDI_PROMTOOL") or shutil.which("promtool")


def test_promtool_checks_the_rules():
    tool = _promtool()
    if not tool:
        pytest.skip("promtool not here (pod / Colab: set CDI_PROMTOOL)")
    r = subprocess.run([tool, "check", "rules", str(RULES)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{len(ALERTS)} rules found" in r.stdout


def test_promtool_fires_each_alert_on_its_synthetic_trigger():
    tool = _promtool()
    if not tool:
        pytest.skip("promtool not here (pod / Colab: set CDI_PROMTOOL)")
    r = subprocess.run([tool, "test", "rules", str(RULE_TESTS)], capture_output=True, text=True,
                       cwd=RULE_TESTS.parent)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SUCCESS" in r.stdout


# ------------------------------------------------------------------ dashboard + stack wiring


def test_the_dashboard_is_provisioned_against_the_local_prometheus():
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    ds = yaml.safe_load((MON / "grafana/provisioning/datasources/prometheus.yml").read_text())
    uid = ds["datasources"][0]["uid"]
    assert ds["datasources"][0]["url"] == "http://127.0.0.1:9090"
    assert dash["uid"] == "cdi-overview" and dash["editable"] is False
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    assert dash["panels"][0]["type"] == "alertlist"
    for p in dash["panels"]:
        assert p["gridPos"]["x"] + p["gridPos"]["w"] <= 24
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == uid
    prov = yaml.safe_load((MON / "grafana/provisioning/dashboards/cdi.yml").read_text())
    assert prov["providers"][0]["options"]["path"] == "$CDI_DASHBOARDS"
    assert 'CDI_DASHBOARDS="$HERE/grafana/dashboards"' in START.read_text(encoding="utf-8")


def test_prometheus_scrapes_both_processes_and_alerts_via_alertmanager():
    cfg = yaml.safe_load((MON / "prometheus.yml").read_text(encoding="utf-8"))
    targets = {c["job_name"]: c["static_configs"][0]["targets"] for c in cfg["scrape_configs"]
               if "static_configs" in c}  # fmt: skip
    assert targets["cdi-mlserve"] == ["127.0.0.1:8077"]
    # E10-S9: every instance / worker, from the target files start_apptier.sh writes
    sd = {c["job_name"]: c["file_sd_configs"][0]["files"] for c in cfg["scrape_configs"]
          if "file_sd_configs" in c}  # fmt: skip
    assert sd == {"cdi-webapp": ["/workspace/monitoring/targets/cdi-webapp.json"],
                  "cdi-worker": ["/workspace/monitoring/targets/cdi-worker.json"]}  # fmt: skip
    assert cfg["rule_files"] == ["rules/cdi-alerts.yml"]
    assert cfg["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["127.0.0.1:9093"]
    am = yaml.safe_load((MON / "alertmanager.yml").read_text(encoding="utf-8"))
    assert am["route"]["receiver"] == "oncall" and am["receivers"][0]["name"] == "oncall"


def test_every_monitoring_service_listens_on_loopback_only():
    sh = START.read_text(encoding="utf-8")
    assert "--web.listen-address=127.0.0.1:9090" in sh
    assert "--web.listen-address=127.0.0.1:9093" in sh
    assert "--cluster.listen-address= " in sh  # Alertmanager gossip off
    assert "GF_SERVER_HTTP_ADDR=127.0.0.1" in sh and "GF_AUTH_ANONYMOUS_ENABLED=false" in sh
    assert "0.0.0.0" not in sh and "0.0.0.0" not in (MON / "prometheus.yml").read_text()


def test_every_download_is_pinned_and_hash_checked():
    from cdi_adapter.podmanifest import parse_versions

    pins = parse_versions((ROOT / "infra/runpod/versions.env").read_text(encoding="utf-8"))
    for key in ("PROMETHEUS", "ALERTMANAGER", "GRAFANA"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", pins[f"{key}_VERSION"]), key
        assert re.fullmatch(r"[0-9a-f]{64}", pins[f"{key}_SHA256"]), key
    sh = START.read_text(encoding="utf-8")
    assert "sha256sum -c --quiet -" in sh and "sha256 mismatch" in sh
    for key in ("PROMETHEUS", "ALERTMANAGER", "GRAFANA"):
        assert f'"${key}_SHA256"' in sh


def test_the_grafana_password_comes_from_the_secrets_store(tmp_path):
    sh = START.read_text(encoding="utf-8")
    assert "cdi_grafana_admin_password" in sh
    # Grafana 13.2 ignores GF_SECURITY_ADMIN_PASSWORD__FILE and keeps admin/admin (Colab deploy
    # check): the password is set after start, from stdin, and admin/admin must be refused
    assert "admin reset-admin-password --password-from-stdin" in sh
    assert '<"$GF_PW_FILE"' in sh
    assert '[ "$(code admin)" = 401 ]' in sh and 'pkill -f "grafana server"' in sh
    assert "-K -" in sh  # credentials reach curl on stdin, not argv
    assert "GF_SECURITY_ADMIN_PASSWORD" not in sh  # neither the value nor the ignored __FILE form
    assert '-u "admin:' not in sh
    created = secretstore.init(tmp_path)
    assert "cdi_grafana_admin_password" in created
    pw = secretstore.read(tmp_path, "cdi_grafana_admin_password")
    assert pw and len(pw) >= secretstore.MIN_SECRET_LEN
    assert not [p for p in secretstore.check(tmp_path) if "grafana" in p]
    secretstore.write(tmp_path, "cdi_grafana_admin_password", "admin")
    assert any("grafana" in p for p in secretstore.check(tmp_path))  # a weak one is refused


# ------------------------------------------------------------------ the new alert inputs


def test_a_job_records_its_end_to_end_time():
    from cdi_adapter.webapp.jobs import JobRunner
    from tests.test_metrics_unit import _stages

    before = value("cdi_job_seconds_count")
    runner = JobRunner(_stages())
    runner.wait(runner.create([("a", b"x", None)]).id)
    assert value("cdi_job_seconds_count") == before + 1
    assert value("cdi_job_seconds_bucket", le="300.0") >= 1


def test_the_oldest_running_job_age_shows_a_breach_while_it_runs():
    reg = CollectorRegistry()
    jobs = [SimpleNamespace(state="running", started_at=100.0),
            SimpleNamespace(state="running", started_at=400.0),
            SimpleNamespace(state="review", started_at=0.0)]  # fmt: skip
    reg.register(metrics.JobsCollector(lambda: jobs, clock=lambda: 500.0))
    assert value("cdi_job_oldest_running_seconds", reg) == 400.0  # the review job does not count
    idle = CollectorRegistry()
    idle.register(metrics.JobsCollector(lambda: [], clock=lambda: 500.0))
    assert value("cdi_job_oldest_running_seconds", idle) == 0.0


def test_gpu_out_of_memory_is_counted_where_it_happens():
    from fastapi.testclient import TestClient

    from cdi_adapter.config import Settings
    from cdi_adapter.mlserve.app import create_app
    from cdi_adapter.mlserve.backends import GenItem, HFBackend, StubBackend

    class OutOfMemoryError(RuntimeError):
        pass

    def loader(mid, s):
        if "7B" in mid:
            raise OutOfMemoryError("CUDA out of memory")
        return "M", "P"

    def boom(*a):
        raise OutOfMemoryError("CUDA out of memory")

    load0 = value("cdi_vlm_oom_total", where="load")
    batch0 = value("cdi_vlm_oom_total", where="batch")
    gen0 = value("cdi_vlm_oom_total", where="generate")
    hf = HFBackend(Settings(_env_file=None, mlserve_backend="hf"),  # type: ignore[call-arg]
                   loader=loader, runner=lambda *a: "ok", batch_runner=boom)  # fmt: skip
    hf.generate("x")  # 7B does not fit -> the 3B serves
    assert value("cdi_vlm_oom_total", where="load") == load0 + 1
    hf.generate_batch([GenItem("a"), GenItem("b")])  # the batch is split, not dropped
    assert value("cdi_vlm_oom_total", where="batch") == batch0 + 1

    class Oom(StubBackend):
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
            raise OutOfMemoryError("CUDA out of memory")

    c = TestClient(create_app(Oom()), raise_server_exceptions=False)
    assert c.post("/vlm/generate", json={"prompt": "x"}).status_code == 500
    assert value("cdi_vlm_oom_total", where="generate") == gen0 + 1


# ------------------------------------------------------------------ E10-S3: Tempo + Loki


def _yaml(name: str) -> dict:
    return yaml.safe_load((MON / name).read_text(encoding="utf-8"))


def test_tempo_and_loki_listen_on_loopback_only():
    tempo, loki = _yaml("tempo.yaml"), _yaml("loki.yaml")
    assert tempo["server"]["http_listen_address"] == "127.0.0.1"
    assert tempo["server"]["grpc_listen_address"] == "127.0.0.1"
    assert tempo["distributor"]["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == (
        "127.0.0.1:4318"
    )
    assert "grpc" not in tempo["distributor"]["receivers"]["otlp"]["protocols"]
    assert loki["server"]["http_listen_address"] == "127.0.0.1"
    assert loki["server"]["grpc_listen_address"] == "127.0.0.1"
    assert loki["common"]["instance_addr"] == "127.0.0.1"
    assert loki["auth_enabled"] is False  # single tenant, loopback only
    assert loki["limits_config"]["allow_structured_metadata"] is True  # OTLP trace ids land here
    assert tempo["usage_report"]["reporting_enabled"] is False
    assert loki["analytics"]["reporting_enabled"] is False


def test_the_services_export_to_exactly_where_tempo_and_loki_listen():
    env = (ROOT / ".env.runpod").read_text(encoding="utf-8")
    assert "CDI_OTEL_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces" in env
    assert "CDI_OTEL_LOGS_ENDPOINT=http://127.0.0.1:3100/otlp/v1/logs" in env
    assert "CDI_OTEL_TRACES_ENDPOINT=\n" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_grafana_links_logs_and_traces_both_ways():
    ds = {
        d["uid"]: d
        for d in yaml.safe_load(
            (MON / "grafana/provisioning/datasources/prometheus.yml").read_text()
        )["datasources"]
    }
    assert ds["cdi-tempo"]["url"] == "http://127.0.0.1:3200"
    assert ds["cdi-loki"]["url"] == "http://127.0.0.1:3100"
    t2l = ds["cdi-tempo"]["jsonData"]["tracesToLogsV2"]
    assert t2l["datasourceUid"] == "cdi-loki" and t2l["filterByTraceID"] is True
    (field,) = ds["cdi-loki"]["jsonData"]["derivedFields"]
    assert field["datasourceUid"] == "cdi-tempo" and field["matcherRegex"] == "trace_id"
    assert field["url"] == "$${__value.raw}"  # `$$` = a literal `$` in provisioning files


def test_tempo_and_loki_are_pinned_hash_checked_and_started():
    from cdi_adapter.podmanifest import parse_versions

    pins = parse_versions((ROOT / "infra/runpod/versions.env").read_text(encoding="utf-8"))
    sh = START.read_text(encoding="utf-8")
    for key in ("TEMPO", "LOKI"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", pins[f"{key}_VERSION"])
        assert re.fullmatch(r"[0-9a-f]{64}", pins[f"{key}_SHA256"])
        assert f'"${key}_SHA256"' in sh
    assert "python3 -m zipfile -e" in sh  # the Loki zip, without an unpinned unzip
    assert '-config.file="$HERE/tempo.yaml"' in sh and '-config.file="$HERE/loki.yaml"' in sh
    assert sh.count("-config.expand-env=true") == 2
    assert "wait_up tempo http://127.0.0.1:3200/ready" in sh
    assert "wait_up loki http://127.0.0.1:3100/ready" in sh


def test_both_service_venvs_carry_the_otel_packages():
    from cdi_adapter.podmanifest import parse_lock

    for lock in ("requirements.lock", "requirements-vllm.lock"):
        pins = parse_lock((ROOT / "infra/runpod" / lock).read_text(encoding="utf-8"))
        assert pins["opentelemetry-sdk"] == pins["opentelemetry-api"] == "1.44.0", lock
        assert pins["opentelemetry-exporter-otlp-proto-http"] == "1.44.0", lock
        assert pins["protobuf"] == "7.36.2", lock  # unchanged by adding OTel


def test_a_running_grafana_reloads_its_provisioning():
    """Found on the Colab check: a Grafana started before Tempo/Loki existed did not know them."""
    sh = START.read_text(encoding="utf-8")
    assert "for what in datasources dashboards; do" in sh
    assert "/api/admin/provisioning/$what/reload" in sh and "-X POST -K -" in sh


# ------------------------------------------------------------------ E10-S4 drift


def _staging():
    import importlib.util

    path = ROOT / "scripts" / "drift_staging_check.py"
    spec = importlib.util.spec_from_file_location("drift_staging_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_injected_shift_is_over_the_alert_line_and_the_control_is_not():
    s = _staging()
    rule = next(r for r in _rules() if r["alert"] == "CdiConfidenceDrift")["expr"]
    line = float(re.search(r"\)\s*>\s*([0-9.]+)", rule).group(1))
    assert s.ks_distance(s.SHIFTED, s.BASELINE) == pytest.approx(0.5)
    assert s.ks_distance(s.SHIFTED, s.BASELINE) > line
    assert s.ks_distance(s.BASELINE, s.BASELINE) == 0 < line
    assert s.RULES == RULES  # staging evaluates the real rule file


def test_the_staging_history_is_valid_openmetrics_with_the_shift_at_the_end():
    s = _staging()
    now = 1_800_000_000
    pts = s.history(now, shift=True)
    assert pts[0][0] == now - 7 * 86400 - 7 * 3600 and pts[-1][0] == now
    assert pts[0][1] == s.BASELINE and pts[-1][1] == s.SHIFTED
    assert all(p[1] == s.BASELINE for p in s.history(now, shift=False))
    text = s.openmetrics(pts)
    assert text.endswith("# EOF\n")
    fams = {f.name: f for f in text_string_to_metric_families(text.replace("# EOF\n", ""))}
    samples = fams["cdi_fact_confidence"].samples
    last = {sm.labels.get("le"): sm.value for sm in samples[-5:] if sm.name.endswith("_bucket")}
    assert last["+Inf"] == len(pts) * s.FACTS_PER_STEP  # cumulative, +Inf == count
    assert last["0.8"] <= last["0.95"] <= last["+Inf"]


def test_the_drift_alert_needs_enough_facts_and_a_same_day_window():
    rule = next(r for r in _rules() if r["alert"] == "CdiConfidenceDrift")
    assert "[6h]" in rule["expr"] and "[7d] offset 6h" in rule["expr"]
    assert ">= 50" in rule["expr"] and rule["for"] == "30m"


def test_a_running_prometheus_reloads_new_rules():
    """An upgrade must load a new alert: a running Prometheus / Alertmanager gets a SIGHUP."""
    sh = START.read_text(encoding="utf-8")
    assert re.search(r"reload\(\) \{ pkill -HUP", sh)
    assert 'reload "$TOOLS/$PROM/prometheus --config.file" prometheus' in sh
    assert 'reload "$TOOLS/$AM/alertmanager --config.file" alertmanager' in sh
