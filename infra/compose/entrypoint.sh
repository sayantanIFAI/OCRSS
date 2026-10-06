#!/bin/sh
set -eu
# Docker secret mounts remain outside the process environment. Pydantic reads a private copy.
install -d -m 700 -o cdi -g cdi /run/cdi-secrets
for name in cdi_database_url cdi_redis_url cdi_s3_access_key cdi_s3_secret_key; do
    install -m 600 -o cdi -g cdi "/run/secrets/$name" "/run/cdi-secrets/$name"
done
mkdir -p /data/inbox /data/processed /data/failed
chown cdi:cdi /data /data/inbox /data/processed /data/failed
exec gosu cdi "$@"
