"""E1-S6 (unit): golden file consistency with ARCHITECTURE §8.2, drift detection and the alert."""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "golden_path", ROOT / "scripts" / "golden_path.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden_path"] = mod
    spec.loader.exec_module(mod)
    return mod


gp = _load()
GOLDEN = gp.load_golden()
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _section_8_2() -> str:
    return ARCH.split("### 8.2 Worked example")[1].split("\n---")[0]


def test_real_vlm_baseline_is_exactly_the_section_8_2_run() -> None:
    sec = _section_8_2()
    rows = {m[0]: (int(m[1]), int(m[2])) for m in re.findall(
        r"`(\w+)\.pdf` \| `\w+` 0\.95 \| (\d+) .*?\| `\w+`, (\d+) resources", sec)}  # fmt: skip
    real = GOLDEN["real_vlm"]
    assert rows == {n: (d["facts"], d["resources"]) for n, d in real["documents"].items()}
    m = re.search(
        r"(\d+) `clinical_fact` rows \+\s*(\d+) `fact_provenance` rows \+ (\d+) `fhir_resource`",
        sec,
    )
    assert m and tuple(map(int, m.groups())) == (
        real["totals"]["facts"],
        real["totals"]["provenance"],
        real["totals"]["resources"],
    )
    assert "3 `Bundle(type=document)`" in sec and real["totals"]["bundles"] == 3
    assert sec.count("**0 errors**") == 3 and real["totals"]["structural_errors"] == 0


def test_both_baselines_cover_the_same_three_documents_and_artifacts() -> None:
    stub, real = GOLDEN["stub"]["documents"], GOLDEN["real_vlm"]["documents"]
    assert list(stub) == list(real) == ["prescription_00", "lab_report_01", "vitals_sheet_02"]
    for name in stub:
        for key in ("doc_type", "artifact_type", "bundles", "structural_errors"):
            assert stub[name][key] == real[name][key], (name, key)


def test_totals_are_the_sum_of_the_documents() -> None:
    for base in ("stub", "real_vlm"):
        docs, totals = GOLDEN[base]["documents"], GOLDEN[base]["totals"]
        for key, want in totals.items():
            assert sum(d[key] for d in docs.values()) == want, (base, key)


def test_stub_differs_from_section_8_2_only_on_the_documented_vitals_facts() -> None:
    stub, real = GOLDEN["stub"]["documents"], GOLDEN["real_vlm"]["documents"]
    for name in ("prescription_00", "lab_report_01"):
        assert stub[name]["facts"] == real[name]["facts"]
    assert (real["vitals_sheet_02"]["facts"], stub["vitals_sheet_02"]["facts"]) == (1, 7)
    assert "§13 records as its known vitals weak spot" in GOLDEN["_about"]


def _measured(base: str = "stub") -> dict:
    return copy.deepcopy(GOLDEN[base])


def test_no_drift_when_measured_equals_golden() -> None:
    assert gp.compare(_measured(), GOLDEN["stub"]) == []


def test_count_drift_is_reported_per_document_and_total() -> None:
    m = _measured()
    m["documents"]["lab_report_01"]["facts"] = 4
    m["totals"]["facts"] = 20
    assert gp.compare(m, GOLDEN["stub"]) == [
        "lab_report_01.facts: expected 5, got 4",
        "totals.facts: expected 21, got 20",
    ]


def test_structural_error_status_and_missing_document_are_drift() -> None:
    m = _measured()
    m["documents"]["prescription_00"]["structural_errors"] = 1
    m["documents"]["prescription_00"]["status"] = "draft"
    del m["documents"]["vitals_sheet_02"]
    drift = gp.compare(m, GOLDEN["stub"])
    assert "prescription_00.structural_errors: expected 0, got 1" in drift
    assert "prescription_00.status: expected 'ready_to_share', got 'draft'" in drift
    assert "vitals_sheet_02: document missing from the run" in drift


def test_real_vlm_baseline_does_not_pin_bundle_status() -> None:
    """§8.2 does not state it; Appendix D records `draft` when facts are held for review."""
    m = _measured("real_vlm")
    for d in m["documents"].values():
        d["status"] = "draft"
    assert gp.compare(m, GOLDEN["real_vlm"]) == []


def test_alert_appends_one_line_per_drifting_run(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "golden_alerts.log"
    gp.alert(log, "real_vlm", "full", ["totals.facts: expected 15, got 14"])
    gp.alert(log, "real_vlm", "full", ["a", "b"])
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "GOLDEN DRIFT baseline=real_vlm mode=full: totals.facts: expected 15, got 14" in lines[0]
    assert lines[1].endswith("a; b")
