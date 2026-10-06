"""Bounded agent tools (backlog E12-S1, ADR-0003): allow-listed, schema-validated, time-bounded.

An agent never gets a free hand on the database or the pipeline: it calls named tools through a
``ToolRegistry``. Each call is

* **allow-listed** - a tool that is not registered (or an agent not allowed to use it) is refused;
* **schema-validated** - its input before it runs and its output after (JSON Schema);
* **time-bounded** - it must answer within its ``timeout_s`` (the caller gets ``timeout``);
* **logged** - one ``agent_tool_call`` row per call, refused ones included (migration 0019);
  a call about a document is also an agent step in ``agent_trace`` (E12-S5), in order, pointing
  at its ``agent_tool_call`` row, so the document's agent decisions rebuild from the trace alone;
* **governed** - the database session a tool gets is marked as an agent's
  (``SET LOCAL cdi.actor_kind = 'agent'``), so the database refuses any attempt to set
  ``auto_accepted`` / ``clinician_confirmed`` or ``ready_to_share``: the S6 gate stays the only
  machine authority, agents only propose.

``default_registry()`` holds the read-only tools agents may use today: a terminology lookup, a
catalogue search and a fact's evidence lines.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import Connection, text

from cdi_adapter.db import session_scope


class ToolError(Exception):
    """A tool call that did not produce a valid answer; ``status`` is what was logged."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status, self.reason = status, reason


@contextmanager
def agent_session(agent: str) -> Iterator[Connection]:
    """A database session marked as the agent's: governing states are refused in it."""
    with session_scope() as conn:
        conn.execute(text("SELECT set_config('cdi.actor_kind', 'agent', true), "
                          "set_config('cdi.agent', :a, true)"), {"a": agent})  # fmt: skip
        yield conn


@dataclass(frozen=True)
class ToolContext:
    agent: str
    document_id: str | None

    def session(self) -> Any:
        return agent_session(self.agent)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    fn: Callable[[ToolContext, dict[str, Any]], Any]
    timeout_s: float = 10.0


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    # agent -> the tool names it may call; an agent not listed may call none
    allowed: dict[str, frozenset[str]] = field(default_factory=dict)

    def register(self, tool: Tool, *, agents: tuple[str, ...] = ()) -> None:
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator.check_schema(tool.output_schema)
        self.tools[tool.name] = tool
        for a in agents:
            self.allowed[a] = self.allowed.get(a, frozenset()) | {tool.name}

    def call(self, agent: str, name: str, payload: dict[str, Any], *,
             document_id: str | None = None, run_id: str | None = None,
             fact_id: str | None = None) -> Any:  # fmt: skip
        t0 = time.monotonic()
        tool = self.tools.get(name)

        def log(status: str, output: Any = None, reason: str | None = None) -> None:
            _log(agent, name, document_id, status, payload, output, reason,
                 int((time.monotonic() - t0) * 1000), run_id=run_id, fact_id=fact_id)  # fmt: skip

        if tool is None or name not in self.allowed.get(agent, frozenset()):
            why = "no such tool" if tool is None else f"{agent} is not allowed to use it"
            log("refused", reason=why)
            raise ToolError("refused", why)
        bad = _first_error(tool.input_schema, payload)
        if bad:
            log("invalid_input", reason=bad)
            raise ToolError("invalid_input", bad)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        future = pool.submit(tool.fn, ToolContext(agent, document_id), payload)
        try:
            out = future.result(timeout=tool.timeout_s)
        except FutureTimeout:
            log("timeout", reason=f"no answer within {tool.timeout_s}s")
            raise ToolError("timeout", f"no answer within {tool.timeout_s}s") from None
        except Exception as exc:  # noqa: BLE001 - logged, then raised as the tool's failure
            log("error", reason=f"{type(exc).__name__}: {exc}"[:500])
            raise ToolError("error", f"{type(exc).__name__}: {exc}") from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        bad = _first_error(tool.output_schema, out)
        if bad:
            log("invalid_output", output=_jsonable(out), reason=bad)
            raise ToolError("invalid_output", bad)
        log("ok", output=_jsonable(out))
        return out


def _first_error(schema: dict[str, Any], value: Any) -> str | None:
    err = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    return None if err is None else f"{'/'.join(map(str, err.absolute_path)) or '$'}: {err.message}"


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _log(agent: str, tool: str, document_id: str | None, status: str, payload: Any,
         output: Any, reason: str | None, ms: int, *, run_id: str | None = None,
         fact_id: str | None = None) -> None:  # fmt: skip
    with session_scope() as c:
        call_id = c.execute(
            text("INSERT INTO agent_tool_call (agent, tool, document_id, status, input, output, "
                 "  reason, duration_ms) VALUES (:a, :t, :d, :s, CAST(:i AS jsonb), "
                 "  CAST(:o AS jsonb), :r, :ms) RETURNING id"),
            {"a": agent, "t": tool, "d": document_id, "s": status,
             "i": json.dumps(_jsonable(payload)), "o": json.dumps(output) if output is not None
             else None, "r": reason, "ms": ms},
        ).scalar_one()  # fmt: skip
        stored = document_id is not None and c.execute(
            text("SELECT 1 FROM source_document WHERE id::text = :d"), {"d": str(document_id)}
        ).first()  # fmt: skip
        if not stored:
            return  # a call not about a stored document is in agent_tool_call only
        # the step's place in the agent's sequence for this document (and run)
        c.execute(text("SELECT pg_advisory_xact_lock(hashtext('agent_trace:' || :d))"),
                  {"d": str(document_id)})  # fmt: skip
        c.execute(
            text("INSERT INTO agent_trace (document_id, run_id, fact_id, seq, agent, step, tool, "
                 "  input, output, duration_ms, tool_call_id) "
                 "SELECT CAST(:d AS uuid), CAST(:run AS uuid), CAST(:f AS uuid), "
                 "  coalesce(max(seq), 0) + 1, :a, 'tool_call', :t, "
                 "  CAST(:i AS jsonb), CAST(:o AS jsonb), :ms, :cid FROM agent_trace "
                 "WHERE document_id = CAST(:d AS uuid) AND agent = :a "
                 "  AND run_id IS NOT DISTINCT FROM CAST(:run AS uuid)"),
            {"d": str(document_id), "run": run_id, "f": fact_id, "a": agent, "t": tool,
             "i": json.dumps(_jsonable(payload)),
             "o": json.dumps({"status": status, "output": output, "reason": reason}),
             "ms": ms, "cid": call_id},
        )  # fmt: skip


# ------------------------------------------------------------------ the tools agents use today

_TEXT_IN = {"type": "object", "additionalProperties": False}


def _terminology_lookup(ctx: ToolContext, p: dict[str, Any]) -> dict[str, Any] | None:
    from cdi_adapter.terminology.service import bind_text

    b = bind_text(p["fact_type"], p["text"])
    if b is None:
        return None
    return {"system": b.concept.system, "code": b.concept.code, "display": b.concept.display,
            "how": b.how}  # fmt: skip


def _catalogue_search(ctx: ToolContext, p: dict[str, Any]) -> list[dict[str, Any]]:
    with ctx.session() as c:
        rows = c.execute(
            text("SELECT id, name, manufacturer, composition FROM med_catalogue "
                 "WHERE name ILIKE :q ORDER BY length(name), name LIMIT :n"),
            {"q": f"%{p['text']}%", "n": p.get("limit", 10)},
        ).mappings().all()  # fmt: skip
    return [{k: (str(v) if v is not None else None) for k, v in dict(r).items()} for r in rows]


def _evidence_lines(ctx: ToolContext, p: dict[str, Any]) -> list[dict[str, Any]]:
    with ctx.session() as c:
        rows = c.execute(
            text("SELECT o.raw_text, o.bbox, o.engine, o.ocr_conf FROM fact_provenance f "
                 "JOIN ocr_observation o ON o.id = ANY(f.ocr_block_ids) "
                 "WHERE f.fact_id = CAST(:f AS uuid) ORDER BY o.reading_order"),
            {"f": p["fact_id"]},
        ).mappings().all()  # fmt: skip
    return [{"text": r["raw_text"], "bbox": list(r["bbox"]), "engine": r["engine"],
             "confidence": r["ocr_conf"]} for r in rows]  # fmt: skip


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    agents = ("medication_agent.v1", "reviewer_copilot", "grounding_critic")
    reg.register(Tool(
        "terminology.lookup", "The standard code a text maps to (S5 seed map), or null",
        _TEXT_IN | {"required": ["fact_type", "text"], "properties": {
            "fact_type": {"type": "string"}, "text": {"type": "string", "maxLength": 300}}},
        {"type": ["object", "null"], "required": ["system", "code", "display", "how"]},
        _terminology_lookup, timeout_s=5.0), agents=agents)  # fmt: skip
    reg.register(Tool(
        "catalogue.search", "Medicine products whose name contains the text",
        _TEXT_IN | {"required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 2, "maxLength": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        {"type": "array", "items": {"type": "object", "required": ["id", "name"]}},
        _catalogue_search, timeout_s=5.0), agents=agents)  # fmt: skip
    reg.register(Tool(
        "evidence.lines", "The OCR lines a fact was read from (its evidence)",
        _TEXT_IN | {"required": ["fact_id"], "properties": {
            "fact_id": {"type": "string", "format": "uuid"}}},
        {"type": "array", "items": {"type": "object", "required": ["text", "bbox"]}},
        _evidence_lines, timeout_s=5.0), agents=agents)  # fmt: skip
    return reg
