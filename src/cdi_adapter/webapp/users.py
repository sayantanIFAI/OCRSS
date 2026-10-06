"""Staff accounts for the web app (backlog E11-S1). Run on the pod / host:

    python -m cdi_adapter.webapp.users add rdas --name "R. Das" --role intake   # password on stdin
    python -m cdi_adapter.webapp.users passwd rdas        # new password; ends their sessions
    python -m cdi_adapter.webapp.users role rdas reviewer
    python -m cdi_adapter.webapp.users disable rdas | enable rdas | unlock rdas
    python -m cdi_adapter.webapp.users list

Passwords are read from the terminal (hidden) or, when piped, from the first line of stdin;
never from the command line, where they would sit in shell history and the process list.
Every change is written to audit_log (actor ``users-cli``).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.webapp.auth import ROLES, hash_password

ACTOR = "users-cli"


def _password(prompt: str = "Password: ") -> str:
    if sys.stdin.isatty():
        first = getpass.getpass(prompt)
        if getpass.getpass("Again: ") != first:
            raise SystemExit("passwords do not match")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def _one(c: Any, username: str) -> Any:
    row = c.execute(text("SELECT * FROM web_user WHERE username = :u"),
                    {"u": username.lower()}).mappings().first()  # fmt: skip
    if row is None:
        raise SystemExit(f"no user {username!r}")
    return row


def _audit(c: Any, action: str, username: str, **detail: Any) -> None:
    repo.write_audit(c, actor=ACTOR, action=action, entity="web_user", entity_id=username,
                     detail=detail)  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.webapp.users",
                                 description=__doc__.splitlines()[0])  # fmt: skip
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--name", required=True, help="display name")
    a.add_argument("--role", required=True, choices=ROLES)
    for name in ("passwd", "disable", "enable", "unlock"):
        sub.add_parser(name).add_argument("username")
    r = sub.add_parser("role")
    r.add_argument("username")
    r.add_argument("role", choices=ROLES)
    sub.add_parser("list")
    args = ap.parse_args(argv)

    with session_scope() as c:
        if args.cmd == "list":
            for u in c.execute(text("SELECT username, display_name, role, active, locked_until "
                                    "FROM web_user ORDER BY username")).mappings():  # fmt: skip
                state = "active" if u["active"] else "disabled"
                if u["locked_until"]:
                    state += f", locked until {u['locked_until']:%Y-%m-%d %H:%M}"
                print(f"{u['username']:<20} {u['role']:<9} {state:<12} {u['display_name']}")
            return 0
        if args.cmd == "add":
            try:
                pw = hash_password(_password())
            except ValueError as err:
                raise SystemExit(str(err)) from None
            if c.execute(text("SELECT 1 FROM web_user WHERE username = :u"),
                         {"u": args.username.lower()}).first():  # fmt: skip
                raise SystemExit(f"user {args.username!r} already exists")
            c.execute(text("INSERT INTO web_user (username, display_name, role, password_hash) "
                           "VALUES (:u, :n, :r, :p)"),
                      {"u": args.username.lower(), "n": args.name, "r": args.role,
                       "p": pw})  # fmt: skip
            _audit(c, "create", args.username.lower(), role=args.role)
            print(f"added {args.username.lower()} ({args.role})")
            return 0

        user = _one(c, args.username)
        if args.cmd == "passwd":
            try:
                pw = hash_password(_password("New password: "))
            except ValueError as err:
                raise SystemExit(str(err)) from None
            c.execute(text("UPDATE web_user SET password_hash = :p, password_changed_at = now(), "
                           "failed_logins = 0, locked_until = NULL WHERE id = :i"),
                      {"p": pw, "i": user["id"]})  # fmt: skip
        elif args.cmd == "role":
            c.execute(text("UPDATE web_user SET role = :r WHERE id = :i"),
                      {"r": args.role, "i": user["id"]})  # fmt: skip
        elif args.cmd in ("disable", "enable"):
            c.execute(text("UPDATE web_user SET active = :a WHERE id = :i"),
                      {"a": args.cmd == "enable", "i": user["id"]})  # fmt: skip
        elif args.cmd == "unlock":
            c.execute(text("UPDATE web_user SET failed_logins = 0, locked_until = NULL "
                           "WHERE id = :i"), {"i": user["id"]})  # fmt: skip
        if args.cmd in ("passwd", "role", "disable"):  # rights changed: sign the user out now
            c.execute(text("DELETE FROM web_session WHERE user_id = :i"), {"i": user["id"]})
        _audit(c, "update", user["username"], change=args.cmd,
               **({"role": args.role} if args.cmd == "role" else {}))  # fmt: skip
        print(f"{args.cmd}: {user['username']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
