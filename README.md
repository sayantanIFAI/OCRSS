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

Integration tests need PostgreSQL 16 and real MinIO. CI uses the [Compose service containers](docs/compose.md); native services also work. On Windows, a local
dev/test server runs from `.tools/` (download it there first; the folder is git-ignored):

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

[![ci](https://github.com/manishtech0607-cmyk/CDI-Adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/manishtech0607-cmyk/CDI-Adapter/actions/workflows/ci.yml)

Non-RunPod fallback: [Compose runbook](docs/compose.md).
Epic 1 release gates: [readiness assessment](docs/epic-1-readiness.md).

Run CLI tools as `python -m <tool>`: Windows Smart App Control blocks pip's unsigned `.exe` launchers.

Configuration is read from `CDI_*` environment variables or a `.env` file; see `.env.example`.
