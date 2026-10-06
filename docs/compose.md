# Compose fallback (E1-S3)

This non-RunPod stack runs PostgreSQL 16, Redis 7, source-built MinIO, a model gateway,
webapp, and queue worker. Migrations and bucket creation must finish before the app/worker
start. The gateway and backing services have no published ports. The webapp binds host
loopback on port 8888; put the documented TLS ingress in front of it for staff access.

## Synthetic verification

Prepare Python 3.12 and `pip install -e '.[dev,ocr,load]'`. Docker with a current Compose
and Buildx is required. From the repository root:

```bash
export CDI_COMPOSE_SECRETS_DIR="$HOME/.local/share/cdi-compose/secrets"
python scripts/compose_secrets.py "$CDI_COMPOSE_SECRETS_DIR"
# Only for local HTTP verification. Keep true behind production TLS.
export CDI_SESSION_COOKIE_SECURE=false
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
curl --fail http://127.0.0.1:8888/healthz
curl --fail http://127.0.0.1:8888/readyz
# Create the first account interactively; never put its password in argv.
docker compose -f infra/compose/docker-compose.yml exec webapp \
  python -m cdi_adapter.webapp.users add counter --name 'Synthetic Counter' --role intake
# Explicit test database; live cdi is never reset.
docker compose -f infra/compose/docker-compose.yml run --rm webapp \
  python scripts/golden_path.py --mode full --baseline stub --fresh-db cdi_golden
```

MinIO builds from a verified source commit because the previous registry image cannot be
pulled. On a network with a private CA, the optional BuildKit secret `build_ca` may supply
its trusted CA bundle. Certificate verification stays enabled.

The default backend is **stub**, for synthetic regression only. This image does not contain
GPU libraries or weights. A real-model fallback deployment and GPU capacity must be validated
before clinical use. The historical 15-fact acceptance requirement remains unresolved; the
current general-purpose stub extracts 21 facts. See `epic-1-readiness.md`.

## Credentials and persistence

For production, provision the same named files from a vault to a private host directory
(0700, files 0600); do not use plaintext `.env` values. `compose_secrets.py` generates fresh,
synthetic credentials only for development and CI, retains them on reruns, and refuses a
path inside the checkout. App containers copy their four mounted secrets to a private
owner-only directory and drop privileges before running Python. Configured secret stores
are authoritative for credentials; missing/weak secrets stop startup.

Compose uses named volumes for database, objects, Redis AOF, and inbox data. `down` preserves
them; `down -v` destroys them. Do not regenerate credentials under existing service volumes:
rotate through the backing services and restart consumers as an explicit maintenance action.
The reference database role initializes PostgreSQL and has administrative privileges;
production needs separate migration and least-privilege application roles.

## CI services

`infra/compose/ci.yml` exposes PostgreSQL/MinIO on loopback for host tests, using disposable
volumes and credentials. After starting those services, run `bash scripts/ci_integration.sh`
with `PY` set to the desired Python executable. Never point these tests at shared data.
