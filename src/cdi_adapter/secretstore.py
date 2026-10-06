"""File-based secrets store (backlog E1-S5; ARCHITECTURE §12 "Secret hygiene").

Credentials live outside the repository and outside ``.env``, one file per secret in a directory
readable by the service owner only (dir 0700, files 0600), and are loaded by ``get_settings()``
through pydantic-settings' ``secrets_dir``. A file is named after its setting's environment
variable in lower case (``cdi_database_url``, ...). A configured store is authoritative for
credentials in ``get_settings()``; other settings keep normal environment/``.env`` precedence.

The store is located by ``CDI_SECRETS_DIR``, else ``~/.cdi/secrets`` if that directory exists.

    python -m cdi_adapter.secretstore init             # generate every missing secret
    python -m cdi_adapter.secretstore check            # permissions, completeness, no dev defaults
    python -m cdi_adapter.secretstore list             # names only, never values
    python -m cdi_adapter.secretstore rotate cdi_s3_secret_key
    python -m cdi_adapter.secretstore get cdi_redis_url --part password   # for scripts only
    python -m cdi_adapter.secretstore pgpass OUT       # libpq password file (no password in argv)
    python -m cdi_adapter.secretstore redis-conf OUT   # `requirepass` include file for redis-server

Stdlib only, so pod scripts can run it before the virtualenv exists.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

ENV_VAR = "CDI_SECRETS_DIR"
DEFAULT_DIR = Path.home() / ".cdi" / "secrets"

# the Settings fields that are credentials (ARCHITECTURE §9)
FIELDS = ("database_url", "redis_url", "s3_access_key", "s3_secret_key")
# service credentials that are not Settings fields: Grafana admin (E10-S2), the Postgres
# replication role the standby connects as (E10-S6)
SERVICE_NAMES = ("cdi_grafana_admin_password", "cdi_replication_password")
NAMES = tuple(f"cdi_{f}" for f in FIELDS) + SERVICE_NAMES

# the well-known dev defaults in config.py / .env.example: never acceptable in a store
DEV_PASSWORDS = frozenset({"cdi", "minioadmin", "postgres", "password", "changeme"})
MIN_SECRET_LEN = 16
POSIX = os.name == "posix"


def active_dir(configured: str | None = None) -> Path | None:
    """The store in use: ``configured`` (Settings.secrets_dir) or ``CDI_SECRETS_DIR``, else the
    default dir if it exists."""
    configured = configured or os.environ.get(ENV_VAR)
    if configured:
        return Path(configured)
    return DEFAULT_DIR if DEFAULT_DIR.is_dir() else None


def insecure_modes(store: Path) -> list[str]:
    """Group/other access on the store or any file in it (POSIX only: Windows has no mode bits)."""
    if not POSIX or not store.exists():
        return []
    problems = []
    if store.is_symlink():
        problems.append("store directory must not be a symlink")
    if stat.S_IMODE(store.stat().st_mode) & 0o077:
        problems.append(
            f"{store}: directory mode {oct(stat.S_IMODE(store.stat().st_mode))}, want 0700"
        )
    for f in sorted(store.iterdir()):
        if f.is_symlink():
            problems.append(f"{f.name}: secret must not be a symlink")
            continue
        mode = stat.S_IMODE(f.stat().st_mode)
        if mode & 0o077:
            problems.append(f"{f.name}: mode {oct(mode)}, want 0600")
    return problems


def assert_safe(store: Path) -> None:
    problems = insecure_modes(store)
    if problems:
        raise PermissionError("secrets store is readable by others: " + "; ".join(problems))


def read(store: Path, name: str) -> str | None:
    p = store / name
    return p.read_text(encoding="utf-8").strip() if p.is_file() else None


def write(store: Path, name: str, value: str) -> None:
    if store.is_symlink():
        raise PermissionError("store directory must not be a symlink")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if POSIX:
        os.chmod(store, 0o700)
    assert_safe(store)
    path = store / name
    if path.is_symlink():
        raise PermissionError(f"secret must not be a symlink: {name}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if POSIX and stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.close(fd)
        raise PermissionError("filesystem does not enforce private secret permissions")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    if POSIX:
        os.chmod(path, 0o600)


def _password() -> str:
    return secrets.token_urlsafe(32)  # URL-safe alphabet: no escaping inside connection URLs


def generate(name: str) -> str:
    """A fresh value for one secret (local-only hosts, as ARCHITECTURE §3.3 runs them)."""
    if name == "cdi_database_url":
        return f"postgresql+psycopg://cdi:{_password()}@127.0.0.1:5432/cdi"
    if name == "cdi_redis_url":
        return f"redis://:{_password()}@127.0.0.1:6379/0"
    if name == "cdi_s3_access_key":
        return "cdi-" + secrets.token_hex(8)
    if name in ("cdi_s3_secret_key", "cdi_grafana_admin_password", "cdi_replication_password"):
        return _password()
    raise KeyError(name)


def init(store: Path) -> list[str]:
    """Create every missing secret; never overwrites one that exists. Returns the names created."""
    created = []
    for name in NAMES:
        if read(store, name) is None:
            write(store, name, generate(name))
            created.append(name)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    return created


def rotate(store: Path, name: str) -> None:
    if name not in NAMES:
        raise KeyError(name)
    write(store, name, generate(name))


def part(value: str, which: str) -> str:
    """``password`` / ``username`` of a URL secret; ``value`` returns it whole."""
    if which == "value":
        return value
    u = urlsplit(value)
    got = u.password if which == "password" else u.username
    return unquote(got or "")


def _secret_strength(name: str, value: str) -> str | None:
    if not value:
        return "empty"
    pw = part(value, "password") if name.endswith("_url") else value
    if name == "cdi_s3_access_key":  # an identifier, not a password: only refuse the default
        return "dev default" if value in DEV_PASSWORDS else None
    if not pw:
        return "URL has no password"
    if pw in DEV_PASSWORDS:
        return "dev default"
    if len(pw) < MIN_SECRET_LEN:
        return f"shorter than {MIN_SECRET_LEN} characters"
    return None


def check(store: Path, *, names: Sequence[str] = NAMES) -> list[str]:
    """Every problem with a store; an empty list means it is fit for a shared host."""
    if not store.is_dir():
        return [f"{store}: no such store (run `init`)"]
    problems = insecure_modes(store)
    for name in names:
        value = read(store, name)
        if value is None:
            problems.append(f"{name}: missing")
            continue
        why = _secret_strength(name, value)
        if why:
            problems.append(f"{name}: {why}")
    return problems


def pgpass_line(database_url: str) -> str:
    u = urlsplit(database_url)
    esc = [
        (x or "").replace("\\", "\\\\").replace(":", "\\:")
        for x in (
            u.hostname or "127.0.0.1",
            str(u.port or 5432),
            u.path.lstrip("/") or "*",
            unquote(u.username or ""),
            unquote(u.password or ""),
        )
    ]
    return ":".join(esc)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if POSIX:
        os.chmod(path, 0o600)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.secretstore")
    ap.add_argument(
        "--dir", type=Path, help=f"store directory (default: ${ENV_VAR} or {DEFAULT_DIR})"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("check")
    sub.add_parser("list")
    sub.add_parser("rotate").add_argument("name", choices=NAMES)
    g = sub.add_parser("get")
    g.add_argument("name", choices=NAMES)
    g.add_argument("--part", choices=("value", "password", "username"), default="value")
    sub.add_parser("pgpass").add_argument("out", type=Path)
    sub.add_parser("redis-conf").add_argument("out", type=Path)
    args = ap.parse_args(argv)
    store = args.dir or active_dir() or DEFAULT_DIR

    if args.cmd == "init":
        created = init(store)
        print(f"{store}: created {', '.join(created) or 'nothing (all present)'}")
        return 0
    if args.cmd == "check":
        problems = check(store)
        for p in problems:
            print(f"FAIL {p}")
        print(f"{store}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
        return 1 if problems else 0
    if args.cmd == "list":
        for name in NAMES:
            print(f"{name}\t{'present' if read(store, name) is not None else 'MISSING'}")
        return 0
    if args.cmd == "rotate":
        rotate(store, args.name)
        print(f"{args.name}: rotated (restart the services that use it)")
        return 0
    source = {"get": getattr(args, "name", ""), "pgpass": "cdi_database_url"}.get(
        args.cmd, "cdi_redis_url"
    )
    value = read(store, source)
    if value is None:
        print("secret missing (run `init`)", file=sys.stderr)
        return 1
    if args.cmd == "get":
        print(part(value, args.part))
    elif args.cmd == "pgpass":
        _write_private(args.out, pgpass_line(value) + "\n")
    else:
        _write_private(args.out, f"requirepass {part(value, 'password')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
