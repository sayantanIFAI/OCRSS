"""Grammar-locked decoding for the vLLM backend (backlog E2-S3, ARCHITECTURE §3.4, §14 #3).

The client sends every extraction's JSON schema (``bundled_schema``: all ``$ref`` inlined). The
gateway turns it into an XGrammar EBNF grammar, and vLLM masks every token the grammar cannot
accept. So the model can only emit JSON that matches the schema.

vLLM 0.11.0 pins XGrammar 0.1.25. Checked token by token with the real Qwen2.5-VL tokenizer
(``tests/test_grammar_unit.py``), XGrammar enforces types, ``required``, ``additionalProperties:
false``, ``enum``, ``const``, ``anyOf``, ``minItems`` and integer ranges exactly, **but not a
``number`` range**: for ``minimum 0, maximum 1`` it emits ``"1." [0-9]+``, so ``1.8`` passes.
Every bounded number in our schemas is a 0-1 ``confidence``. Each one is therefore swapped for a
placeholder before conversion, and the placeholder is replaced by an exact rule afterwards.

Anything else the grammar could not enforce exactly is refused with :class:`GrammarError`
instead of being sent loosely: a schema edit that adds one fails loudly (and in CI), not as
silently unconstrained output on the pod.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

UNIT_INTERVAL = {"type": "number", "minimum": 0, "maximum": 1}
_MARK = "__cdi_unit_interval__"
_MARK_LITERAL = json.dumps(json.dumps(_MARK))  # how XGrammar writes the JSON string in EBNF
_RULE_NAME = "cdi_unit_interval"
# 0, 0.x (up to 6 decimals), 1, 1.0 (up to 6 zeros): every value is inside [0, 1]
UNIT_INTERVAL_RULE = f'{_RULE_NAME} ::= ("0" ("." [0-9]{{1,6}})?) | ("1" ("." "0"{{1,6}})?)'

_ANNOTATIONS = {"description", "title", "$id", "$schema", "$comment", "examples", "default"}
# E15-S2: a string property may name a clinical field domain, and the grammar then locks that
# field's own form (cdi_adapter.fieldgrammar) instead of allowing any string. The key is an
# annotation: it tells this converter what to do and is ignored by every JSON-Schema validator.
FIELD_KEY = "x-cdi-field"
_ANNOTATIONS |= {FIELD_KEY}


def _field_mark(domain: str) -> str:
    return f"__cdi_field_{domain}__"


def _field_rule_name(domain: str) -> str:
    return f"cdi_field_{domain}"


_SUPPORTED = _ANNOTATIONS | {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "anyOf",
    "items",
    "minItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "$defs",
}


class GenerationError(RuntimeError):
    """A constrained generation did not finish as a valid schema instance."""


def strict_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON constant")

    return json.loads(text, parse_constant=reject_constant)


def validate_output(text: str, schema: dict[str, Any]) -> None:
    """Never publish a truncated or invalid constrained response as successful generation."""
    from jsonschema import Draft202012Validator

    try:
        value = strict_json(text)
        if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
            raise GenerationError("constrained generation violated its schema")
    except (ValueError, TypeError):
        raise GenerationError("constrained generation did not produce complete JSON") from None


class GrammarError(ValueError):
    """The schema uses a feature the grammar cannot enforce exactly."""


def _is_unit_interval(node: dict[str, Any]) -> bool:
    core = {k: v for k, v in node.items() if k not in _ANNOTATIONS}
    return core == UNIT_INTERVAL


def unsupported(schema: Any, path: str = "#") -> list[str]:
    """Where ``schema`` goes beyond what the grammar enforces exactly (empty: fully enforced).

    Covers vLLM 0.11.0's own list (``has_xgrammar_unsupported_json_features``: ``multipleOf``,
    ``uniqueItems`` / ``contains`` / ``min|maxContains``, string ``format``, ``min|maxProperties``
    / ``propertyNames`` / ``patternProperties``), plus what we measured: a number range other
    than 0-1, ``maxItems`` / ``pattern`` / ``min|maxLength`` (not used by our schemas; not
    verified token by token, so refused), and a ``$ref`` left un-inlined.
    """
    out: list[str] = []
    if isinstance(schema, list):
        for i, v in enumerate(schema):
            out += unsupported(v, f"{path}/{i}")
        return out
    if not isinstance(schema, dict):
        return out
    if _is_unit_interval(schema):
        return out
    bad = [k for k in schema if k not in _SUPPORTED]
    # The confidence grammar substitution must never rewrite a literal data value.
    if any(_MARK in json.dumps(schema[k]) for k in ("const", "enum") if k in schema):
        bad.append("reserved grammar marker in literal data")
    types = schema.get("type")
    if types != "integer" and any(k in schema for k in ("minimum", "maximum", "exclusiveMinimum",
                                            "exclusiveMaximum")):  # fmt: skip
        bad.append("number range other than 0-1 or range without integer type")
    out += [f"{path}: {k}" for k in bad]
    for k, v in schema.items():
        if k == "properties" and isinstance(v, dict):  # a map of names, not a schema itself
            for name, sub in v.items():
                out += unsupported(sub, f"{path}/properties/{name}")
        elif k == "$defs" and isinstance(v, dict):
            for name, sub in v.items():
                out += unsupported(sub, f"{path}/$defs/{name}")
        elif k not in _ANNOTATIONS | {"enum", "const"} and isinstance(v, dict | list):
            out += unsupported(v, f"{path}/{k}")
    return out


def field_domain(node: Any) -> str | None:
    """The clinical field domain a schema node names, if it names a known one (E15-S2)."""
    if not isinstance(node, dict):
        return None
    domain = node.get(FIELD_KEY)
    if domain is None:
        return None
    from cdi_adapter import fieldgrammar

    if domain not in fieldgrammar.FIELDS:
        raise GrammarError(f"unknown clinical field domain {domain!r}")
    return str(domain)


def with_placeholders(schema: Any) -> Any:
    """Every 0-1 number, and every clinical field, replaced by a string constant the EBNF step
    can find and swap for its exact rule."""
    if isinstance(schema, dict):
        if _is_unit_interval(schema):
            return {"const": _MARK}
        domain = field_domain(schema)
        if domain is not None:
            return {"const": _field_mark(domain)}
        result = {}
        for k, v in schema.items():
            if k in {"properties", "$defs"} and isinstance(v, dict):
                result[k] = {name: with_placeholders(sub) for name, sub in v.items()}
            else:
                result[k] = v if k in _ANNOTATIONS | {"const", "enum"} else with_placeholders(v)
        return result
    if isinstance(schema, list):
        return [with_placeholders(v) for v in schema]
    return schema


def schema_to_ebnf(schema: dict[str, Any]) -> str:
    """The exact XGrammar EBNF for ``schema`` (cached per schema). Needs ``xgrammar``, which is
    in the gateway venv (``requirements-vllm.lock``)."""
    from jsonschema import Draft202012Validator, SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise GrammarError("invalid JSON schema") from None
    # key order kept: XGrammar emits properties in schema order, which is the order the
    # prompt asks for (sorting would force ``confidence`` first)
    return _ebnf(json.dumps(schema))


@lru_cache(maxsize=64)
def _ebnf(schema_json: str) -> str:
    import xgrammar as xgr

    schema = json.loads(schema_json)
    problems = unsupported(schema)
    if problems:
        raise GrammarError("schema cannot be grammar-locked exactly: " + "; ".join(problems[:10]))
    grammar = str(xgr.Grammar.from_json_schema(with_placeholders(schema), any_whitespace=False))
    if _MARK_LITERAL in grammar:
        grammar = grammar.replace(_MARK_LITERAL, _RULE_NAME) + "\n" + UNIT_INTERVAL_RULE + "\n"
    grammar = _with_field_rules(grammar)  # E15-S2
    xgr.Grammar.from_ebnf(grammar)  # the same parse vLLM runs before accepting the request
    return grammar


def _with_field_rules(grammar: str) -> str:
    """Replace each clinical field's marker string with its own rule, and append the rules
    (E15-S2). The field's text is a JSON string, so the rule is quoted - the model may only emit
    a reading the field grammar accepts, and nothing else."""
    from cdi_adapter import fieldgrammar

    rules = []
    for domain in fieldgrammar.FIELDS:
        literal = json.dumps(json.dumps(_field_mark(domain)))
        if literal not in grammar:
            continue
        name = _field_rule_name(domain)
        grammar = grammar.replace(literal, name)
        body = fieldgrammar.ebnf(domain).split("::=", 1)[1].strip()
        rules.append(f'{name} ::= "\\"" {body} "\\""')
    if not rules:
        return grammar
    return grammar + "\n" + "\n".join(rules) + "\n"
