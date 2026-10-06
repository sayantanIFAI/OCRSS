"""E12-S1: the deterministic workflow orchestrator with bounded agent tools (real PostgreSQL).

AC: every document transition is persisted and replayable; agent/tool calls are allow-listed,
schema-validated and time-bounded; a negative test proves an agent cannot directly set
auto_accepted / clinician_confirmed / ready_to_share; the S6 gate remains the sole machine
authority for fact acceptance."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.orchestrator import workflow
from cdi_adapter.orchestrator.__main__ import main as cli
from cdi_adapter.orchestrator.tools import (
    Tool,
    ToolError,
    ToolRegistry,
    agent_session,
    default_registry,
)
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


def _governed_doc() -> str:
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    return doc


def _states(doc: str) -> dict[str, str]:
    with session_scope() as c:
        return {f["local_text"]: f["review_state"] for f in repo.get_facts_for_document(c, doc)}


# ------------------------------------------------------------------ transitions, persisted


def test_every_transition_is_persisted_in_order(db_env):
    doc = _governed_doc()
    with session_scope() as c:
        rec = workflow.steps(c, doc)
    assert [s["stage"] for s in rec] == ["terminology", "validate"]  # the stages that ran
    assert all(s["status"] == "ok" and s["pipeline_run_id"] for s in rec)
    v = rec[-1]
    assert v["outputs"]["auto_accepted"] == 7 and v["document_status"] == "validated"
    assert v["started_at"] <= v["finished_at"]


def test_a_failed_stage_is_recorded_too(db_env):
    doc, _ = _doc_with_ocr(_rx())
    with session_scope() as c:
        run = repo.start_run(c, document_id=doc, stage="ocr", model_name="rapidocr-onnxruntime")
        repo.fail_run(c, run, error="engine crashed")
    with session_scope() as c:
        (s,) = workflow.steps(c, doc)
    assert (s["stage"], s["status"], s["error"]) == ("ocr", "failed", "engine crashed")


def test_the_record_is_append_only(db_env):
    doc = _governed_doc()
    for sql in (
        "UPDATE workflow_step SET status = 'failed' WHERE document_id = :d",
        "DELETE FROM workflow_step WHERE document_id = :d",
    ):
        with pytest.raises(IntegrityError, match="append-only"), get_engine().begin() as c:
            c.execute(text(sql), {"d": doc})


# ------------------------------------------------------------------ replayable


def test_the_replay_plan_reproduces_each_recorded_step(db_env):
    doc, _ = _doc_with_ocr(_rx())
    with session_scope() as c:  # what the pipeline records: RapidOCR, then the VLM pass
        for engine in ("rapidocr", "vlm"):
            run = repo.start_run(c, document_id=doc, stage="ocr", model_name=engine)
            repo.finish_run(c, run, metrics={"engine": engine, "blocks": 3})
        run = repo.start_run(c, document_id=doc, stage="classify")
        repo.finish_run(c, run, metrics={})
    with session_scope() as c:
        plan = workflow.replay_plan(c, doc)
        from_classify = workflow.replay_plan(c, doc, from_stage="classify")
    assert [(p["stage"], p["args"]) for p in plan] == [
        ("ocr", {"force_engine": "rapidocr"}), ("ocr", {"force_engine": "vlm"}),
        ("classify", {})]  # fmt: skip
    assert [p["stage"] for p in from_classify] == ["classify"]


def test_a_replay_re_runs_the_steps_and_gives_the_same_result(db_env):
    doc = _governed_doc()
    before = _states(doc)
    out = workflow.replay(doc, actor="test-replay")
    assert out["status"] == "ok"
    assert [r["stage"] for r in out["results"]] == ["terminology", "validate"]
    assert _states(doc) == before  # deterministic: the same decisions again
    with session_scope() as c:
        rec = workflow.steps(c, doc)
        row = c.execute(text("SELECT status, requested_by, plan FROM workflow_replay "
                             "WHERE id = :i"), {"i": out["replay_id"]}).one()  # fmt: skip
    assert [s["stage"] for s in rec] == ["terminology", "validate", "terminology", "validate"]
    assert row.status == "ok" and row.requested_by == "test-replay" and len(row.plan) == 2


def test_a_replay_stops_and_records_the_first_failure(db_env):
    doc = _governed_doc()

    def broken(_doc: str) -> None:
        raise RuntimeError("terminology service down")

    out = workflow.replay(doc, runners={"terminology": broken, "validate": validate_document})
    assert out["status"] == "failed"
    assert out["results"] == [{"stage": "terminology", "status": "failed",
                               "error": "RuntimeError: terminology service down"}]  # fmt: skip


# ------------------------------------------------------------------ bounded agent tools


def _calls(agent: str) -> list[dict]:
    with session_scope() as c:
        return [dict(r) for r in c.execute(
            text("SELECT tool, status, reason, output FROM agent_tool_call WHERE agent = :a "
                 "ORDER BY id"), {"a": agent}).mappings()]  # fmt: skip


def _registry(**tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools.values():
        reg.register(t, agents=("test_agent",))
    return reg


ECHO_OUT = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}
ECHO_IN = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}},
           "additionalProperties": False}  # fmt: skip


def test_tools_are_allow_listed(db_env):
    agent = "agent-" + uuid.uuid4().hex[:6]
    reg = _registry(echo=Tool("echo", "", ECHO_IN, ECHO_OUT, lambda ctx, p: p))
    with pytest.raises(ToolError, match="refused"):
        reg.call(agent, "echo", {"n": 1})  # this agent may not use it
    with pytest.raises(ToolError, match="refused"):
        reg.call("test_agent", "shell.exec", {"cmd": "rm -rf /"})  # no such tool
    assert reg.call("test_agent", "echo", {"n": 1}) == {"n": 1}
    assert [c["status"] for c in _calls(agent)] == ["refused"]
    assert _calls(agent)[0]["reason"].endswith("is not allowed to use it")


def test_tool_input_and_output_are_schema_validated(db_env):
    reg = _registry(bad=Tool("bad", "", ECHO_IN, ECHO_OUT, lambda ctx, p: {"n": "not a number"}),
                    echo=Tool("echo", "", ECHO_IN, ECHO_OUT, lambda ctx, p: p))  # fmt: skip
    with pytest.raises(ToolError) as e:
        reg.call("test_agent", "echo", {"n": "x", "extra": 1})
    assert e.value.status == "invalid_input"
    with pytest.raises(ToolError) as e:
        reg.call("test_agent", "bad", {"n": 1})
    assert e.value.status == "invalid_output"


def test_tool_calls_are_time_bounded(db_env):
    reg = _registry(slow=Tool("slow", "", ECHO_IN, ECHO_OUT,
                              lambda ctx, p: time.sleep(3) or p, timeout_s=0.3))  # fmt: skip
    t0 = time.monotonic()
    with pytest.raises(ToolError) as e:
        reg.call("test_agent", "slow", {"n": 1})
    assert e.value.status == "timeout" and time.monotonic() - t0 < 2


def test_every_call_is_logged_with_its_outcome(db_env):
    agent = "logged-" + uuid.uuid4().hex[:6]
    reg = ToolRegistry()
    reg.register(Tool("echo", "", ECHO_IN, ECHO_OUT, lambda ctx, p: p), agents=(agent,))
    reg.call(agent, "echo", {"n": 7})
    with pytest.raises(ToolError):
        reg.call(agent, "echo", {"n": "x"})
    assert [(c["tool"], c["status"]) for c in _calls(agent)] == [
        ("echo", "ok"),
        ("echo", "invalid_input"),
    ]
    assert _calls(agent)[0]["output"] == {"n": 7}


def test_the_default_tools_work_through_the_registry(db_env):
    doc = _governed_doc()
    reg = default_registry()
    got = reg.call("reviewer_copilot", "terminology.lookup",
                   {"fact_type": "condition", "text": "Hypertension"})  # fmt: skip
    assert got and got["system"] == "http://snomed.info/sct"
    with session_scope() as c:
        fact = next(f for f in repo.get_facts_for_document(c, doc)
                    if f["fact_type"] == "medication")  # fmt: skip
    lines = reg.call("grounding_critic", "evidence.lines", {"fact_id": str(fact["id"])},
                     document_id=doc)  # fmt: skip
    assert lines and all("text" in ln and len(ln["bbox"]) == 4 for ln in lines)


# ------------------------------------------------------------------ the AC's negative test


def test_an_agent_cannot_set_a_governing_state(db_env):
    doc = _governed_doc()
    with session_scope() as c:
        fact = next(f for f in repo.get_facts_for_document(c, doc)
                    if f["review_state"] == "auto_accepted")  # fmt: skip
        c.execute(text("UPDATE clinical_fact SET review_state = 'in_review' WHERE id = :f"),
                  {"f": fact["id"]})  # fmt: skip
    for sql in (
        "UPDATE clinical_fact SET review_state = 'auto_accepted' WHERE id = :f",
        "UPDATE clinical_fact SET review_state = 'clinician_confirmed' WHERE id = :f",
    ):
        with pytest.raises(IntegrityError, match="may not set clinical_fact.review_state"):
            with agent_session("rogue_agent") as c:
                c.execute(text(sql), {"f": fact["id"]})
    bundle = {"resourceType": "Bundle"}
    with pytest.raises(IntegrityError, match="may not set fhir_bundle.status"):
        with agent_session("rogue_agent") as c:
            c.execute(text("INSERT INTO fhir_bundle (source_document_id, artifact_type, bundle, "
                           "  bundle_hash, status) VALUES (:d, 'PrescriptionRecord', "
                           "  CAST(:b AS jsonb), repeat('a', 64), 'ready_to_share')"),
                      {"d": doc, "b": json.dumps(bundle)})  # fmt: skip
    with pytest.raises(IntegrityError, match="may not set cn_fhir_bundle.status"):
        with agent_session("rogue_agent") as c:
            pid = c.execute(text("SELECT patient_id FROM clinical_fact WHERE id = :f"),
                            {"f": fact["id"]}).scalar_one()  # fmt: skip
            c.execute(text("INSERT INTO cn_fhir_bundle (patient_id, artifact_type, bundle, "
                           "  bundle_hash, validation, status) VALUES (:p, 'PrescriptionRecord', "
                           "  CAST(:b AS jsonb), repeat('a', 64), '{}', 'ready_to_share')"),
                      {"p": pid, "b": json.dumps(bundle)})  # fmt: skip
    with agent_session("rogue_agent") as c:  # proposing / holding is still allowed to agents
        c.execute(text("UPDATE clinical_fact SET review_state = 'in_review' WHERE id = :f"),
                  {"f": fact["id"]})  # fmt: skip
    # the S6 gate is not an agent: it remains the authority that auto-accepts
    validate_document(doc)
    assert _states(doc)[fact["local_text"]] == "auto_accepted"


def test_a_tool_that_tries_to_govern_is_refused_and_logged(db_env):
    doc = _governed_doc()
    with session_scope() as c:
        fid = str(next(f for f in repo.get_facts_for_document(c, doc))["id"])

    def sneaky(ctx, p):
        with ctx.session() as c:
            c.execute(text("UPDATE clinical_fact SET review_state = 'clinician_confirmed' "
                           "WHERE id = :f"), {"f": p["fact_id"]})  # fmt: skip
        return {"n": 1}

    agent = "sneaky-" + uuid.uuid4().hex[:6]
    reg = ToolRegistry()
    reg.register(Tool("confirm", "", {"type": "object"}, ECHO_OUT, sneaky), agents=(agent,))
    with pytest.raises(ToolError) as e:
        reg.call(agent, "confirm", {"fact_id": fid}, document_id=doc)
    assert e.value.status == "error" and "may not set" in e.value.reason
    assert [c["status"] for c in _calls(agent)] == ["error"]
    assert _states(doc)  # nothing changed by the refused write


def test_the_cli_lists_steps_plans_and_tools(db_env, capsys):
    doc = _governed_doc()
    capsys.readouterr()

    def last() -> dict:
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert cli(["steps", doc]) == 0 and [s["stage"] for s in last()["steps"]] == [
        "terminology", "validate"]  # fmt: skip
    assert cli(["replay", doc, "--dry-run"]) == 0 and len(last()["plan"]) == 2
    assert cli(["tools"]) == 0
    names = {t["name"] for t in last()["tools"]}
    assert names == {"terminology.lookup", "catalogue.search", "evidence.lines"}
