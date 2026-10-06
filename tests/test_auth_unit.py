"""E11-S1 (unit): every route has a policy, nobody gets in without a session, passwords, CSRF."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from cdi_adapter.webapp import auth
from cdi_adapter.webapp.app import app
from tests.helpers import browser


def _api_routes(routes) -> list[APIRoute]:
    """Every APIRoute, including those inside included routers (FastAPI 0.14x keeps those
    wrapped as ``_IncludedRouter``, whose ``original_router`` holds them)."""
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif getattr(r, "original_router", None) is not None:
            out += _api_routes(r.original_router.routes)
    return out


ROUTES = [(m, r.path) for r in _api_routes(app.routes) for m in sorted(r.methods)]


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path).replace(
        "/pages/00000000-0000-0000-0000-000000000001", "/pages/1"
    )


def test_every_route_has_an_explicit_policy_and_no_policy_is_stale() -> None:
    assert ROUTES and len(ROUTES) >= 30
    missing = [k for k in ROUTES if k not in auth.POLICY]
    assert missing == [], f"routes without an access policy: {missing}"
    stale = [k for k in auth.POLICY if k not in ROUTES]
    assert stale == [], f"policies for routes that no longer exist: {stale}"


def test_only_health_and_sign_in_are_public() -> None:
    public = sorted(k for k, v in auth.POLICY.items() if v is auth.PUBLIC)
    # /readyz: the E10-S9 balancer's health check (booleans + instance name, like /healthz)
    assert public == [("GET", "/healthz"), ("GET", "/login"), ("GET", "/readyz"),
                      ("POST", "/login")]  # fmt: skip
    for roles in auth.POLICY.values():
        assert roles is auth.PUBLIC or roles <= set(auth.ROLES)


def test_roles_follow_the_counter_and_clinical_split() -> None:
    P = auth.POLICY
    assert P[("GET", "/admin")] == {"admin"}
    assert P[("POST", "/api/jobs")] == {"intake", "admin"}  # the counter uploads
    assert "intake" not in P[("POST", "/api/jobs/{job_id}/generate")]  # keep/drop is clinical
    assert "intake" not in P[("POST", "/api/facts/{fact_id}/review")]
    for (_, path), roles in P.items():
        if path.startswith(("/review", "/api/review", "/api/reviewer", "/api/documents")):
            assert roles == {"reviewer", "admin"}, path
        if roles is not auth.PUBLIC:
            assert "admin" in roles, path  # admin can always act


@pytest.mark.parametrize(
    ("method", "path"), [k for k in ROUTES if auth.POLICY.get(k) is not auth.PUBLIC]
)
def test_no_route_works_without_a_session(method: str, path: str) -> None:  # noqa: E501
    """The E11-S1 negative test: no cookie -> 401 (API) / sign-in redirect (page), never data."""
    c = browser(app)
    r = c.request(method, _fill(path), follow_redirects=False)
    if path.startswith("/api/") or method != "GET":
        assert r.status_code == 401, (method, path, r.status_code)
    else:
        assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}"


def test_a_forged_session_cookie_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Conn:
        def execute(self, *a, **k):  # the token's hash is in no session row
            class R:
                def mappings(self):
                    return self

                def first(self):
                    return None

            return R()

    from contextlib import contextmanager

    @contextmanager
    def fake_scope():
        yield Conn()

    monkeypatch.setattr(auth, "session_scope", fake_scope)
    c = browser(app)
    c.cookies.set(auth.COOKIE, "made-up-token")
    assert c.get("/api/review/tasks").status_code == 401


def test_state_changing_requests_need_a_same_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    no_origin = TestClient(app, base_url="https://testserver")
    assert no_origin.post("/login", data={"username": "x", "password": "y"}).status_code == 403
    evil = TestClient(
        app, base_url="https://testserver", headers={"Origin": "https://evil.example"}
    )
    assert evil.post("/api/jobs").status_code == 403
    assert evil.post("/logout").status_code == 403
    # a same-origin Referer is accepted when a browser sends no Origin
    ref = TestClient(app, base_url="https://testserver",
                     headers={"Referer": "https://testserver/login"})  # fmt: skip
    assert ref.post("/api/jobs").status_code == 401  # past CSRF, stopped by sign-in


def test_a_host_rewriting_proxy_is_accepted_only_when_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Colab / RunPod proxies rewrite Host, so Origin != Host for genuine same-origin posts."""
    from fastapi.testclient import TestClient

    from cdi_adapter.config import get_settings

    proxied = TestClient(app, base_url="https://testserver",
                         headers={"Origin": "https://8888-x.prod.colab.dev"})  # fmt: skip
    assert proxied.post("/api/jobs").status_code == 403  # not named: still refused

    monkeypatch.setenv("CDI_PUBLIC_ORIGINS", "8888-x.prod.colab.dev, emr.example.in")
    get_settings.cache_clear()
    try:
        assert proxied.post("/api/jobs").status_code == 401  # past CSRF, stopped by sign-in
        # naming one proxy does not admit any other origin
        evil = TestClient(app, base_url="https://testserver",
                          headers={"Origin": "https://evil.example"})  # fmt: skip
        assert evil.post("/api/jobs").status_code == 403
    finally:
        get_settings.cache_clear()


def test_api_docs_are_not_published() -> None:
    c = browser(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


def test_the_sign_in_page_is_public_and_keeps_the_health_marker() -> None:
    r = browser(app).get("/login")
    assert r.status_code == 200 and "name=password type=password" in r.text
    assert "Agentic EMR generation" in r.text  # E1-S9 ingress + start_all.sh health checks
    assert "cdn" not in r.text.lower()


def test_next_is_never_an_open_redirect() -> None:
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:x", ""):
        assert auth._safe_next(bad) is None, bad
    assert auth._safe_next("/reviewer?x=1") == "/reviewer?x=1"


def test_passwords_are_salted_scrypt_and_checked_in_constant_time() -> None:
    h1, h2 = auth.hash_password("a long passphrase"), auth.hash_password("a long passphrase")
    assert h1 != h2 and h1.startswith("scrypt$32768$8$1$")  # a new salt every time
    assert auth.check_password("a long passphrase", h1)
    assert not auth.check_password("a long passphrasE", h1)
    assert not auth.check_password("x", "garbage") and not auth.check_password("x", "md5$1$2$3$4$5")
    with pytest.raises(ValueError, match="at least 12"):
        auth.hash_password("short")
    src = open(auth.__file__, encoding="utf-8").read()
    assert "hmac.compare_digest" in src


def test_every_page_offers_sign_out() -> None:
    from cdi_adapter.webapp.theme import header

    assert '<form method=post action="/logout"' in header()
