# Alt-deploy image (TECH_STACK §13): python:3.11-slim + libGL/glib (OpenCV, PyMuPDF) +
# postgresql-client (pg_dump / psql). CPU only — the GPU model gateway runs on the pod.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 cdi
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY db ./db
COPY schemas ./schemas
RUN pip install --no-cache-dir -e ".[ocr]"
USER cdi
