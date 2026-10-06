"""E1-S9: stable ingress (Caddy on an India VM + a reverse SSH tunnel opened by the pod)."""

from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cdi_adapter.podmanifest import parse_versions
from tests.test_ops_unit import _bash

ROOT = Path(__file__).resolve().parents[1]
INGRESS = ROOT / "infra" / "ingress"
RUNPOD = ROOT / "infra" / "runpod"
CADDY = (INGRESS / "Caddyfile").read_text(encoding="utf-8")
SETUP = (INGRESS / "setup_ingress_vm.sh").read_text(encoding="utf-8")
TUNNEL = (RUNPOD / "start_tunnel.sh").read_text(encoding="utf-8")
START = (RUNPOD / "start_all.sh").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "scripts"))
import check_ingress  # noqa: E402


def _need_bash() -> str:
    bash = _bash()
    if bash is None:
        pytest.skip("bash not available")
    return bash


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# ---------------------------------------------------------------- scripts + pins


@pytest.mark.parametrize("script", [INGRESS / "setup_ingress_vm.sh", RUNPOD / "start_tunnel.sh"])
def test_scripts_are_valid_native_bash(script: Path) -> None:
    r = subprocess.run([_need_bash(), "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert not re.search(r"\bdocker\b", _code(text), re.I)  # owner decision: no Docker


def test_caddy_is_pinned_and_checksum_verified() -> None:
    pins = parse_versions((INGRESS / "versions.env").read_text(encoding="utf-8"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins["CADDY_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{128}", pins["CADDY_SHA512"])
    assert 'echo "$CADDY_SHA512  $deb" | sha512sum -c -' in SETUP
    assert "releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.deb" in SETUP
    assert '!= "$CADDY_VERSION"' in SETUP  # re-installed only when the version differs


def test_the_pod_pins_autossh_in_the_e1_s4_snapshot() -> None:
    pins = parse_versions((RUNPOD / "versions.env").read_text(encoding="utf-8"))
    assert "autossh=1.4g-1" in pins["APT_PINS"].split()


# ---------------------------------------------------------------- Caddyfile


def test_caddy_serves_the_stable_hostname_over_tls_to_the_tunnel() -> None:
    assert "\n{$CDI_INGRESS_HOSTNAME} {\n" in CADDY  # a hostname site = automatic HTTPS
    assert "email {$CDI_INGRESS_ACME_EMAIL}" in CADDY
    assert "reverse_proxy 127.0.0.1:{$CDI_INGRESS_REMOTE_PORT} {" in CADDY
    assert 'Strict-Transport-Security "max-age=31536000"' in CADDY
    assert "admin localhost:2019" in CADDY  # the admin API is never exposed


def test_caddy_health_checks_by_content_not_status() -> None:
    proxy = CADDY[CADDY.index("reverse_proxy") :]
    assert "health_uri /login\n" in proxy  # E11-S1: the one public page carries the marker
    assert 'curl -fsS "http://127.0.0.1:$PORT/login"' in START
    marker = re.search(r'health_body "([^"]+)"', proxy).group(1)  # type: ignore[union-attr]
    assert marker in START  # the same marker start_all.sh checks (§10.4)
    assert "max_fails 1" in proxy and "fail_duration" in proxy  # passive: first failure counts


def test_a_dead_backend_is_a_distinguishable_503() -> None:
    errors = CADDY[CADDY.index("handle_errors") :]
    assert errors.startswith("handle_errors 502 503 504 {")
    assert 'header X-CDI-Ingress "backend-down"' in errors
    assert f'respond "{check_ingress.MARKER}:' in errors and '" 503 {' in errors


def test_the_ui_is_clinic_only_until_there_is_a_login() -> None:
    block = CADDY[CADDY.index("@outside {") : CADDY.index("reverse_proxy")]
    assert "not remote_ip {$CDI_INGRESS_ALLOW}" in block
    assert "not path /healthz" in block  # monitors may see component booleans only
    assert '" 403 {' in block
    assert CADDY.index("respond @outside") < CADDY.index("reverse_proxy")


# ---------------------------------------------------------------- setup_ingress_vm.sh


def _setup_fn(call: str, **env: str) -> subprocess.CompletedProcess[str]:
    script = f". '{(INGRESS / 'setup_ingress_vm.sh').as_posix()}'; {call}"
    return subprocess.run([_need_bash(), "-c", script], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], **env})  # fmt: skip


def test_sourcing_the_setup_script_runs_nothing() -> None:
    r = _setup_fn("echo sourced")
    assert r.returncode == 0 and r.stdout.strip() == "sourced", r.stderr


def test_tunnel_key_may_forward_one_port_and_nothing_else() -> None:
    r = _setup_fn("authorized_keys_line 18888 'ssh-ed25519 AAAAkey cdi-pod-tunnel'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        'restrict,port-forwarding,permitlisten="127.0.0.1:18888",command="/usr/sbin/nologin" '
        "ssh-ed25519 AAAAkey cdi-pod-tunnel\n"
    )
    sshd = SETUP[SETUP.index("Match User $TUNNEL_USER") :]
    for must in ("AllowTcpForwarding remote", "GatewayPorts no", "PermitTTY no",
                 "AuthenticationMethods publickey", "ClientAliveInterval 15"):  # fmt: skip
        assert must in sshd, must
    assert "sshd -t" in SETUP  # a broken sshd config is never loaded


GOOD = {
    "CDI_INGRESS_HOSTNAME": "emr.example.in",
    "CDI_INGRESS_ACME_EMAIL": "it@example.in",
    "CDI_INGRESS_ALLOW": "203.0.113.10/32 198.51.100.0/28",
    "CDI_INGRESS_POD_KEYS": "ssh-ed25519 AAAAkey cdi-pod-tunnel",
    "CDI_INGRESS_REMOTE_PORT": "18888",
}


def test_setup_accepts_a_complete_clinic_only_config() -> None:
    r = _setup_fn("check_config", **GOOD)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"CDI_INGRESS_ALLOW": ""}, "never public"),
        ({"CDI_INGRESS_ALLOW": "203.0.113.10/32 0.0.0.0/0"}, "that is everyone"),
        ({"CDI_INGRESS_ALLOW": "::/0"}, "that is everyone"),
        ({"CDI_INGRESS_HOSTNAME": ""}, "CDI_INGRESS_HOSTNAME is not set"),
        ({"CDI_INGRESS_ACME_EMAIL": ""}, "CDI_INGRESS_ACME_EMAIL is not set"),
        ({"CDI_INGRESS_POD_KEYS": ""}, "no pod can connect"),
        ({"CDI_INGRESS_REMOTE_PORT": "http"}, "not a port"),
    ],
)
def test_setup_refuses_an_unsafe_or_incomplete_config(
    override: dict[str, str], message: str
) -> None:
    r = _setup_fn("check_config", **{**GOOD, **override})
    assert r.returncode != 0 and message in r.stdout, r.stdout


def test_setup_validates_before_it_reloads() -> None:
    validate = SETUP.index("caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new")
    assert validate < SETUP.index("mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile")
    assert validate < SETUP.index("systemctl reload caddy")
    assert "EnvironmentFile=/etc/caddy/cdi.env" in SETUP
    assert 'CDI_INGRESS_ALLOW="$CDI_INGRESS_ALLOW"' in SETUP  # quoted: bash + systemd both read it
    for port in ("ufw allow OpenSSH", "ufw allow 80/tcp", "ufw allow 443/tcp"):
        assert port in SETUP


def test_the_env_template_passes_the_setup_checks() -> None:
    env: dict[str, str] = {}
    for line in (INGRESS / "cdi-ingress.env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value.strip('"')
    assert set(env) == set(GOOD)
    assert _setup_fn("check_config", **env).returncode == 0


# ---------------------------------------------------------------- start_tunnel.sh (run for real)


FAKES = {
    "ssh-keygen": 'f=""; while [ $# -gt 0 ]; do [ "$1" = -f ] && f=$2; shift; done\n'
    'echo PRIVATE >"$f"; echo "ssh-ed25519 AAAAfake cdi-pod-tunnel" >"$f.pub"\n',
    "autossh": 'echo "$@" >"$WS/autossh.args"\necho $$ >"$AUTOSSH_PIDFILE"\nexec sleep 30\n',
    "setsid": 'exec "$@"\n',
    "nohup": 'exec "$@"\n',
}


@pytest.fixture
def pod(tmp_path: Path) -> Iterator[dict[str, str]]:
    fakes = tmp_path / "bin"
    fakes.mkdir()
    bodies = dict(FAKES)
    if os.name == "nt":  # NTFS has no POSIX modes for Git Bash's install(1); modes are Linux-tested
        bodies["install"] = (
            'd=0; a=(); while [ $# -gt 0 ]; do case $1 in -d) d=1;; -m) shift;; *) a+=("$1");; '
            'esac; shift; done\nif [ $d = 1 ]; then mkdir -p "${a[@]}"; else cp "${a[@]}"; fi\n'
        )
    for name, body in bodies.items():
        (fakes / name).write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        (fakes / name).chmod(0o755)
    ws = tmp_path / "ws"
    (ws / "cdi").mkdir(parents=True)
    env = {
        "FAKES": fakes.as_posix(),
        "WS": ws.as_posix(),
        "CDI_SECRETS_DIR": (tmp_path / "run-secrets").as_posix(),
    }
    yield env
    pid = ws / "logs" / "tunnel.pid"
    if pid.exists():
        subprocess.run([_need_bash(), "-c", f"kill {pid.read_text().strip()} 2>/dev/null"])


def _tunnel(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    # bash puts the fakes first itself (Git Bash converts the Windows PATH only at startup)
    run = ('d=$FAKES; if command -v cygpath >/dev/null; then d=$(cygpath -u "$d"); fi; '
           f'PATH="$d:$PATH" exec bash "{(RUNPOD / "start_tunnel.sh").as_posix()}"')  # fmt: skip
    return subprocess.run([_need_bash(), "-c", run], capture_output=True, text=True,
                          env={**os.environ, **env, **extra}, timeout=60)  # fmt: skip


CONFIGURED = {
    "CDI_INGRESS_SSH": "cdi-tunnel@ingress.example.in",
    "CDI_INGRESS_SSH_PORT": "2222",
    "CDI_INGRESS_HOSTNAME": "emr.example.in",
    "CDI_INGRESS_HOST_KEY": "ssh-ed25519 AAAAvmhostkey",
}


def test_unconfigured_pod_skips_the_tunnel(pod: dict[str, str]) -> None:
    r = _tunnel(pod, CDI_INGRESS_SSH="")
    assert r.returncode == 0 and "not configured" in r.stdout, r.stderr
    assert not (Path(pod["WS"]) / "autossh.args").exists()


def test_a_missing_host_key_refuses_to_connect(pod: dict[str, str]) -> None:
    r = _tunnel(pod, **{**CONFIGURED, "CDI_INGRESS_HOST_KEY": ""})
    assert r.returncode != 0 and "CDI_INGRESS_HOST_KEY" in r.stderr
    assert not (Path(pod["WS"]) / "autossh.args").exists()


def test_tunnel_opens_once_with_a_pinned_host_key(pod: dict[str, str]) -> None:
    ws = Path(pod["WS"])
    first = _tunnel(pod, **CONFIGURED)
    assert first.returncode == 0, first.stderr
    assert "new tunnel key" in first.stdout
    assert "ssh-ed25519 AAAAfake cdi-pod-tunnel" in first.stdout
    assert "tunnel started" in first.stdout

    args = (ws / "autossh.args").read_text().split()
    assert args[:3] == ["-M", "0", "-N"]
    assert args[args.index("-R") + 1] == "127.0.0.1:18888:127.0.0.1:8888"
    assert args[args.index("-p") + 1] == "2222" and args[-1] == "cdi-tunnel@ingress.example.in"
    for opt in ("StrictHostKeyChecking=yes", "ExitOnForwardFailure=yes", "BatchMode=yes",
                "ServerAliveInterval=15", "IdentitiesOnly=yes"):  # fmt: skip
        assert opt in args, opt
    secrets = Path(pod["CDI_SECRETS_DIR"])
    known = (secrets / "ingress_known_hosts").read_text()
    assert known == "[ingress.example.in]:2222 ssh-ed25519 AAAAvmhostkey\n"
    assert args[args.index("-i") + 1].endswith("tunnel_id_ed25519")
    assert (secrets / "tunnel_id_ed25519").read_text().strip() == "PRIVATE"

    (ws / "autossh.args").unlink()
    second = _tunnel(pod, **CONFIGURED)  # start_all.sh on a pod that is already up
    assert second.returncode == 0 and "already running" in second.stdout, second.stderr
    assert "new tunnel key" not in second.stdout  # the persistent key is reused
    assert not (ws / "autossh.args").exists()


def test_start_all_prints_the_counter_url_as_the_address_of_record() -> None:
    assert START.index('bash "$HERE/start_tunnel.sh"') > START.index('echo "LIVE URL:')
    assert 'COUNTER="https://$CDI_INGRESS_HOSTNAME"' in START
    assert 'echo "COUNTER URL: $COUNTER   (address of record' in START
    assert "scripts/check_ingress.py" in START


def test_pod_env_ships_the_ingress_unconfigured() -> None:
    env = (ROOT / ".env.runpod").read_text(encoding="utf-8")
    for key in ("CDI_INGRESS_HOSTNAME=", "CDI_INGRESS_SSH=", "CDI_INGRESS_HOST_KEY="):
        assert f"\n{key}\n" in env, key
    from cdi_adapter.config import Settings

    Settings(_env_file=None, ingress_hostname="x")  # type: ignore[call-arg]  # extra keys ignored


# ---------------------------------------------------------------- check_ingress.py


def test_classify_tells_the_layers_apart() -> None:
    ok = json.dumps({"status": "ok", "db": True, "s3": True, "mlserve": True})
    bad = json.dumps({"status": "degraded", "db": True, "s3": False, "mlserve": False})
    down = f"{check_ingress.MARKER}: the EMR service is not reachable right now."
    c = check_ingress.classify
    assert c(200, {}, ok).state == "UP"
    v = c(503, {}, bad)
    assert v.state == "DEGRADED" and v.detail.endswith("down: s3, mlserve") and v.code == 2
    assert c(503, {"X-CDI-Ingress": "backend-down"}, down).state == "BACKEND_DOWN"
    # the RunPod failure mode this block exists for: HTTP 200 wrapped around an error page
    assert c(200, {}, "<html><h1>502 Bad Gateway</h1></html>").state == "UNREACHABLE"
    assert c(503, {}, down).state == "UNREACHABLE"  # marker without the ingress header
    assert c(200, {}, json.dumps({"status": "ok"})).state == "UNREACHABLE"  # not our /healthz
    assert c(None, {}, "").code == 4


def test_the_real_healthz_contract_classifies(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from cdi_adapter.webapp import app as webapp

    class Store:
        def healthy(self) -> bool:
            return True

    def no_db() -> Any:
        raise OSError("db down")

    monkeypatch.setattr(webapp, "get_store", lambda: Store())
    monkeypatch.setattr(webapp, "session_scope", no_db)
    r = TestClient(webapp.app).get("/healthz")
    v = check_ingress.classify(r.status_code, dict(r.headers), r.text)
    assert (v.state, v.detail) == ("DEGRADED", "web app reachable; down: db")


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], str]] = {}

    def do_GET(self) -> None:  # noqa: N802
        status, headers, body = self.routes[self.server.server_address[1]]  # type: ignore[index]
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args: Any) -> None:
        pass


def _serve(status: int, headers: dict[str, str], body: str) -> tuple[str, http.server.HTTPServer]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.routes[srv.server_address[1]] = (status, headers, body)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


@pytest.mark.parametrize(
    ("status", "headers", "body", "state", "code"),
    [
        (200, {}, '{"status":"ok","db":true,"s3":true,"mlserve":true}', "UP", 0),
        (503, {}, '{"status":"degraded","db":false,"s3":true,"mlserve":true}', "DEGRADED", 2),
        (503, {"X-CDI-Ingress": "backend-down"}, "CDI_BACKEND_DOWN: retry", "BACKEND_DOWN", 3),
        (200, {}, "<h1>502 Bad Gateway</h1>", "UNREACHABLE", 4),
    ],
)
def test_the_cli_probes_over_http(
    status: int, headers: dict[str, str], body: str, state: str, code: int, capsys: Any
) -> None:
    url, srv = _serve(status, headers, body)
    try:
        assert check_ingress.main([url]) == code
        assert capsys.readouterr().out.startswith(f"{state}:")
    finally:
        srv.shutdown()


def test_nothing_listening_is_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    v = check_ingress.probe(f"http://127.0.0.1:{port}", timeout=2)
    assert v.state == "UNREACHABLE" and v.code == 4


def test_runbook_covers_setup_operation_and_acceptance() -> None:
    book = (ROOT / "docs" / "ingress.md").read_text(encoding="utf-8")
    for must in ("setup_ingress_vm.sh", "CDI_INGRESS_POD_KEYS", "CDI_INGRESS_HOST_KEY",
                 "check_ingress.py", "BACKEND_DOWN", "address of record", "India",
                 "no Docker", "Acceptance test"):  # fmt: skip
        assert must in book, must
