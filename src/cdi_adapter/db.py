"""Engine + transactional connections (SQLAlchemy 2 Core; ARCHITECTURE §4, TECH_STACK §3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine

from cdi_adapter.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide pooled engine for ``CDI_DATABASE_URL``."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
    return _engine


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
