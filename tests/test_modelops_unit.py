"""E10-S5 unit: canary routing, the canary-to-primary fallback, the promotion gate, the gateway's
version on /healthz and the operator CLI. The drill against real PostgreSQL and real gateways over
HTTP is ``tests/test_modelops_integration.py``."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from cdi_adapter.ml.client import RoutedClient
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import StubBackend
from cdi_adapter.modelops import __main__ as cli
from cdi_adapter.modelops import compare, ops, router
from cdi_adapter.modelops.registry import RolloutError, RolloutState
from tests.test_metrics_unit import value


@pytest.fixture(autouse=True)
def _fresh_router(monkeypatch):
    monkeypatch.setenv("CDI_MODEL_ROUTE_TTL_S", "60")
    from cdi_adapter.config import get_settings

    get_settings.cache_clear()
    router.reset()
    yield
    router.reset()
    get_settings.cache_clear()


def _live(percent: int, canary: bool = True) -> router._Live:
    st = RolloutState(id=7, action="canary", primary_version="v1", primary_url="http://p:1",
                      canary_version="v2" if canary else None,
                      canary_url="http://c:2" if canary else None,
                      canary_percent=percent if canary else 0)  # fmt: skip
    return router._Live(st, {"v1": "model-1", "v2": "model-2"})


# ------------------------------------------------------------------ routing


def test_a_document_always_lands_in_the_same_bucket():
    doc = uuid.uuid4()
    assert router.bucket(doc) == router.bucket(str(doc)) and 0 <= router.bucket(doc) < 100


def test_buckets_spread_evenly_so_the_percent_is_the_share():
    ids = [uuid.UUID(int=i * 7919 + 13) for i in range(20000)]
    share = sum(router.bucket(d) < 10 for d in ids) / len(ids)
    assert 0.09 < share < 0.11


def test_no_rollout_routes_everything_to_the_configured_gateway(monkeypatch):
    monkeypatch.setattr(router, "_read", lambda: router._Live(None, {}))
    r = router.route(uuid.uuid4())
    assert (r.role, r.url, r.version, r.fallback) == ("primary", "http://127.0.0.1:8077", None,
                                                      None)  # fmt: skip


def test_the_canary_gets_its_percent_and_falls_back_to_the_primary(monkeypatch):
    monkeypatch.setattr(router, "_read", lambda: _live(25))
    docs = [uuid.uuid4() for _ in range(400)]
    for d in docs:
        r = router.route(d)
        if router.bucket(d) < 25:
            assert (r.role, r.url, r.version, r.model_id) == ("canary", "http://c:2", "v2",
                                                              "model-2")  # fmt: skip
            assert r.fallback == router.Route("primary", "http://p:1", "v1", "model-1")
        else:
            assert (r.role, r.version, r.fallback) == ("primary", "v1", None)
    assert router.route(None).role == "primary"  # no document: never the canary


def test_zero_percent_or_no_canary_sends_nothing_to_it(monkeypatch):
    for lv in (_live(0), _live(30, canary=False)):
        monkeypatch.setattr(router, "_read", lambda lv=lv: lv)
        router.reset()
        assert {router.route(uuid.uuid4()).role for _ in range(200)} == {"primary"}


def test_state_is_cached_and_a_database_outage_keeps_the_last_known(monkeypatch):
    calls = []

    def read():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("database down")
        return _live(40)

    monkeypatch.setattr(router, "_read", read)
    t = [0.0]
    assert router.live(clock=lambda: t[0]).state.canary_percent == 40
    router.live(clock=lambda: t[0])
    assert len(calls) == 1  # within the TTL
    t[0] = 120.0
    assert router.live(clock=lambda: t[0]).state.canary_percent == 40  # stale, not lost
    assert len(calls) == 2
    assert value("cdi_model_canary_percent") == 40


# ------------------------------------------------------------------ the client's fallback


def _transport(canary_status: int, seen: list[str]) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "c":
            return httpx.Response(canary_status, json={"detail": "x"})
        return httpx.Response(200, json={"text": "{}", "model": "model-1", "backend": "hf",
                                         "usage": {}})  # fmt: skip

    return httpx.MockTransport(handle)


def _canary_route() -> router.Route:
    primary = router.Route("primary", "http://p:1", "v1", "model-1")
    return router.Route("canary", "http://c:2", "v2", "model-2", fallback=primary)


def test_a_failing_canary_hands_the_call_to_the_primary():
    seen: list[str] = []
    before = value("cdi_model_canary_fallbacks_total")
    c = RoutedClient(_canary_route(), transport=_transport(503, seen))
    assert (c.role, c.version, c.model) == ("canary", "v2", "model-2")
    assert c.generate("p") == "{}"
    assert seen == ["c", "p"]
    assert (c.role, c.version, c.model) == ("primary", "v1", "model-1")  # who answered
    assert value("cdi_model_canary_fallbacks_total") == before + 1
    c.generate("p")
    assert seen[-1] == "p"  # the rest of the document stays on the primary


def test_a_refused_request_is_an_answer_not_a_fallback():
    seen: list[str] = []
    c = RoutedClient(_canary_route(), transport=_transport(422, seen))
    with pytest.raises(httpx.HTTPStatusError):
        c.generate("p")
    assert seen == ["c"] and c.role == "canary"


def test_the_primary_has_nowhere_to_fall_back_to():
    seen: list[str] = []
    c = RoutedClient(router.Route("primary", "http://c:2", "v1"), transport=_transport(503, seen))
    with pytest.raises(httpx.HTTPStatusError):
        c.generate("p")


# ------------------------------------------------------------------ gateway identity


def test_the_gateway_reports_the_version_it_was_started_for():
    h = TestClient(create_app(StubBackend(), version="stub-2026.09")).get("/healthz").json()
    v = {"version": "stub-2026.09", "model_id": "stub-keyword-responder", "backend": "stub"}
    assert h["version"] == "stub-2026.09" and ops.serves(h, v)
    assert not ops.serves({**h, "version": "other"}, v)
    assert not ops.serves({**h, "model": "Qwen/Qwen2.5-VL-3B-Instruct"}, v)  # an OOM fallback
    assert not ops.serves(None, v)


def test_a_gateway_is_started_with_its_versions_pins_on_loopback_only():
    v = {"version": "q7b-a1", "model_id": "Qwen/Qwen2.5-VL-7B-Instruct", "revision": "cc59489",
         "backend": "hf"}  # fmt: skip
    env = ops.gateway_env(v, 8078)
    assert (env["CDI_MLSERVE_PORT"], env["CDI_VLM_MODEL_ID"], env["CDI_VLM_MODEL_REVISION"],
            env["CDI_MODEL_VERSION"], env["CDI_MLSERVE_BACKEND"]) == (
        "8078", "Qwen/Qwen2.5-VL-7B-Instruct", "cc59489", "q7b-a1", "hf")  # fmt: skip
    assert ops._loopback_port("http://127.0.0.1:8078") == 8078
    for bad in ("http://0.0.0.0:8078", "http://10.0.0.5:8078", "http://127.0.0.1"):
        with pytest.raises(RolloutError):
            ops._loopback_port(bad)


# ------------------------------------------------------------------ the promotion gate


def _stats(**kw):
    base = {"documents": 40, "failure_rate": 0.0, "partial_rate": 0.05, "reviewed": 30,
            "rejection_rate": 0.05, "correction_rate": 0.1, "eval_report": None}  # fmt: skip
    return {**base, **kw}


def test_an_equal_canary_may_be_promoted():
    assert compare.verdict(_stats(), _stats()) == (True, [])


@pytest.mark.parametrize(
    "canary, reason",
    [
        ({"documents": 5}, "only 5 canary documents"),
        ({"partial_rate": 0.2}, "partial_rate"),
        ({"failure_rate": 0.1}, "failure_rate"),
        ({"rejection_rate": 0.2}, "rejection_rate"),
        ({"correction_rate": 0.3}, "correction_rate"),
        ({"eval_report": {"field_f1": 0.80}}, "E2-S7 field_f1"),
        ({"eval_report": {"hallucination_rate": 0.05}}, "E2-S7 hallucination_rate"),
    ],
)
def test_a_worse_canary_is_blocked(canary, reason):
    primary = _stats(eval_report={"field_f1": 0.9, "hallucination_rate": 0.01})
    ok, why = compare.verdict(_stats(**canary), primary)
    assert not ok and any(reason in w for w in why), why


def test_review_rates_count_only_with_enough_decided_facts():
    ok, _ = compare.verdict(_stats(reviewed=3, rejection_rate=0.9), _stats())
    assert ok


# ------------------------------------------------------------------ the CLI


def test_rollback_is_one_command(monkeypatch, capsys):
    st = RolloutState(id=9, action="rollback", primary_version="v1", primary_url="http://p:1",
                      actor="ops")  # fmt: skip
    called = {}

    def fake(**kw):
        called.update(kw)
        return st

    monkeypatch.setattr(ops, "rollback", fake)
    assert cli.main(["--actor", "ops", "rollback", "--reason", "drill"]) == 0
    assert called == {"actor": "ops", "reason": "drill"}
    assert "rolled back: primary v1 @ http://p:1" in capsys.readouterr().out


def test_a_refused_command_exits_2_and_says_why(monkeypatch, capsys):
    def refuse(**kw):
        raise RolloutError("no canary is deployed")

    monkeypatch.setattr(ops, "promote", refuse)
    assert cli.main(["promote"]) == 2
    assert "refused: no canary is deployed" in capsys.readouterr().err


def test_every_command_parses():
    p = cli.build_parser()
    for argv in (["register", "v", "--model-id", "m", "--revision", "r", "--backend", "hf"],
                 ["versions"], ["serve", "v", "--url", "http://127.0.0.1:8078"],
                 ["deploy", "v", "--url", "u"], ["canary", "v", "--url", "u", "--percent", "5"],
                 ["set-percent", "0"], ["compare", "--json"],
                 ["promote", "--force", "--reason", "x"], ["rollback"], ["status"]):  # fmt: skip
        assert p.parse_args(argv).cmd == argv[0]


def test_the_pinned_revision_applies_to_the_registered_model_only():
    from cdi_adapter.config import Settings
    from cdi_adapter.mlserve.backends import _revision

    s = Settings(vlm_model_id="Qwen/A", vlm_model_revision="abc123")
    assert _revision("Qwen/A", s) == "abc123"
    assert _revision("Qwen/Qwen2.5-VL-3B-Instruct", s) is None  # the OOM fallback: unpinned
    assert _revision("Qwen/A", Settings(vlm_model_id="Qwen/A", vlm_model_revision="")) is None
