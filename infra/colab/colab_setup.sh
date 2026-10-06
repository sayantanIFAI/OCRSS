#!/usr/bin/env bash
# One-shot rebuild of the whole stack on a fresh Google Colab VM (backlog E1-S3: Colab is one of the
# "fallback hosts" docs/fallback-host.md describes — Ubuntu 24.04, root, NVIDIA GPU).
#
# Colab is a VERIFICATION host only: it is Google-hosted outside India, so no real PHI ever goes on
# it (ARCHITECTURE residency rule). Synthetic / golden data only. Its VM is wiped after ~90 minutes
# idle, so this script exists to make the rebuild one command instead of twenty.
#
# Usage, from a notebook cell, after uploading the source archive to /content:
#     !bash /content/cdi/infra/colab/colab_setup.sh 2>&1 | tail -40
# or, when only the archive is there yet:
#     !mkdir -p /content/cdi && tar -xzf /content/cdi-src.tar.gz -C /content/cdi \
#        && bash /content/cdi/infra/colab/colab_setup.sh
#
# Differences from the pod that this script handles:
#   * Colab's `python3` is 3.13; the venv must be built with the 3.12 the lock is pinned to, so a
#     PATH shim puts /usr/bin/python3.12 first.
#   * The `cdi` role does not own CREATEDB, which the test suite needs for its throwaway `cdi_test`
#     database. Granted here, on the test host only — never in bootstrap_pod.sh, because the
#     production app role has no business creating databases.
#   * bootstrap_pod.sh currently stops at step 5b on FHIR package digest drift (infra/fhir/
#     packages.lock is stale — it drifts on the Windows dev box too). Postgres, Redis and MinIO are
#     all started before that step, so the bootstrap failure is tolerated here and the services it
#     did start are verified explicitly. E0-S16 stays Blocked until the lock is refreshed through
#     docs/ig-upgrade.md.
set -uo pipefail

WS=${WS:-/workspace}
REPO=${REPO:-/content/cdi}
SHIM=/opt/cdi-shim
export WS REPO

step() { printf '\n=== %s\n' "$1"; }

step "0. python3.12 shim (Colab's python3 is 3.13; the lock is cp312)"
mkdir -p "$SHIM" "$WS"
ln -sf /usr/bin/python3.12 "$SHIM/python3"
export PATH=$SHIM:$PATH
python3 --version

step "1. .env (from the pod template; stub backend for the Phase 0 test pass)"
cd "$REPO"
[ -f .env ] || cp .env.runpod .env
sed -i 's/^CDI_MLSERVE_BACKEND=hf$/CDI_MLSERVE_BACKEND=stub/' .env
grep -e MLSERVE_BACKEND -e IG_PACKAGE .env

step "2. bootstrap (apt pins, Postgres 16.15, venv, Redis 7, MinIO from source)"
# Tolerated: see the header note on step 5b / FHIR drift.
bash "$REPO/infra/runpod/bootstrap_pod.sh" || echo "bootstrap returned $? - services checked below"

step "3. services"
ok=0
curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 \
  && echo "minio   OK" || { echo "minio   DOWN"; ok=1; }
redis-cli -p 6379 ping >/dev/null 2>&1 \
  && echo "redis   OK" || echo "redis   (auth-protected or down; checked again by the suite)"
su postgres -c "/usr/lib/postgresql/16/bin/pg_isready -h /tmp" >/dev/null 2>&1 \
  && echo "pg      OK" || { echo "pg      DOWN"; ok=1; }
[ "$ok" = 0 ] || { echo "required services are down; see $WS/logs/bootstrap.log"; exit 1; }

step "4. test-host grants + test tooling"
# The suite creates and drops its own cdi_test database (tests/conftest.py).
su postgres -c "psql -h /tmp -c 'ALTER ROLE cdi CREATEDB'"
# requirements.lock is the production set and deliberately has no test tools.
"$REPO/.venv/bin/python" -m pip install -q pytest==9.1.1

step "5. done - export these to run the suite"
cat <<'HINT'
  S=/run/cdi-secrets
  DBURL=$(python3 src/cdi_adapter/secretstore.py --dir $S get cdi_database_url)
  export CDI_TEST_ADMIN_URL=${DBURL%/cdi}/postgres        # the stored URL: its password is encoded
  export CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000
  export CDI_S3_ACCESS_KEY_TEST=$(python3 src/cdi_adapter/secretstore.py --dir $S get cdi_s3_access_key)
  export CDI_S3_SECRET_KEY_TEST=$(python3 src/cdi_adapter/secretstore.py --dir $S get cdi_s3_secret_key)
  export CDI_MLSERVE_BACKEND=stub CDI_TEST_REQUIRE_INFRA=1
  .venv/bin/python -m pytest -m integration -q
HINT
