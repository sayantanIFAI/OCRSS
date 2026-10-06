"""Provision synthetic Compose credentials outside the checkout (local/CI only).

Production: supply the same named files from your vault; see docs/compose.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cdi_adapter import secretstore as ss


def provision(store: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    if store.resolve().is_relative_to(root):
        raise ValueError("secrets must be outside the repository")
    ss.init(store)
    ss.assert_safe(store)
    for name, old, new in (
        ("cdi_database_url", "@127.0.0.1:", "@postgres:"),
        ("cdi_redis_url", "@127.0.0.1:", "@redis:"),
    ):
        value = ss.read(store, name)
        assert value is not None
        ss.write(store, name, value.replace(old, new))
    db = ss.read(store, "cdi_database_url") or ""
    redis = ss.read(store, "cdi_redis_url") or ""
    ss.write(store, "postgres_password", ss.part(db, "password"))
    ss.write(store, "redis.conf", "requirepass " + ss.part(redis, "password"))
    if problems := ss.check(store):
        raise ValueError("; ".join(problems))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    provision(ap.parse_args().directory)
