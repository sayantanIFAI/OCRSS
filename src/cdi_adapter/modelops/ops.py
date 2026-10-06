"""Operator commands behind ``python -m cdi_adapter.modelops`` (backlog E10-S5).

Every command checks before it changes anything: a gateway is only put in the rollout after its
``/healthz`` reports the exact registered version and model, and every change is one appended
rollout row plus an ``audit_log`` row. ``rollback`` is the one-command undo:

- with a canary deployed: drop it; every document goes back to the primary at once
- without one: return to the previous primary version, starting its gateway first when it no
  longer runs (``serve``), and switch only once it answers with that version

A side of the rollout may be served by several replicas (backlog E10-S9): ``--url`` lists them
comma separated, e.g. ``http://127.0.0.1:8077,http://127.0.0.1:8078``. Every check then applies to
every replica (all must serve the version), and ``serve`` starts each replica that is not up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.ml.client import split_urls
from cdi_adapter.modelops import compare as cmp
from cdi_adapter.modelops import registry, router
from cdi_adapter.modelops.registry import RolloutError, RolloutState

ROOT = Path(__file__).resolve().parents[3]
MAX_CANARY_PERCENT = 50  # a canary is a small share first; 100 % is a promotion

Health = Callable[[str], dict[str, Any] | None]
Launch = Callable[[dict[str, Any], str], Any]


def _one_health(url: str) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{url.rstrip('/')}/healthz", timeout=5.0)
        r.raise_for_status()
        return dict(r.json())
    except (httpx.HTTPError, ValueError):
        return None


def gateway_health(url: str) -> dict[str, Any] | None:
    """``/healthz`` of the gateway, or of every replica of a comma-separated list: the first
    replica's answer when they all agree on status, version, model and backend; ``None`` when
    any replica does not answer; ``status: mixed`` when they disagree."""
    hs = [_one_health(u) for u in split_urls(url)]
    if any(h is None for h in hs):
        return None
    key = ("status", "version", "model", "backend")
    first = hs[0]
    assert first is not None
    if any(tuple(h.get(k) for k in key) != tuple(first.get(k) for k in key)
           for h in hs if h is not None):  # fmt: skip
        return {"status": "mixed", "replicas": hs}
    return {**first, "replicas": len(hs)}


def serves(health: dict[str, Any] | None, version: dict[str, Any]) -> bool:
    """The gateway answers as exactly this registered version (and has not fallen back)."""
    return (health is not None and health.get("status") == "ok"
            and health.get("version") == version["version"]
            and health.get("model") == version["model_id"]
            and health.get("backend") == version["backend"])  # fmt: skip


def _require_serving(url: str, version: dict[str, Any], health: Health) -> None:
    h = health(url)
    if not serves(h, version):
        seen = "no answer"
        if h is not None:
            seen = f"version={h.get('version')} model={h.get('model')} backend={h.get('backend')}"
        raise RolloutError(f"{url} does not serve {version['version']} ({version['model_id']}, "
                           f"{version['backend']}): {seen}. Start it with "
                           f"`python -m cdi_adapter.modelops serve {version['version']} "
                           f"--url {url}`")  # fmt: skip


def _loopback_port(url: str) -> int:
    u = urlsplit(url)
    if u.hostname not in ("127.0.0.1", "localhost") or u.port is None:
        raise RolloutError(f"{url}: a gateway is started on 127.0.0.1:<port> only (§12)")
    return u.port


def gateway_env(version: dict[str, Any], port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"CDI_MLSERVE_PORT": str(port), "CDI_MLSERVE_BACKEND": version["backend"],
                "CDI_VLM_MODEL_ID": version["model_id"],
                "CDI_VLM_MODEL_REVISION": version["revision"],
                "CDI_MODEL_VERSION": version["version"]})  # fmt: skip
    return env


def launch_gateway(version: dict[str, Any], url: str) -> list[subprocess.Popen[bytes]]:
    """Start a gateway process for ``version`` on the loopback port of each replica in ``url``
    that does not answer yet, detached."""
    return [_launch_one(version, u) for u in split_urls(url) if _one_health(u) is None]


def _launch_one(version: dict[str, Any], url: str) -> subprocess.Popen[bytes]:
    port = _loopback_port(url)
    py = sys.executable
    vllm_py = ROOT / ".venv-vllm" / "bin" / "python"
    if version["backend"] == "vllm" and vllm_py.exists():  # E2-S1: vLLM has its own venv
        py = str(vllm_py)
    logs = Path(os.environ.get("WS", str(ROOT))) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / f"mlserve-{port}.log", "ab")  # noqa: SIM115 - handed to the child
    return subprocess.Popen([py, "-m", "cdi_adapter.mlserve"], cwd=ROOT,
                            env=gateway_env(version, port), stdin=subprocess.DEVNULL,
                            stdout=out, stderr=out, start_new_session=True)  # fmt: skip


def wait_serving(url: str, version: dict[str, Any], health: Health, timeout: float = 180.0,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
    deadline = clock() + timeout
    while clock() < deadline:
        if serves(health(url), version):
            return
        time.sleep(1.0)
    _require_serving(url, version, health)


def _version(conn: Any, name: str) -> dict[str, Any]:
    v = registry.get_version(conn, name)
    if v is None:
        raise RolloutError(f"version {name} is not registered")
    return v


def _record(conn: Any, st: RolloutState, detail: dict[str, Any] | None = None) -> RolloutState:
    repo.write_audit(conn, actor=st.actor, action="update", entity="model_rollout",
                     entity_id=str(st.id),
                     detail={"action": st.action, "primary": st.primary_version,
                             "canary": st.canary_version, "canary_percent": st.canary_percent,
                             "reason": st.reason, **(detail or {})})  # fmt: skip
    router.reset()
    return st


# ------------------------------------------------------------------ commands


def register(*, actor: str, **kw: Any) -> dict[str, Any]:
    with session_scope() as conn:
        v = registry.register(conn, registered_by=actor, **kw)
        repo.write_audit(conn, actor=actor, action="create", entity="model_version",
                         entity_id=v["version"],
                         detail={"model_id": v["model_id"], "revision": v["revision"],
                                 "backend": v["backend"]})  # fmt: skip
    return v


def deploy(version: str, url: str, *, actor: str, reason: str | None = None, force: bool = False,
           health: Health = gateway_health) -> RolloutState:  # fmt: skip
    """The first primary. Replacing a live primary directly skips the canary, so it needs
    ``force`` and a reason."""
    with session_scope() as conn:
        registry.lock(conn)
        st = registry.current(conn)
        if st is not None and not force:
            raise RolloutError(f"{st.primary_version} is already the primary; change it with "
                               "`canary` then `promote` (or --force with a --reason)")  # fmt: skip
        if st is not None and not reason:
            raise RolloutError("--force needs a --reason")
        v = _version(conn, version)
        _require_serving(url, v, health)
        new = registry.append(conn, actor=actor, action="deploy", primary_version=version,
                              primary_url=url, reason=reason)  # fmt: skip
        return _record(conn, new)


def canary(version: str, url: str, percent: int, *, actor: str, reason: str | None = None,
           health: Health = gateway_health) -> RolloutState:  # fmt: skip
    if not 1 <= percent <= MAX_CANARY_PERCENT:
        raise RolloutError(f"a canary takes 1-{MAX_CANARY_PERCENT} % of documents; 100 % is "
                           "`promote`")  # fmt: skip
    with session_scope() as conn:
        registry.lock(conn)
        st = registry.current(conn)
        if st is None:
            raise RolloutError("no primary yet: `deploy` one first")
        if st.canary_version is not None and st.canary_version != version:
            raise RolloutError(f"{st.canary_version} is already the canary: `promote` or "
                               "`rollback` it first")  # fmt: skip
        if ",".join(split_urls(url)) == st.primary_url:
            raise RolloutError("the canary needs its own gateway, not the primary's")
        v = _version(conn, version)
        _require_serving(url, v, health)
        new = registry.append(conn, actor=actor, action="canary",
                              primary_version=st.primary_version, primary_url=st.primary_url,
                              canary_version=version, canary_url=url, canary_percent=percent,
                              reason=reason)  # fmt: skip
        return _record(conn, new)


def set_percent(percent: int, *, actor: str, reason: str | None = None) -> RolloutState:
    if not 0 <= percent <= MAX_CANARY_PERCENT:
        raise RolloutError(f"0-{MAX_CANARY_PERCENT} %; 100 % is `promote`")
    with session_scope() as conn:
        registry.lock(conn)
        st = registry.current(conn)
        if st is None or st.canary_version is None:
            raise RolloutError("no canary is deployed")
        new = registry.append(conn, actor=actor, action="set_percent",
                              primary_version=st.primary_version, primary_url=st.primary_url,
                              canary_version=st.canary_version, canary_url=st.canary_url,
                              canary_percent=percent, reason=reason)  # fmt: skip
        return _record(conn, new)


def promote(*, actor: str, reason: str | None = None, force: bool = False,
            health: Health = gateway_health) -> tuple[RolloutState, dict[str, Any]]:  # fmt: skip
    """The canary becomes the primary, only if the live comparison lets it (or ``force`` with a
    reason, recorded in the log)."""
    with session_scope() as conn:
        registry.lock(conn)
        st = registry.current(conn)
        if st is None or st.canary_version is None or st.canary_url is None:
            raise RolloutError("no canary is deployed")
        report = cmp.compare(conn)
        if not report["promote_ok"]:
            if not force:
                raise RolloutError("the canary may not be promoted: "
                                   + "; ".join(report["blocking"]))  # fmt: skip
            if not reason:
                raise RolloutError("--force needs a --reason")
        v = _version(conn, st.canary_version)
        _require_serving(st.canary_url, v, health)
        new = registry.append(conn, actor=actor, action="promote",
                              primary_version=st.canary_version, primary_url=st.canary_url,
                              reason=reason)  # fmt: skip
        detail = {"compare": {k: report[k] for k in ("promote_ok", "blocking")},
                  "forced": bool(force and not report["promote_ok"])}  # fmt: skip
        return _record(conn, new, detail), report


def rollback(*, actor: str, reason: str | None = None, health: Health = gateway_health,
             launch: Launch = launch_gateway, wait_s: float = 180.0) -> RolloutState:  # fmt: skip
    with session_scope() as conn:
        registry.lock(conn)
        st = registry.current(conn)
        if st is None:
            raise RolloutError("nothing to roll back: no rollout recorded")
        if st.canary_version is not None:
            why = reason or f"canary {st.canary_version} rolled back"
            new = registry.append(conn, actor=actor, action="rollback",
                                  primary_version=st.primary_version, primary_url=st.primary_url,
                                  reason=why)  # fmt: skip
            return _record(conn, new, {"dropped_canary": st.canary_version})
        prev = registry.previous_primary(conn)
        if prev is None:
            raise RolloutError(f"{st.primary_version} is the first primary: nothing to return to")
        target = _version(conn, prev.primary_version)
    # outside the transaction: starting a gateway can take minutes (the model loads)
    h = health(prev.primary_url)
    if not serves(h, target):
        if h is not None:
            raise RolloutError(f"{prev.primary_url} is up but serves "
                               f"{h.get('version')} ({h.get('model')}), not {target['version']}; "
                               f"stop it, or serve {target['version']} on another port and "
                               "`deploy --force` there")  # fmt: skip
        launch(target, prev.primary_url)
        wait_serving(prev.primary_url, target, health, timeout=wait_s)
    with session_scope() as conn:
        registry.lock(conn)
        now = registry.current(conn)
        if now is None or now.id != st.id:
            raise RolloutError("the rollout changed while the gateway was starting; "
                               "check `status` and run rollback again")  # fmt: skip
        new = registry.append(conn, actor=actor, action="rollback",
                              primary_version=target["version"], primary_url=prev.primary_url,
                              reason=reason or f"{st.primary_version} rolled back")  # fmt: skip
        return _record(conn, new, {"from": st.primary_version})


def serve(version: str, url: str, *, launch: Launch = launch_gateway,
          health: Health = gateway_health, wait_s: float = 180.0) -> dict[str, Any]:  # fmt: skip
    with session_scope() as conn:
        v = _version(conn, version)
    h = health(url)
    if serves(h, v):
        return v
    if h is not None:
        raise RolloutError(f"{url} already runs {h.get('version')} ({h.get('model')})")
    launch(v, url)
    wait_serving(url, v, health, timeout=wait_s)
    return v


def status(health: Health = gateway_health) -> dict[str, Any]:
    with session_scope() as conn:
        st = registry.current(conn)
        hist = registry.history(conn, 10)
    if st is None:
        return {"state": None, "history": []}
    sides = [("primary", st.primary_version, st.primary_url)]
    if st.canary_version and st.canary_url:
        sides.append(("canary", st.canary_version, st.canary_url))
    return {
        "state": st,
        "gateways": {role: {"version": v, "url": u, "health": health(u)} for role, v, u in sides},
        "history": hist,
    }
