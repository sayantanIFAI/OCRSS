"""E10-S5 "canary + rollback drill" on real PostgreSQL with real gateways over HTTP.

Two model gateways (the real ``mlserve`` app on loopback ports, each serving its own registered
version; the models are stubs with different names, since no GPU is here) take real extractions
through the router. The drill: deploy -> canary at a share -> the live comparison -> one-command
rollback of the canary -> canary again -> promote -> one-command rollback to the previous primary,
including when its gateway has to be started again first. The GPU version of the drill is
``scripts/model_rollout_drill.py`` (Colab / the pod).
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from sqlalchemy import text

from cdi_adapter import db
from cdi_adapter.db import session_scope
from cdi_adapter.extract.service import extract_document, extract_documents
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import StubBackend
from cdi_adapter.modelops import compare, ops, registry, router
from cdi_adapter.modelops.registry import RolloutError
from tests.test_batch_integration import MemoryStore
from tests.test_extract_integration import _doc_with_ocr
from tests.test_metrics_unit import value
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

RUN = uuid.uuid4().hex[:6]  # the shared test database is never reset: names of this run only


class Named(StubBackend):
    def __init__(self, model: str, mode: str = "ok") -> None:
        super().__init__()
        self.model, self.mode = model, mode

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        if self.mode == "broken":
            raise RuntimeError("canary crashed")
        if self.mode == "garbage":
            return "I cannot read this page."
        return super().generate(prompt, image_png, max_tokens=max_tokens, json_schema=json_schema)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def gateway(model: str, version: str, mode: str = "ok",
            port: int | None = None) -> Iterator[tuple[str, uvicorn.Server]]:  # fmt: skip
    port = port or _free_port()
    app = create_app(Named(model, mode), version=version)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        t.join(10)


@pytest.fixture
def rollout(db_env, monkeypatch):
    """A clean rollout log, HTTP routing on, no cache. The log is append-only for everyone; only
    this test fixture, as the table owner, lifts the trigger to start from nothing."""
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "hf")  # the web app side: route over HTTP
    monkeypatch.setenv("CDI_MODEL_ROUTE_TTL_S", "0")
    monkeypatch.setenv("CDI_EXTRACT_RETRIES", "0")
    get_settings.cache_clear()
    db.reset_engine()

    def wipe() -> None:
        with session_scope() as c:
            c.execute(text("ALTER TABLE model_rollout DISABLE TRIGGER model_rollout_no_rewrite"))
            c.execute(text("DELETE FROM model_rollout"))
            c.execute(text("ALTER TABLE model_rollout ENABLE TRIGGER model_rollout_no_rewrite"))

    # Exercise both routes on every run; random UUIDs can all hash to the primary.
    # Keep each document's assignment stable through retries and batch extraction.
    assignments: dict[str, int] = {}

    def balanced_bucket(document_id: str) -> int:
        return assignments.setdefault(str(document_id), (len(assignments) % 2) * 99)

    monkeypatch.setattr(router, "bucket", balanced_bucket)
    wipe()
    router.reset()
    yield
    wipe()
    router.reset()


def _register(name: str, model: str) -> str:
    version = f"{name}-{RUN}"
    ops.register(actor="test", version=version, model_id=model, revision="stub", backend="stub")
    return version


def _extract(n: int) -> list[tuple[str, str, str]]:
    """(expected role, extraction.model_version, extraction.model_name) for n new documents."""
    out = []
    for _ in range(n):
        doc, _ = _doc_with_ocr(_rx())
        expected = router.route(doc).role
        extract_document(doc, store=MemoryStore())
        with session_scope() as c:
            v, m = c.execute(text("SELECT model_version, model_name FROM extraction "
                                  "WHERE document_id = :d"), {"d": doc}).one()  # fmt: skip
        out.append((expected, v, m))
    return out


def test_canary_and_rollback_drill(rollout, monkeypatch):
    a, b = _register("drill-a", "stub-a"), _register("drill-b", "stub-b")
    with gateway("stub-a", a) as (url_a, server_a), gateway("stub-b", b) as (url_b, _):
        ops.deploy(a, url_a, actor="ops")
        assert {v for _, v, _ in _extract(3)} == {a}

        # canary: a share of new documents, by document, recorded per extraction
        ops.canary(b, url_b, 50, actor="ops", reason="drill")
        can0 = value("cdi_model_extractions_total", role="canary", outcome="complete")
        got = _extract(12)
        for role, v, m in got:
            assert (v, m) == ((b, "stub-b") if role == "canary" else (a, "stub-a"))
        n_canary = sum(r == "canary" for r, _, _ in got)
        assert 0 < n_canary < 12
        assert value("cdi_model_extractions_total", role="canary", outcome="complete") == (
            can0 + n_canary
        )

        # the live comparison sees both sides on the same window
        with session_scope() as c:
            rep = compare.compare(c)
        assert rep["canary"]["version"] == b and rep["canary"]["documents"] == n_canary
        assert rep["primary"]["documents"] == 12 - n_canary
        assert rep["canary"]["served_by"] == ["stub-b"]
        assert not rep["promote_ok"]  # fewer than 20 canary documents
        assert any("E2-S7" in n for n in rep["notes"])

        # ONE COMMAND: the canary is gone for every new document
        st = ops.rollback(actor="ops", reason="drill: canary rollback")
        assert (st.primary_version, st.canary_version, st.canary_percent) == (a, None, 0)
        assert {v for _, v, _ in _extract(6)} == {a}

        # canary again, promote (the gate lowered for a short drill), then roll back the promotion
        ops.canary(b, url_b, 50, actor="ops")
        _extract(8)
        monkeypatch.setitem(compare.LIMITS, "min_documents", 1)
        st, rep = ops.promote(actor="ops", reason="drill")
        assert rep["promote_ok"] and (st.primary_version, st.primary_url) == (b, url_b)
        assert {v for _, v, _ in _extract(4)} == {b}

        def no_launch(*_):
            raise AssertionError("the previous gateway is still up: nothing to start")

        st = ops.rollback(actor="ops", reason="drill: promotion rollback", launch=no_launch)
        assert (st.primary_version, st.primary_url) == (a, url_a)
        assert {v for _, v, _ in _extract(4)} == {a}

        # promote again, stop the old gateway: rollback starts it again before switching
        ops.canary(b, url_b, 50, actor="ops")
        _extract(8)
        ops.promote(actor="ops", reason="drill")
        server_a.should_exit = True
        port_a = int(url_a.rsplit(":", 1)[1])
        deadline = time.monotonic() + 10
        while ops.gateway_health(url_a) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        started = []

        def launch(version, url):
            cm = gateway(version["model_id"], version["version"], port=port_a)
            cm.__enter__()
            started.append(cm)

        try:
            st = ops.rollback(actor="ops", reason="drill: gateway restart", launch=launch)
            assert len(started) == 1
            assert (st.primary_version, st.primary_url) == (a, url_a)
            assert {v for _, v, _ in _extract(3)} == {a}
        finally:
            for cm in started:
                cm.__exit__(None, None, None)

    with session_scope() as c:
        actions = [s.action for s in reversed(registry.history(c))]
        audits = c.execute(text("SELECT count(*) FROM audit_log WHERE entity = 'model_rollout' "
                                "AND at > now() - interval '10 minutes'")).scalar_one()  # fmt: skip
    assert actions == ["deploy", "canary", "rollback", "canary", "promote", "rollback", "canary",
                       "promote", "rollback"]  # fmt: skip
    assert audits >= len(actions)


def test_a_broken_canary_costs_a_retry_never_a_document(rollout):
    a, b = _register("fb-a", "stub-a"), _register("fb-b", "stub-b")
    with gateway("stub-a", a) as (url_a, _), gateway("stub-b", b, "broken") as (url_b, _):
        ops.deploy(a, url_a, actor="ops")
        ops.canary(b, url_b, 50, actor="ops")
        before = value("cdi_model_canary_fallbacks_total")
        got = _extract(10)
        assert {v for _, v, _ in got} == {a}  # every document extracted, by the primary
        n_canary = sum(r == "canary" for r, _, _ in got)
        assert n_canary > 0 and value("cdi_model_canary_fallbacks_total") == before + n_canary


def test_a_batch_never_mixes_the_two_sides(rollout):
    a, b = _register("mix-a", "stub-a"), _register("mix-b", "stub-b")
    with gateway("stub-a", a) as (url_a, _), gateway("stub-b", b) as (url_b, _):
        ops.deploy(a, url_a, actor="ops")
        ops.canary(b, url_b, 50, actor="ops")
        docs = [_doc_with_ocr(_rx())[0] for _ in range(8)]
        roles = [router.route(d).role for d in docs]
        assert len(set(roles)) == 2
        results = extract_documents(docs, store=MemoryStore())
        assert all(not isinstance(r, Exception) for r in results)
        with session_scope() as c:
            for d, role in zip(docs, roles, strict=True):
                v = c.execute(text("SELECT model_version FROM extraction WHERE document_id = :d"),
                              {"d": d}).scalar_one()  # fmt: skip
                assert v == (b if role == "canary" else a)


def test_promotion_is_gated_by_the_live_comparison(rollout, monkeypatch):
    a, b = _register("gate-a", "stub-a"), _register("gate-b", "stub-b")
    monkeypatch.setitem(compare.LIMITS, "min_documents", 1)
    with gateway("stub-a", a) as (url_a, _), gateway("stub-b", b, "garbage") as (url_b, _):
        ops.deploy(a, url_a, actor="ops")
        ops.canary(b, url_b, 50, actor="ops")
        _extract(10)
        with pytest.raises(RolloutError, match="partial_rate"):
            ops.promote(actor="ops")
        with pytest.raises(RolloutError, match="--reason"):
            ops.promote(actor="ops", force=True)
        st, rep = ops.promote(actor="ops", force=True, reason="owner accepted: drill")
        assert st.primary_version == b and not rep["promote_ok"]
        with session_scope() as c:
            detail = c.execute(
                text("SELECT detail FROM audit_log WHERE entity = 'model_rollout' "
                     "AND entity_id = :i"), {"i": str(st.id)},
            ).scalar_one()  # fmt: skip
        assert detail["forced"] is True and detail["reason"] == "owner accepted: drill"


def test_nothing_changes_unless_the_gateway_serves_that_exact_version(rollout):
    a, b = _register("chk-a", "stub-a"), _register("chk-b", "stub-b")
    with gateway("stub-a", a) as (url_a, _), gateway("stub-b", b) as (url_b, _):
        with pytest.raises(RolloutError, match="does not serve"):
            ops.deploy(b, url_a, actor="ops")  # url_a serves a, not b
        with pytest.raises(RolloutError, match="no primary"):
            ops.canary(b, url_b, 10, actor="ops")
        ops.deploy(a, url_a, actor="ops")
        with pytest.raises(RolloutError, match="already the primary"):
            ops.deploy(b, url_b, actor="ops")
        for pct in (0, 51, 100):
            with pytest.raises(RolloutError):
                ops.canary(b, url_b, pct, actor="ops")
        with pytest.raises(RolloutError, match="own gateway"):
            ops.canary(b, url_a, 10, actor="ops")
        with pytest.raises(RolloutError, match="first primary"):
            ops.rollback(actor="ops")
    with pytest.raises(RolloutError, match="already registered with another model"):
        ops.register(actor="t", version=a, model_id="stub-z", revision="stub", backend="stub")
    with session_scope() as c:
        assert [s.action for s in registry.history(c)] == ["deploy"]


def test_the_rollout_log_cannot_be_rewritten(rollout):
    a = _register("log-a", "stub-a")
    with gateway("stub-a", a) as (url_a, _):
        st = ops.deploy(a, url_a, actor="ops")
    for stmt in ("UPDATE model_rollout SET primary_url = 'x' WHERE id = :i",
                 "DELETE FROM model_rollout WHERE id = :i"):  # fmt: skip
        with pytest.raises(Exception, match="append-only"), session_scope() as c:
            c.execute(text(stmt), {"i": st.id})


def test_serve_starts_a_real_gateway_process_for_a_version(rollout, tmp_path, monkeypatch):
    """The launcher itself: a separate ``python -m cdi_adapter.mlserve`` for a stub version."""
    monkeypatch.setenv("WS", str(tmp_path))
    v = _register("proc", "stub-keyword-responder")
    url = f"http://127.0.0.1:{_free_port()}"
    procs = []

    def launch(version, u):
        procs.extend(ops.launch_gateway(version, u))  # one process per replica (E10-S9)

    try:
        ops.serve(v, url, launch=launch, wait_s=90)
        h = ops.gateway_health(url)
        assert h is not None and h["version"] == v and h["backend"] == "stub"
        assert len(procs) == 1
        ops.serve(v, url, launch=launch)  # already serving: nothing started
        assert len(procs) == 1
    finally:
        for p in procs:
            p.terminate()
            p.wait(30)
