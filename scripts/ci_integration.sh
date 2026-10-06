#!/usr/bin/env bash
# Disposable Compose credentials; never use this against a production database.
set -euo pipefail
: "${CDI_COMPOSE_SECRETS_DIR:?Set the disposable CI secrets directory}"
export CDI_DATABASE_URL=$(cat "$CDI_COMPOSE_SECRETS_DIR/cdi_database_url")
export CDI_DATABASE_URL=${CDI_DATABASE_URL/@postgres:/@127.0.0.1:}
export CDI_TEST_ADMIN_URL=${CDI_DATABASE_URL%/cdi}/postgres
export CDI_S3_ACCESS_KEY=$(cat "$CDI_COMPOSE_SECRETS_DIR/cdi_s3_access_key")
export CDI_S3_SECRET_KEY=$(cat "$CDI_COMPOSE_SECRETS_DIR/cdi_s3_secret_key")
export CDI_TEST_S3_ENDPOINT=http://127.0.0.1:9000
export CDI_S3_ENDPOINT_URL=$CDI_TEST_S3_ENDPOINT
export CDI_S3_BUCKET=cdi-ci
export CDI_MLSERVE_BACKEND=stub
PY=${PY:-python}
"$PY" -m alembic upgrade head
"$PY" scripts/ci.py --suite integration
