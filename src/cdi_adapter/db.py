"""Engine + transactional connections (SQLAlchemy 2 Core; ARCHITECTURE §4, TECH_STACK §3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL, make_url

from cdi_adapter.config import Settings, get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide pooled engine for ``CDI_DATABASE_URL``."""
    global _engine
    if _engine is None:
        _engine = create_engine(engine_url(get_settings()), pool_pre_ping=True, future=True)
    return _engine


def engine_url(s: Settings) -> URL:
    """``CDI_DATABASE_URL``, or with ``CDI_DATABASE_HOSTS`` every listed server and libpq's
    ``target_session_attrs=read-write``: new connections go to the writable one (backlog E10-S6).
    ``pool_pre_ping`` drops connections to a server that went away, so a failover only costs the
    statements that were in flight."""
    url = make_url(s.database_url)
    hosts = [h.strip() for h in s.database_hosts.split(",") if h.strip()]
    if not hosts:
        return url
    query = {k: v for k, v in url.query.items() if k not in ("host", "target_session_attrs")}
    query |= {"host": tuple(hosts), "target_session_attrs": "read-write"}
    query.setdefault("connect_timeout", "3")
    # URL.set() ignores None, so the single host is dropped by building the URL again
    return URL.create(url.drivername, username=url.username, password=url.password,
                      database=url.database, query=query)  # fmt: skip


def reset_engine() -> None:
    """Dispose the pooled engine (tests, or after a credential change)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope() -> Iterator[Connection]:
    """One transaction: commit on success, roll back on any exception."""
    with get_engine().begin() as conn:
        yield conn
