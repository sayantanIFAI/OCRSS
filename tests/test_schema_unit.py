"""E0-S2 (unit, no database): schema files and migration chain traced to ARCHITECTURE §4."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from cdi_adapter import repo

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
VIEWS = (ROOT / "db" / "views.sql").read_text(encoding="utf-8")
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _load(rev_file: str):
    path = ROOT / "db" / "alembic" / "versions" / rev_file
    spec = importlib.util.spec_from_file_location(rev_file[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _arch_section_4_1_tables() -> set[str]:
    sec = ARCH.split("### 4.1 Table groups")[1].split("> This table describes")[0]
    names: set[str] = set()
    for row in sec.splitlines():
        cells = row.split("|")
        # the derived rows (read models, S7 continuity and summary) are not §4.1 base tables
        derived = ("Read model", "S7 continuity", "S7 summary")
        if len(cells) > 3 and not cells[1].strip().startswith(derived):
            names.update(re.findall(r"`([a-z_]+)`", cells[2]))
    return names


def test_schema_defines_exactly_the_20_tables_of_section_4_1():
    created = set(re.findall(r"^CREATE TABLE (\w+)", SCHEMA, re.M))
    assert created == _arch_section_4_1_tables()
    assert len(created) == 20  # "20 tables in 13 groups"


def test_views_define_the_5_read_models_of_section_4_1():
    created = set(re.findall(r"CREATE OR REPLACE VIEW (\w+)", VIEWS))
    assert created == {
        "v_problem_list",
        "v_medication_list",
        "v_allergy_list",
        "v_results_grid",
        "v_encounter_timeline",
    }
    assert all(v in ARCH for v in created)


def test_clinical_fact_columns_match_section_4_2():
    body = SCHEMA.split("CREATE TABLE clinical_fact (")[1].split(");")[0]
    cols = set(re.findall(r"^\s{4}(\w+)\s", body, re.M))
    required = {
        "patient_id", "encounter_id", "fact_type", "code_system", "code", "code_display",
        "code_status", "local_text", "value_kind", "value_num", "value_unit_ucum",
        "value_code_system", "value_code", "value_code_display", "value_text", "value_bool",
        "value_low_num", "value_high_num", "ref_range_low", "ref_range_high", "ref_range_text",
        "abnormal_flag", "clinical_status", "verification", "onset", "effective_time",
        "asserted_time", "extraction_id", "source_doc_ids", "confidence_overall",
        "confidence_ocr", "confidence_extract", "confidence_terminology", "review_state",
        "supersedes", "is_current", "dedup_key",
    }  # fmt: skip
    assert required <= cols


@pytest.mark.parametrize(
    "column,values",
    [
        ("review_state", ["pending", "auto_accepted", "in_review", "clinician_confirmed",
                          "corrected", "rejected"]),
        ("code_status", ["unmapped", "candidate", "bound", "local_only"]),
        ("fact_type", ["condition", "symptom", "finding", "lab_result", "vital_sign",
                       "procedure", "medication", "allergy", "immunization",
                       "diagnostic_report", "advice", "care_plan", "referral", "document"]),
    ],
)  # fmt: skip
def test_clinical_fact_check_constraints_match_section_4_2(column, values):
    body = SCHEMA.split("CREATE TABLE clinical_fact (")[1].split(");")[0]
    check = re.search(rf"{column}\s.*?CHECK \({column} IN \((.*?)\)\)", body, re.S).group(1)
    assert re.findall(r"'(\w+)'", check) == values


def test_fact_provenance_and_fhir_columns_match_sections_4_3_4_4():
    prov = SCHEMA.split("CREATE TABLE fact_provenance (")[1].split(");")[0]
    for col in ("ocr_block_ids", "bbox_union", "extracted_text", "pipeline_run_ids",
                "model_stack", "agent", "recorded_at"):  # fmt: skip
        assert re.search(rf"^\s+{col}\s", prov, re.M), col
    bundle = SCHEMA.split("CREATE TABLE fhir_bundle (")[1].split(");")[0]
    for col in ("care_context", "artifact_type", "bundle_hash", "fhir_version", "ig_package",
                "validation_status", "status"):  # fmt: skip
        assert re.search(rf"^\s+{col}\s", bundle, re.M), col
    assert "'draft', 'validated', 'ready_to_share', 'shared', 'superseded'" in bundle


def test_migration_0001_downgrade_drops_every_table():
    m = _load("0001_initial_schema.py")
    assert set(m.TABLES) == set(re.findall(r"^CREATE TABLE (\w+)", SCHEMA, re.M))
    assert set(m.VIEWS) == set(re.findall(r"CREATE OR REPLACE VIEW (\w+)", VIEWS))


def test_migration_chain_is_linear_0001_to_0027():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "db" / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0027"]  # 0027: conflict resolution (E5-S3)
    chain = [r.revision for r in script.walk_revisions("base", "heads")]
    assert chain == [
        "0027",
        "0026",
        "0025",
        "0024",
        "0023",
        "0022",
        "0021",
        "0020",
        "0019",
        "0018",
        "0017",
        "0016",
        "0015",
        "0014",
        "0013",
        "0012",
        "0011",
        "0010",
        "0009",
        "0008",
        "0007",
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]


def test_migration_0003_seeds_four_dummy_registry_patients():
    m = _load("0003_patient_registry.py")
    assert len(m.SEED) == 4
    assert len({s[0] for s in m.SEED}) == 4
    assert all(s[0].startswith("CFP-") for s in m.SEED)


def test_migration_0004_carries_section_4_5_names():
    m = _load("0004_canonical_emr.py")
    ddl = "\n".join(m.DDL)
    named = ("rv_item", "rv_item_element", "rv_action", "ai_event", "ai_suggestion",
             "ai_clinical_verification", "cn_lab_result", "cn_clinical_note",
             "cn_fhir_bundle")  # fmt: skip
    for t in named:
        assert re.search(rf"CREATE TABLE {t} \(", ddl), t
    assert any(t.startswith("ops_") for t in m.DROP_ORDER)
    assert any(t.startswith("fin_") for t in m.DROP_ORDER)
    lab = ddl.split("CREATE TABLE cn_lab_result")[1].split("CREATE TABLE")[0]
    assert "test_code" in lab and not re.search(r"[\s,(]code text", lab)  # §4.5 bug fix
    created = set(re.findall(r"CREATE TABLE (\w+)", ddl))
    assert created == set(m.DROP_ORDER)  # downgrade drops everything upgrade creates


def test_repo_rejects_unknown_enums_before_touching_the_db():
    with pytest.raises(ValueError):
        repo.start_run(None, document_id=None, stage="teleport")
    with pytest.raises(ValueError):
        repo.write_audit(None, actor="t", action="erase", entity="x")
    with pytest.raises(ValueError):
        repo.set_document_status(None, "00000000-0000-0000-0000-000000000000", "done")
    assert set(repo.DOCUMENT_STATUSES) == set(
        re.search(r"status\s+text NOT NULL DEFAULT 'received' CHECK \(status IN \((.*?)\)\)",
                  SCHEMA, re.S).group(1).replace("'", "").replace(" ", "").replace("\n", "")
        .split(",")
    )  # fmt: skip
