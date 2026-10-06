"""E1-S5: secrets store, settings integration, misuse cases, and the repository secret scan."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from cdi_adapter import secretstore as ss
from cdi_adapter.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "secret_scan", ROOT / "scripts" / "secret_scan.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["secret_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    ss.init(d)
    return d


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No CDI_* credential in the environment and no .env in the working directory."""
    for k in list(os.environ):
        if k.startswith("CDI_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------------------------ the store
def test_init_creates_every_credential_strong_and_unique(store: Path) -> None:
    assert sorted(p.name for p in store.iterdir()) == sorted(ss.NAMES)
    assert ss.check(store) == []
    pw = {
        ss.part(ss.read(store, n) or "", "password") for n in ("cdi_database_url", "cdi_redis_url")
    }
    assert len(pw) == 2 and all(len(p) >= ss.MIN_SECRET_LEN for p in pw)
    assert ss.read(store, "cdi_database_url").startswith("postgresql+psycopg://cdi:")  # type: ignore[union-attr]


def test_init_never_overwrites_an_existing_secret(store: Path) -> None:
    before = ss.read(store, "cdi_s3_secret_key")
    assert ss.init(store) == []
    assert ss.read(store, "cdi_s3_secret_key") == before


def test_rotate_replaces_only_that_secret(store: Path) -> None:
    db, s3 = ss.read(store, "cdi_database_url"), ss.read(store, "cdi_s3_secret_key")
    ss.rotate(store, "cdi_s3_secret_key")
    assert ss.read(store, "cdi_s3_secret_key") != s3
    assert ss.read(store, "cdi_database_url") == db


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("cdi_database_url", "postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi", "dev default"),
        ("cdi_s3_secret_key", "minioadmin", "dev default"),
        ("cdi_s3_access_key", "minioadmin", "dev default"),
        ("cdi_redis_url", "redis://127.0.0.1:6379/0", "URL has no password"),
        ("cdi_s3_secret_key", "short-one", "shorter than"),
        ("cdi_s3_secret_key", "", "empty"),
    ],
)
def test_misuse_dev_default_or_weak_secret_is_rejected(store, name, value, why) -> None:
    ss.write(store, name, value)
    problems = ss.check(store)
    assert len(problems) == 1 and problems[0].startswith(name) and why in problems[0]


def test_missing_secret_and_missing_store_are_reported(store: Path, tmp_path: Path) -> None:
    (store / "cdi_redis_url").unlink()
    assert ss.check(store) == ["cdi_redis_url: missing"]
    assert "no such store" in ss.check(tmp_path / "nope")[0]


def test_misuse_group_readable_store_is_flagged(store: Path, monkeypatch) -> None:
    """Forced POSIX mode check: on Windows every file reports 0o666, i.e. open to others."""
    monkeypatch.setattr(ss, "POSIX", True)
    if os.name != "posix":
        assert ss.insecure_modes(store)  # detection works; the 0600 pass case runs on Linux
        with pytest.raises(PermissionError):
            ss.assert_safe(store)
        return
    os.chmod(store / "cdi_s3_secret_key", 0o644)
    assert ss.insecure_modes(store) == ["cdi_s3_secret_key: mode 0o644, want 0600"]
    os.chmod(store / "cdi_s3_secret_key", 0o600)
    os.chmod(store, 0o755)
    assert "directory mode 0o755" in ss.insecure_modes(store)[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits (Linux CI / pod)")
def test_init_writes_0700_dir_and_0600_files(store: Path) -> None:
    assert ss.insecure_modes(store) == []
    assert oct(store.stat().st_mode & 0o777) == "0o700"


# ------------------------------------------------------------------------------ settings wiring
def test_settings_read_credentials_from_the_store(store, clean_env, monkeypatch) -> None:
    monkeypatch.setenv(ss.ENV_VAR, str(store))
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: [])  # Windows has no mode bits
    s = get_settings()
    assert s.database_url == ss.read(store, "cdi_database_url")
    assert s.s3_secret_key == ss.read(store, "cdi_s3_secret_key")
    assert s.redis_url == ss.read(store, "cdi_redis_url")


def test_environment_still_overrides_the_store(store, clean_env, monkeypatch) -> None:
    monkeypatch.setenv(ss.ENV_VAR, str(store))
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: [])
    monkeypatch.setenv("CDI_S3_BUCKET", "b")
    monkeypatch.setenv("CDI_S3_SECRET_KEY", "from-env-for-a-test")
    assert get_settings().s3_secret_key == "from-env-for-a-test"


def test_misuse_open_store_refuses_to_load(store, clean_env, monkeypatch) -> None:
    monkeypatch.setenv(ss.ENV_VAR, str(store))
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: ["cdi_s3_secret_key: mode 0o644"])
    with pytest.raises(PermissionError, match="readable by others"):
        get_settings()


def test_misuse_configured_store_that_does_not_exist_fails(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv(ss.ENV_VAR, str(tmp_path / "absent"))
    with pytest.raises(FileNotFoundError):
        get_settings()


def test_credentials_never_appear_in_settings_repr(store, clean_env, monkeypatch) -> None:
    monkeypatch.setenv(ss.ENV_VAR, str(store))
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: [])
    text = repr(get_settings()) + str(get_settings())
    for name in ss.NAMES:
        assert (ss.read(store, name) or "x") not in text
    assert "s3_bucket" in text


def test_no_store_keeps_local_dev_defaults(clean_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ss, "DEFAULT_DIR", tmp_path / "no-default-store")
    assert get_settings().s3_access_key == "minioadmin"


# ------------------------------------------------------------------------------------------ CLI
def test_cli_list_prints_names_never_values(store, capsys) -> None:
    assert ss.main(["--dir", str(store), "list"]) == 0
    out = capsys.readouterr().out
    assert all(n in out for n in ss.NAMES)
    assert all((ss.read(store, n) or "x") not in out for n in ss.NAMES)


def test_cli_check_exit_codes(store, capsys, monkeypatch) -> None:
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: [])
    assert ss.main(["--dir", str(store), "check"]) == 0
    ss.write(store, "cdi_s3_secret_key", "minioadmin")
    assert ss.main(["--dir", str(store), "check"]) == 1
    assert "minioadmin" not in capsys.readouterr().out  # the value is never echoed


def test_cli_get_part_and_generated_files(store, capsys, tmp_path) -> None:
    pw = ss.part(ss.read(store, "cdi_redis_url") or "", "password")
    assert ss.main(["--dir", str(store), "get", "cdi_redis_url", "--part", "password"]) == 0
    assert capsys.readouterr().out.strip() == pw
    assert ss.main(["--dir", str(store), "redis-conf", str(tmp_path / "r.conf")]) == 0
    assert (tmp_path / "r.conf").read_text() == f"requirepass {pw}\n"
    assert ss.main(["--dir", str(store), "pgpass", str(tmp_path / "pgpass")]) == 0
    db_pw = ss.part(ss.read(store, "cdi_database_url") or "", "password")
    assert (tmp_path / "pgpass").read_text() == f"127.0.0.1:5432:cdi:cdi:{db_pw}\n"


def test_pgpass_escapes_colons_and_backslashes() -> None:
    line = ss.pgpass_line("postgresql://u:" + "a%3Ab%5Cc" + "@db.local:6543/x")  # scan-clean
    assert line == "db.local:6543:x:u:a\\:b\\\\c"


def test_store_module_is_stdlib_only() -> None:
    """Pod bootstrap runs it with the system python3 before the venv exists."""
    src = (ROOT / "src" / "cdi_adapter" / "secretstore.py").read_text(encoding="utf-8")
    imports = {
        ln.split()[1].split(".")[0]
        for ln in src.splitlines()
        if ln.startswith(("import ", "from "))
    }
    assert imports <= {
        "__future__",
        "argparse",
        "os",
        "secrets",
        "stat",
        "sys",
        "collections",
        "pathlib",
        "urllib",
    }


# ------------------------------------------------------------------------------- secret scan
def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_scan_flags_real_keys_and_masks_them(tmp_path: Path) -> None:
    # built at runtime so this file itself stays clean for test_repository_is_clean
    key = "rpa_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    pw = "S3cr3t" * 3
    pem = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    root = _tree(tmp_path, {
        "notes.md": f"RUNPOD_API_KEY={key}\n",
        "x.env": f"CDI_DATABASE_URL=postgresql://cdi:{pw}@db/cdi\n",
        "k.txt": f"{pem}\n",
    })  # fmt: skip
    findings = scan.scan_tree(root)
    kinds = sorted(f.kind for f in findings)
    assert kinds == ["password-in-url", "private-key", "runpod-api-key"]
    assert all(key not in str(f) and pw not in str(f) for f in findings)


def test_scan_dev_defaults_only_allowed_in_dev_scoped_files(tmp_path: Path) -> None:
    root = _tree(tmp_path, {
        "tests/conftest.py": 'URL = "postgresql://cdi:cdi@127.0.0.1/postgres"\n',
        ".env.example": "CDI_S3_SECRET_KEY=minioadmin\n",
        "infra/runpod/bootstrap_pod.sh": "MINIO_ROOT_PASSWORD=minioadmin\n",
        ".env.runpod": "CDI_DATABASE_URL=postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi\n",
    })  # fmt: skip
    where = sorted(f.where for f in scan.scan_tree(root))
    assert where == [".env.runpod", "infra/runpod/bootstrap_pod.sh"]


def test_scan_ignores_templates_and_placeholders(tmp_path: Path) -> None:
    root = _tree(tmp_path, {
        "a.py": 'u = f"redis://:{_password()}@127.0.0.1:6379/0"\n',
        "b.sh": 'URL="postgresql://cdi:${PW}@host/db"\n',
        "c.md": "postgresql://user:<password>@host/db\n",
    })  # fmt: skip
    assert scan.scan_tree(root) == []


def test_scan_history_is_none_without_git(tmp_path: Path) -> None:
    assert scan.scan_history(tmp_path) is None


def test_repository_is_clean() -> None:
    """The DoD: no credential in plaintext in the repo (working tree)."""
    assert [str(f) for f in scan.scan_tree(ROOT)] == []


def test_store_location_can_come_from_dotenv_like_the_pod(store, clean_env, monkeypatch, tmp_path):
    """The pod's .env (from .env.runpod) sets CDI_SECRETS_DIR, not the process environment."""
    monkeypatch.setattr(ss, "insecure_modes", lambda _d: [])
    (tmp_path / ".env").write_text(f"CDI_SECRETS_DIR={store}\n", encoding="utf-8")
    assert get_settings().s3_secret_key == ss.read(store, "cdi_s3_secret_key")


def test_pod_config_carries_no_credentials() -> None:
    keys = {
        ln.split("=", 1)[0]
        for ln in (ROOT / ".env.runpod").read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#")
    }
    assert keys.isdisjoint({f"CDI_{f}".upper() for f in ss.FIELDS})
    assert "CDI_SECRETS_DIR" in keys
