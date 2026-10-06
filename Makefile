# Developer shortcuts (TECH_STACK §13 "Make — developer convenience").
PY ?= .venv/bin/python
COMPOSE = docker compose -f infra/compose/docker-compose.yml

.PHONY: venv lock loadtest ci test test-unit lint up down nuke logs migrate migrate-live migrate-rollback schema-status api worker worker-cpu worker-gpu webapp mlserve watch samples smoke

venv:
	python3 -m venv .venv && $(PY) -m pip install -e ".[dev,ocr,load]"

# E1-S4: regenerate infra/runpod/requirements.lock from pyproject.toml + infra/runpod/versions.env
lock:
	$(PY) scripts/lock_pod_deps.py

# E1-S7: 30 patients x 5-10 documents against HOST (e.g. make loadtest HOST=https://emr.example.in)
loadtest:
	$(PY) scripts/load_test.py --host $(HOST)

ci:
	$(PY) scripts/ci.py

test:
	$(PY) -m pytest

test-unit:
	$(PY) -m pytest -m "not integration"

lint:
	$(PY) -m ruff check src tests db && $(PY) -m ruff format --check src tests db && $(PY) -m mypy src

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

nuke:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f --tail=100

migrate:
	$(PY) -m alembic upgrade head

# E9-S1: the same migration with the cluster up - holds the job queue (schema_gate), waits for the
# jobs in flight to end, applies it, releases the queue. DOWN=0020 rolls one back the same way.
migrate-live:
	$(PY) -m cdi_adapter.migration upgrade head

migrate-rollback:
	$(PY) -m cdi_adapter.migration downgrade $(DOWN)

schema-status:
	$(PY) -m cdi_adapter.migration status

api:
	$(PY) -m uvicorn cdi_adapter.api:app --host 127.0.0.1 --port 8080

# one worker for both pools: dev and any single-worker deployment (E9-S2 role "all")
worker:
	$(PY) -m celery -A cdi_adapter.worker.celery_app worker \
	  -Q $$($(PY) -c "from cdi_adapter.worker import queues_for_role as q; print(q('all'))")

# the split pools (E9-S2): scale worker-cpu for Phase-1 throughput, worker-gpu for extraction
worker-cpu:
	$(PY) -m celery -A cdi_adapter.worker.celery_app worker --pool threads \
	  -Q $$($(PY) -c "from cdi_adapter.worker import queues_for_role as q; print(q('cpu'))") \
	  --concurrency $$($(PY) -c "from cdi_adapter.worker import concurrency_for_role as c; print(c('cpu'))")

worker-gpu:
	$(PY) -m celery -A cdi_adapter.worker.celery_app worker --pool threads \
	  -Q $$($(PY) -c "from cdi_adapter.worker import queues_for_role as q; print(q('gpu'))") \
	  --concurrency $$($(PY) -c "from cdi_adapter.worker import concurrency_for_role as c; print(c('gpu'))")

webapp:
	$(PY) -m cdi_adapter.webapp

mlserve:
	$(PY) -m cdi_adapter.mlserve

watch:
	$(PY) -m cdi_adapter.ingest.watcher

samples:
	$(PY) scripts/make_sample_docs.py --out data/samples

smoke:
	$(PY) scripts/pipeline_smoke.py data/samples
