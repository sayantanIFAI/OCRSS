"""E12-S5: agent trace logging (real PostgreSQL).

AC: for any document, the full sequence of agent decisions that led to its final facts can be
reconstructed from agent_trace alone, cross-referenced to the fact_provenance rows it produced.
Every step is append-only and also in audit_log; a tool-registry call about a document is a step
too; a re-extraction that purges a fact keeps the steps that decided on it."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.orchestrator import trace
from cdi_adapter.orchestrator.__main__ import main as cli
from cdi_adapter.orchestrator.tools import Tool, ToolError, ToolRegistry
from tests.test_orchestrator_integration import _governed_doc

pytestmark = pytest.mark.integration

IMMUTABLE = "agent_trace .* is an audit record"
OUT = {"type": "object"}


def _registry(agent: str) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("echo", "", {"type": "object"}, OUT, lambda ctx, p: {"seen": p}),
                 agents=(agent,))  # fmt: skip
    return reg


def _facts(doc: str) -> list[dict]:
    with session_scope() as c:
        return repo.get_facts_for_document(c, doc)


def _decisions(doc: str) -> dict:
    with session_scope() as c:
        return trace.decisions(c, doc)


# ------------------------------------------------------------------ the AC: rebuilt from the trace


def test_a_documents_agent_decisions_rebuild_from_the_trace_alone(db_env):
    doc = _governed_doc()  # the S6 gate's resolution cascade logs one agent step per fact
    fact = str(_facts(doc)[0]["id"])
    agent = "copilot-" + uuid.uuid4().hex[:6]
    reg = _registry(agent)
    reg.call(agent, "echo", {"q": 1}, document_id=doc, fact_id=fact)
    reg.call(agent, "echo", {"q": 2}, document_id=doc)

    got = _decisions(doc)
    assert got["complete"] and got["gaps"] == {"steps_without_audit": [],
                                               "tool_calls_without_step": []}  # fmt: skip
    ids = [s["id"] for s in got["steps"]]
    assert ids == sorted(ids) and len(ids) > 2  # the order they were written
    assert all(s["audit_log_id"] for s in got["steps"])
    # every current fact: the agent steps that decided on it and the provenance it produced
    with session_scope() as c:
        prov = {str(r[0]): str(r[1]) for r in c.execute(
            text("SELECT p.id, p.fact_id FROM fact_provenance p JOIN clinical_fact f "
                 "ON f.id = p.fact_id WHERE CAST(:d AS uuid) = ANY(f.source_doc_ids)"),
            {"d": doc})}  # fmt: skip
    assert {f["id"] for f in got["facts"]} == {str(f["id"]) for f in _facts(doc)}
    for f in got["facts"]:
        assert f["agent_steps"], f"no agent step for fact {f['id']}"
        assert {p["id"] for p in f["provenance"]} == {p for p, fid in prov.items()
                                                      if fid == f["id"]}  # fmt: skip
    by_id = {s["id"]: s for s in got["steps"]}
    mine = [s for s in got["steps"] if s["agent"] == agent]
    assert [(s["step"], s["tool"], s["seq"], s["tool_status"]) for s in mine] == [
        ("tool_call", "echo", 1, "ok"),
        ("tool_call", "echo", 2, "ok"),
    ]
    assert mine[0]["fact_ref"] == fact and mine[0]["id"] in next(
        f["agent_steps"] for f in got["facts"] if f["id"] == fact
    )
    assert mine[0]["input"] == {"q": 1} and mine[0]["output"]["output"] == {"seen": {"q": 1}}
    assert all(by_id[i]["fact_ref"] == f["id"] for f in got["facts"] for i in f["agent_steps"])


def test_each_step_is_in_audit_log_as_the_agent(db_env):
    doc = _governed_doc()
    agent = "copilot-" + uuid.uuid4().hex[:6]
    _registry(agent).call(agent, "echo", {"q": 1}, document_id=doc)
    (step,) = [s for s in _decisions(doc)["steps"] if s["agent"] == agent]
    with session_scope() as c:
        a = c.execute(text("SELECT actor, action, detail FROM audit_log WHERE id = :i"),
                      {"i": step["audit_log_id"]}).mappings().one()  # fmt: skip
    assert a["actor"] == agent and a["action"] == "create"
    assert a["detail"]["document_id"] == doc and a["detail"]["step"] == "tool_call"
    assert a["detail"]["tool_call_id"] == step["tool_call_id"]


def test_a_refused_tool_call_is_a_step_too(db_env):
    doc = _governed_doc()
    agent = "copilot-" + uuid.uuid4().hex[:6]
    with pytest.raises(ToolError):
        _registry(agent).call("someone-else", "echo", {}, document_id=doc)
    with pytest.raises(ToolError):
        _registry(agent).call(agent, "no.such.tool", {}, document_id=doc)
    got = _decisions(doc)
    assert got["complete"]
    tail = got["steps"][-2:]
    assert [(s["agent"], s["tool"], s["tool_status"]) for s in tail] == [
        ("someone-else", "echo", "refused"),
        (agent, "no.such.tool", "refused"),
    ]
    assert tail[1]["output"]["reason"] == "no such tool"


def test_a_call_not_about_a_stored_document_is_only_a_tool_call(db_env):
    agent = "copilot-" + uuid.uuid4().hex[:6]
    missing = str(uuid.uuid4())
    assert _registry(agent).call(agent, "echo", {"q": 1}, document_id=missing) == {"seen": {"q": 1}}
    with session_scope() as c:
        n = c.execute(text("SELECT count(*) FROM agent_trace WHERE agent = :a"),
                      {"a": agent}).scalar_one()  # fmt: skip
    assert n == 0


# ------------------------------------------------------------------ append-only


def test_direct_sql_cannot_change_or_remove_a_step(db_env):
    doc = _governed_doc()
    sid = _decisions(doc)["steps"][0]["id"]
    for sql in ("UPDATE agent_trace SET output = '{}'::jsonb WHERE id = :i",
                "UPDATE agent_trace SET confidence = 0.01 WHERE id = :i",
                "UPDATE agent_trace SET fact_ref = NULL WHERE id = :i",
                "DELETE FROM agent_trace WHERE id = :i"):  # fmt: skip
        with pytest.raises(IntegrityError, match=IMMUTABLE), get_engine().begin() as c:
            c.execute(text(sql), {"i": sid})
    with pytest.raises(IntegrityError, match="never truncated"), get_engine().begin() as c:
        c.execute(text("TRUNCATE agent_trace CASCADE"))
    with pytest.raises(IntegrityError), get_engine().begin() as c:  # cascade from the document
        c.execute(text("DELETE FROM source_document WHERE id = :d"), {"d": doc})
    assert _decisions(doc)["steps"][0]["id"] == sid


def test_a_re_extraction_keeps_the_steps_that_decided_on_purged_facts(db_env):
    doc = _governed_doc()
    before = _decisions(doc)
    decided = [s for s in before["steps"] if s["fact_ref"]]
    assert decided
    with session_scope() as c:  # what re-extraction does first
        assert repo.purge_document_facts(c, doc) > 0
    after = _decisions(doc)
    assert [s["id"] for s in after["steps"]] == [s["id"] for s in before["steps"]]
    assert after["facts"] == [] and after["complete"]
    assert sorted(after["superseded"]) == sorted(s["id"] for s in decided)
    kept = {s["id"]: s for s in after["steps"]}
    for s in decided:
        assert kept[s["id"]]["fact_ref"] == s["fact_ref"] and not kept[s["id"]]["fact_current"]
        assert kept[s["id"]]["output"] == s["output"]


# ------------------------------------------------------------------ gaps, and the CLI


def test_a_tool_call_without_a_step_is_a_gap(db_env, capsys):
    doc = _governed_doc()
    assert cli(["trace", doc]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["complete"] and out["document_id"] == doc
    with session_scope() as c:  # written outside the registry: not in the trace
        cid = c.execute(text("INSERT INTO agent_tool_call (agent, tool, document_id, status) "
                             "VALUES ('rogue', 'x', :d, 'ok') RETURNING id"),
                        {"d": doc}).scalar_one()  # fmt: skip
    assert cli(["trace", doc]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["gaps"]["tool_calls_without_step"] == [cid] and not out["complete"]
