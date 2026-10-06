"""Which gateway, and so which model version, extracts a document (backlog E10-S5).

The live rollout state (the latest ``model_rollout`` row) is re-read at most every
``CDI_MODEL_ROUTE_TTL_S`` seconds, so a canary change or a rollback reaches every worker within
that time without a restart. A document's share bucket comes from a hash of its id: the same
document always goes to the same side (retries, re-runs), and ``canary_percent`` % of documents go
to the canary. With no rollout recorded yet (the registry not in use) everything goes to
``CDI_MLSERVE_URL``, exactly as before E10-S5.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from cdi_adapter import metrics
from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger
from cdi_adapter.modelops import registry

log = get_logger(__name__)

ROLES = ("primary", "canary")


@dataclass(frozen=True)
class Route:
    role: str
    url: str
    version: str | None = None
    model_id: str | None = None
    fallback: Route | None = None  # a canary's primary: where a failed canary call goes


@dataclass(frozen=True)
class _Live:
    state: registry.RolloutState | None
    models: dict[str, str]


_lock = threading.Lock()
_cache: tuple[float, _Live] | None = None


def bucket(document_id: UUID | str) -> int:
    """0-99, stable for a document id."""
    return int(hashlib.sha256(str(document_id).encode()).hexdigest()[:8], 16) % 100


def _read() -> _Live:
    from cdi_adapter.db import session_scope

    with session_scope() as conn:
        state = registry.current(conn)
        models: dict[str, str] = {}
        if state is not None:
            rows = conn.execute(text("SELECT version, model_id FROM model_version "
                                     "WHERE version = ANY(:v)"),
                                {"v": [v for v in (state.primary_version, state.canary_version)
                                       if v]}).all()  # fmt: skip
            models = {v: m for v, m in rows}
    return _Live(state, models)


def live(clock: Callable[[], float] = time.monotonic) -> _Live:
    global _cache
    ttl = get_settings().model_route_ttl_s
    with _lock:
        if _cache is not None and clock() - _cache[0] < ttl:
            return _cache[1]
        try:
            fresh = _read()
        except Exception as exc:  # the database is down: keep routing on the last known state
            if _cache is not None:
                log.warning("model_route_stale", error=str(exc))
                return _cache[1]
            log.warning("model_route_unavailable", error=str(exc))
            return _Live(None, {})
        _cache = (clock(), fresh)
    st = fresh.state
    metrics.CANARY_PERCENT.set(st.canary_percent if st is not None and st.has_canary else 0)
    return fresh


def reset() -> None:
    """Forget the cached state (tests; an operator command in the same process)."""
    global _cache
    with _lock:
        _cache = None


def route(document_id: UUID | str | None = None) -> Route:
    lv = live()
    st = lv.state
    if st is None:
        s = get_settings()
        return Route("primary", s.mlserve_url.rstrip("/"), s.model_version or None)
    primary = Route("primary", st.primary_url, st.primary_version,
                    lv.models.get(st.primary_version))  # fmt: skip
    if (document_id is not None and st.has_canary and st.canary_url and st.canary_version
            and bucket(document_id) < st.canary_percent):  # fmt: skip
        return Route("canary", st.canary_url, st.canary_version,
                     lv.models.get(st.canary_version), fallback=primary)  # fmt: skip
    return primary
