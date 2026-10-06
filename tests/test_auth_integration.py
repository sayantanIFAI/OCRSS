"""E11-S1 on real PostgreSQL: sign-in, role checks, lockout, expiry, sign-out, audit, user CLI."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.helpers import TEST_PASSWORD, add_web_user, browser, signed_in

pytestmark = pytest.mark.integration


@pytest.fixture
def app(db_env, monkeypatch):
    # These tests assert the pod's hardened session cookie (always behind TLS), so the setting is
    # pinned here: a local dev .env sets CDI_SESSION_COOKIE_SECURE=false for plain-http browsing.
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_SESSION_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    from cdi_adapter.webapp.app import app

    yield app
    get_settings.cache_clear()


def _db(sql: str, **params):
    from cdi_adapter.db import session_scope

    with session_scope() as c:
        return c.execute(text(sql), params).mappings().all() if sql.lstrip().upper().startswith(
            "SELECT") else c.execute(text(sql), params)  # fmt: skip


def _login(c, username, password=TEST_PASSWORD, **extra):
    return c.post("/login", data={"username": username, "password": password, **extra},
                  follow_redirects=False)  # fmt: skip


# ---------------------------------------------------------------- sign-in


def test_sign_in_sets_a_hardened_cookie_and_stores_only_its_hash(app) -> None:
    user = add_web_user("intake")
    c = browser(app)
    r = _login(c, user)
    assert r.status_code == 303 and r.headers["location"] == "/"  # intake lands on the counter
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in cookie, flag
    token = c.cookies.get("cdi_session")
    assert token and len(token) >= 40
    rows = _db("SELECT token_sha256 FROM web_session s JOIN web_user u ON u.id = s.user_id "
               "WHERE u.username = :u", u=user)  # fmt: skip
    import hashlib

    assert [r["token_sha256"] for r in rows] == [hashlib.sha256(token.encode()).hexdigest()]
    assert c.get("/api/me").json() == {"username": user, "display_name": "Test intake",
                                       "role": "intake"}  # fmt: skip


def test_next_returns_to_the_page_asked_for(app) -> None:
    c = browser(app)
    r = _login(c, add_web_user("reviewer"), next="/review")
    assert r.headers["location"] == "/review"
    r = _login(browser(app), add_web_user("reviewer"), next="https://evil.example/")
    assert r.headers["location"] == "/reviewer"  # never an open redirect


def test_wrong_password_unknown_user_and_disabled_user_are_refused_alike(app) -> None:
    user = add_web_user("intake")
    c = browser(app)
    for name, pw in ((user, "wrong password!!"), ("nobody-here", TEST_PASSWORD)):
        r = _login(c, name, pw)
        assert r.status_code == 401 and "Wrong username or password." in r.text
        assert "cdi_session" not in r.headers.get("set-cookie", "")
    _db("UPDATE web_user SET active = false WHERE username = :u", u=user)
    assert _login(c, user).status_code == 401


def test_repeated_failures_lock_the_account(app) -> None:
    user = add_web_user("intake")
    c = browser(app)
    for _ in range(4):
        assert _login(c, user, "wrong password!!").status_code == 401
    r = _login(c, user, "wrong password!!")  # the 5th failure (CDI_LOGIN_MAX_FAILURES)
    assert r.status_code == 401 and "locked" in r.text
    assert _login(c, user).status_code == 401  # even the right password, while locked
    _db("UPDATE web_user SET locked_until = now() - interval '1 second' WHERE username = :u",
        u=user)  # fmt: skip
    assert _login(c, user).status_code == 303  # the lock expires


# ---------------------------------------------------------------- role checks


INTAKE_OK = [("GET", "/"), ("GET", "/api/me"), ("GET", "/api/registry/lookup?q=x")]
INTAKE_DENIED = [("GET", "/review"), ("GET", "/reviewer"), ("GET", "/admin"),
                 ("GET", "/api/review/tasks"), ("GET", "/api/reviewer/workbasket"),
                 ("POST", "/api/jobs/x/generate"), ("GET", "/api/documents/"
                  "00000000-0000-0000-0000-000000000001/evidence")]  # fmt: skip
REVIEWER_OK = [("GET", "/review"), ("GET", "/reviewer"), ("GET", "/api/review/tasks"),
               ("GET", "/api/reviewer/workbasket")]  # fmt: skip
REVIEWER_DENIED = [("GET", "/"), ("POST", "/api/jobs"), ("POST", "/api/registry"),
                   ("GET", "/admin")]  # fmt: skip


@pytest.mark.parametrize(("method", "path"), INTAKE_OK)
def test_intake_can_use_the_counter(app, method, path) -> None:
    r = signed_in(app, "intake").request(method, path, follow_redirects=False)
    assert r.status_code not in (401, 403, 303), (path, r.status_code)  # 404 = no such patient


@pytest.mark.parametrize(("method", "path"), INTAKE_DENIED)
def test_intake_cannot_touch_clinical_review_or_admin(app, method, path) -> None:
    r = signed_in(app, "intake").request(method, path, follow_redirects=False)
    assert r.status_code == 403, (path, r.status_code)


@pytest.mark.parametrize(("method", "path"), REVIEWER_OK)
def test_reviewer_can_review(app, method, path) -> None:
    assert signed_in(app, "reviewer").request(method, path).status_code == 200


@pytest.mark.parametrize(("method", "path"), REVIEWER_DENIED)
def test_reviewer_cannot_upload_or_inspect(app, method, path) -> None:
    r = signed_in(app, "reviewer").request(method, path, follow_redirects=False)
    assert r.status_code == 403, (path, r.status_code)


def test_admin_reaches_everything_role_gated(app) -> None:
    c = signed_in(app, "admin")
    for path in ("/", "/review", "/reviewer", "/admin", "/api/review/tasks",
                 "/api/reviewer/workbasket"):  # fmt: skip
        assert c.get(path).status_code == 200, path


# ---------------------------------------------------------------- session lifetime


def test_idle_and_absolute_timeouts_end_the_session(app) -> None:
    for column, value in (("last_seen_at", datetime.now(UTC) - timedelta(minutes=31)),
                          ("expires_at", datetime.now(UTC) - timedelta(seconds=1))):  # fmt: skip
        user = add_web_user("reviewer")
        c = browser(app)
        _login(c, user)
        assert c.get("/api/me").status_code == 200
        _db(f"UPDATE web_session SET {column} = :v WHERE user_id = "
            "(SELECT id FROM web_user WHERE username = :u)", v=value, u=user)  # fmt: skip
        assert c.get("/api/me").status_code == 401, column
        assert (
            _db(
                "SELECT 1 FROM web_session s JOIN web_user u ON u.id = s.user_id "
                "WHERE u.username = :u",
                u=user,
            )
            == []
        )  # the dead session is deleted


def test_disabling_a_user_ends_their_live_session(app) -> None:
    user = add_web_user("reviewer")
    c = browser(app)
    _login(c, user)
    _db("UPDATE web_user SET active = false WHERE username = :u", u=user)
    assert c.get("/api/me").status_code == 401


def test_sign_out_ends_the_session_everywhere(app) -> None:
    user = add_web_user("intake")
    c = browser(app)
    _login(c, user)
    token = c.cookies.get("cdi_session")
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    replay = browser(app)
    replay.cookies.set("cdi_session", token)  # a copied cookie is useless after sign-out
    assert replay.get("/api/me").status_code == 401


def test_sign_in_failure_and_sign_out_are_audited(app) -> None:
    user = add_web_user("intake")
    c = browser(app)
    _login(c, user, "wrong password!!")
    _login(c, user)
    c.post("/logout")
    rows = _db("SELECT action, detail->>'outcome' AS outcome FROM audit_log "
               "WHERE entity = 'web_session' AND actor = :u ORDER BY id", u=user)  # fmt: skip
    assert [(r["action"], r["outcome"]) for r in rows] == [
        ("create", "refused"), ("create", "signed_in"), ("delete", "signed_out")]  # fmt: skip


# ---------------------------------------------------------------- user CLI


def _cli(monkeypatch, capsys, argv, stdin=""):
    from cdi_adapter.webapp import users

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    try:
        rc = users.main(argv)
    except SystemExit as exc:
        return exc.code, capsys.readouterr().out
    return rc, capsys.readouterr().out


def test_users_cli_manages_accounts_and_ends_sessions_on_change(app, monkeypatch, capsys) -> None:
    rc, out = _cli(monkeypatch, capsys, ["add", "r.das", "--name", "R. Das", "--role", "intake"],
                   "too short\n")  # fmt: skip
    assert "at least 12" in str(rc)
    rc, out = _cli(monkeypatch, capsys, ["add", "r.das", "--name", "R. Das", "--role", "intake"],
                   TEST_PASSWORD + "\n")  # fmt: skip
    assert rc == 0 and "added r.das (intake)" in out
    rc, _ = _cli(monkeypatch, capsys, ["add", "r.das", "--name", "x", "--role", "admin"],
                 TEST_PASSWORD + "\n")  # fmt: skip
    assert "already exists" in str(rc)

    c = browser(app)
    assert _login(c, "r.das").status_code == 303
    assert _cli(monkeypatch, capsys, ["role", "r.das", "reviewer"])[0] == 0
    assert c.get("/api/me").status_code == 401  # a role change signs the user out
    assert _login(c, "r.das").headers["location"] == "/reviewer"

    assert _cli(monkeypatch, capsys, ["passwd", "r.das"], "a brand new passphrase\n")[0] == 0
    assert c.get("/api/me").status_code == 401
    assert _login(browser(app), "r.das").status_code == 401  # the old password is gone
    assert _login(browser(app), "r.das", "a brand new passphrase").status_code == 303

    rc, out = _cli(monkeypatch, capsys, ["list"])
    assert rc == 0 and "r.das" in out and "reviewer" in out
    stored = _db("SELECT password_hash FROM web_user WHERE username = 'r.das'")[0]
    assert stored["password_hash"].startswith("scrypt$") and "passphrase" not in str(stored)
    acts = _db("SELECT action, detail->>'change' AS change FROM audit_log "
               "WHERE entity = 'web_user' AND entity_id = 'r.das' ORDER BY id")  # fmt: skip
    assert [(a["action"], a["change"]) for a in acts] == [
        ("create", None), ("update", "role"), ("update", "passwd")]  # fmt: skip
