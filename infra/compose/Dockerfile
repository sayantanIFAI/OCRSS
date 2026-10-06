FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 postgresql-client curl gosu \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 cdi
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY db ./db
COPY schemas ./schemas
COPY scripts ./scripts
COPY tests/golden ./tests/golden
COPY infra/compose/entrypoint.sh /usr/local/bin/cdi-entrypoint
RUN pip install --no-cache-dir -e ".[ocr]" && chmod 755 /usr/local/bin/cdi-entrypoint
ENV CDI_SECRETS_DIR=/run/cdi-secrets
ENTRYPOINT ["cdi-entrypoint"]
