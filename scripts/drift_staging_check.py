"""Inject a confidence-distribution shift into a STAGING Prometheus and check the drift alert
(backlog E10-S4 "an injected distribution shift in staging triggers the alert").

Never touches the live Prometheus: the synthetic history (7 days of a normal distribution, then a
same-day shift) is backfilled with ``promtool tsdb create-blocks-from openmetrics`` into a fresh
temporary data folder, and a separate Prometheus on 127.0.0.1:9091 evaluates the REAL rule file
(``infra/monitoring/rules/cdi-alerts.yml``) against it. A control run without the shift must stay
quiet. The alert's ``for: 30m`` is wall-clock time, so this check requires the rule to become
active (``pending``/``firing``); the ``for`` timing itself is covered by ``promtool test rules``.

    python scripts/drift_staging_check.py --tools /workspace/tools   # on the pod / Colab
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "infra" / "monitoring" / "rules" / "cdi-alerts.yml"
ALERT = "CdiConfidenceDrift"
STEP = 300  # 5-minute points
FACTS_PER_STEP = 10
# share of facts per bucket (<= 0.8, (0.8, 0.95], above) - the same shape as the promtool test
BASELINE = (0.1, 0.2, 0.7)
SHIFTED = (0.6, 0.2, 0.2)
EDGES = ("0.8", "0.95", "+Inf")


def history(now: int, *, shift: bool, days: int = 7, shift_hours: float = 7.0) -> list[
        tuple[int, tuple[float, float, float]]]:  # fmt: skip
    """(timestamp, bucket shares) per step: the baseline, then the shift for its last hours."""
    start = now - days * 86400 - int(shift_hours * 3600)
    shift_from = now - int(shift_hours * 3600)
    return [(t, SHIFTED if shift and t > shift_from else BASELINE)
            for t in range(start, now + 1, STEP)]  # fmt: skip


def openmetrics(points: Sequence[tuple[int, tuple[float, float, float]]]) -> str:
    """Cumulative histogram points in OpenMetrics text (what promtool backfills)."""
    lines = ["# TYPE cdi_fact_confidence histogram",
             "# HELP cdi_fact_confidence Calibrated fact confidence (synthetic)"]  # fmt: skip
    cum = [0, 0, 0]
    total = 0
    for t, shares in points:
        per = [round(s * FACTS_PER_STEP) for s in shares]
        cum = [cum[0] + per[0], cum[1] + per[0] + per[1], cum[2] + sum(per)]
        total += sum(per)
        lab = 'job="cdi-webapp",fact_type="medication"'
        for edge, c in zip(EDGES, cum, strict=True):
            lines.append(f'cdi_fact_confidence_bucket{{{lab},le="{edge}"}} {c} {t}')
        lines.append(f"cdi_fact_confidence_count{{{lab}}} {total} {t}")
        lines.append(f"cdi_fact_confidence_sum{{{lab}}} {total * 0.9:.1f} {t}")
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


def ks_distance(recent: Sequence[float], baseline: Sequence[float]) -> float:
    """Max gap between two cumulative bucket distributions (what the rule computes)."""

    def cdf(shares: Sequence[float]) -> list[float]:
        out, c = [], 0.0
        for s in shares:
            c += s
            out.append(c)
        return out

    return max(abs(a - b) for a, b in zip(cdf(recent), cdf(baseline), strict=True))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - loopback only
        return json.load(r)


# needs the Prometheus binaries: runs on the pod / Colab, not in the unit tests
def run_staging(tools: Path, *, shift: bool, port: int = 9091,
                timeout: float = 180) -> dict:  # pragma: no cover  # fmt: skip
    prom = next(tools.glob("prometheus-*/prometheus"))
    promtool = prom.with_name("promtool")
    work = Path(tempfile.mkdtemp(prefix="cdi-drift-staging-"))
    proc = None
    try:
        om = work / "history.om"
        now = int(time.time())
        om.write_text(openmetrics(history(now, shift=shift)), encoding="utf-8")
        data = work / "data"
        subprocess.run([str(promtool), "tsdb", "create-blocks-from", "openmetrics", str(om),
                        str(data)], check=True, capture_output=True)  # fmt: skip
        cfg = work / "staging.yml"
        cfg.write_text(f"global: {{evaluation_interval: 15s}}\nrule_files: [{RULES}]\n",
                       encoding="utf-8")  # fmt: skip
        proc = subprocess.Popen([str(prom), f"--config.file={cfg}", f"--storage.tsdb.path={data}",
                                 "--storage.tsdb.retention.time=30d",
                                 f"--web.listen-address=127.0.0.1:{port}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + timeout
        state, ks = "unknown", None
        while time.time() < deadline:
            time.sleep(5)
            try:
                rules = _get(f"{base}/api/v1/rules")["data"]["groups"]
            except OSError:
                continue
            rule = next((r for g in rules for r in g["rules"] if r["name"] == ALERT), None)
            if rule is None or rule.get("lastEvaluation", "").startswith("0001"):
                continue
            state = rule["state"]
            q = ("max(abs(sum by (le) (increase(cdi_fact_confidence_bucket[6h])) / "
                 "scalar(sum(increase(cdi_fact_confidence_count[6h]))) - sum by (le) "
                 "(increase(cdi_fact_confidence_bucket[7d] offset 6h)) / scalar(sum(increase("
                 "cdi_fact_confidence_count[7d] offset 6h)))))")  # fmt: skip
            res = _get(f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": q}))
            vals = res["data"]["result"]
            ks = float(vals[0]["value"][1]) if vals else None
            if state in ("pending", "firing") or (not shift and ks is not None):
                break
        return {"shift": shift, "state": state, "ks": ks}
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - staging host CLI
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tools", type=Path, default=Path("/workspace/tools"))
    args = ap.parse_args(argv)
    shifted = run_staging(args.tools, shift=True)
    control = run_staging(args.tools, shift=False)
    print(f"injected shift : alert {shifted['state']:8} KS {shifted['ks']}")
    print(f"control        : alert {control['state']:8} KS {control['ks']}")
    ok = shifted["state"] in ("pending", "firing") and control["state"] == "inactive"
    print("DRIFT STAGING CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
