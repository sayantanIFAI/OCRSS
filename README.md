# CDI-Adapter

Clinical Document Intelligence Adapter: turns a legacy hospital's scanned-only records into a
governed, structured EMR and ABDM/ABHA FHIR R4 record bundles.

- Architecture (as-built target): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Technology choices: [`docs/TECH_STACK.md`](docs/TECH_STACK.md)
- Feature backlog + build status: [`docs/backlog 1.md`](docs/backlog%201.md)
- Infrastructure backlog + build status: [`docs/backlog infra 1.md`](docs/backlog%20infra%201.md)
- What is actually built, from the code: [`docs/BACKLOG_AS_BUILT.md`](docs/BACKLOG_AS_BUILT.md)

## Local development

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -m "not integration"
.venv/Scripts/python -m ruff check src tests
```

Integration tests need a native PostgreSQL 16 (no Docker; ARCHITECTURE §3.3). On Windows, a local
dev/test server runs from `.tools/` (it must be downloaded there first; the folder is git-ignored):

```bash
powershell -File scripts/dev/pg_local.ps1 start
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m pytest
```

## CI (backlog E1-S1)

One gate, run the same way locally and in `.github/workflows/ci.yml`: ruff lint, ruff format check,
mypy, then `pytest -m "not integration"`. Any red stage fails the run. See [`docs/ci.md`](docs/ci.md).

```bash
.venv/Scripts/python scripts/ci.py
```

<!-- CI badge: add once the repository is on a git host (E1-S1 is Blocked on that), e.g.
     ![ci](https://github.com/<owner>/<repo>/actions/workflows/ci.yml/badge.svg) -->

Run CLI tools as `python -m <tool>`: Windows Smart App Control blocks pip's unsigned `.exe` launchers.

Configuration is read from `CDI_*` environment variables or a `.env` file; see `.env.example`.
