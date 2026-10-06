# RunPod pod operations (ARCHITECTURE §3.3, §10)

Native processes on one GPU pod — **no Docker**.

| Process | Port | Started by |
|---|---|---|
| PostgreSQL 16 (PGDATA on the overlay, restored from `/workspace/backup/cdi.dump`) | 5432 | `bootstrap_pod.sh` |
| Redis 7 (AOF in `/workspace/redis`) | 6379 | `bootstrap_pod.sh` |
| MinIO (data in `/workspace/minio-data`, built from source at `MINIO_TAG`) | 9000 / 9001 | `bootstrap_pod.sh` |
| model gateway `cdi_adapter.mlserve` | 8077 | `start_mlserve.sh` |
| web app `cdi_adapter.webapp` | 8888 (RunPod-proxied) | `start_all.sh` |

## First boot and every restart

```bash
cd /workspace/cdi && cp .env.runpod .env   # first boot only
bash /workspace/cdi/infra/runpod/start_all.sh
```

The script prints `LIVE URL: https://<POD_ID>-8888.proxy.runpod.net`.

## Snapshots

`snapshot.sh` runs from cron every 15 minutes (installed by `bootstrap_pod.sh`). Run it by hand
before a planned stop. It keeps the last 10 timestamped dumps; `cdi.dump` is always the newest one.

## MinIO build

MinIO publishes no binaries any more, so `bootstrap_pod.sh` builds the pinned tag from the
official repository with the official Go toolchain. Set `GO_SHA256` to the checksum published on
go.dev for `go${GO_VERSION}.linux-amd64.tar.gz` so the toolchain download is verified.

## Health and logs

```bash
curl -s localhost:8888/healthz          # {status, db, s3, mlserve}
curl -s localhost:8077/healthz          # {backend, model, loaded, device}
tail -f /workspace/logs/webapp.log      # pipeline events
PGPASSFILE=/run/cdi-secrets/pgpass psql postgresql://cdi@127.0.0.1:5432/cdi -f scripts/verify_s3.sql
```

RunPod's proxy returns HTTP 200 with a fake 502 page for unbound ports, so health checks here grep
a content marker instead of trusting the status code.
