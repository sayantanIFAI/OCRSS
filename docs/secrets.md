# Secrets (backlog E1-S5)

ARCHITECTURE §12 "Secret hygiene" · code: `src/cdi_adapter/secretstore.py` · scan: `scripts/secret_scan.py`

## What is a secret here

The four credential settings (ARCHITECTURE §9): `CDI_DATABASE_URL` (holds the DB password),
`CDI_REDIS_URL` (holds the Redis password), `CDI_S3_ACCESS_KEY`, `CDI_S3_SECRET_KEY`. Plus the
`RUNPOD_API_KEY`, which is not ours: the RunPod platform injects it into the pod.

## The store

- One file per secret, named after the env var in lower case (`cdi_database_url`, ...), in a
  directory readable only by the service owner: **dir 0700, files 0600**.
- `get_settings()` loads it through pydantic-settings' `secrets_dir`. Location: `CDI_SECRETS_DIR`,
  else `~/.cdi/secrets` if it exists; with neither, the local-dev defaults apply (dev box only).
- A store readable by group/other **refuses to load** (`PermissionError`); a configured store that
  does not exist fails (`FileNotFoundError`). Credentials are `repr=False` on `Settings`, so they
  do not appear in logs or tracebacks.
- Precedence is pydantic-settings' own: environment > `.env` > store. So a shared host must keep
  credentials out of its `.env` and environment — the secret scan enforces that for the repo's
  pod config (`.env.runpod`).
- No new server: no vault product is named in ARCHITECTURE.md or TECH_STACK.md, and the pod runs
  everything as native processes (§3.3). A KMS-backed vault is §14 #9 / E1-S10 work.

```bash
python -m cdi_adapter.secretstore init        # generate every missing secret (never overwrites)
python -m cdi_adapter.secretstore check       # modes, completeness, no dev defaults, >= 16 chars
python -m cdi_adapter.secretstore list        # names only
python -m cdi_adapter.secretstore rotate cdi_s3_secret_key
```

## On the pod

`bootstrap_pod.sh` step 0:

1. `init` the persistent copy in `/workspace/secrets` (first boot only).
2. Copy it to `CDI_SECRETS_DIR=/run/cdi-secrets` (overlay) as 0700/0600 and `check` it.
3. Derive `pgpass` (libpq) and `redis.conf` (`requirepass`) there, so no password is ever on a
   command line (`/proc/<pid>/cmdline` is world-readable).
4. Re-apply the Postgres role password from the store (SQL on stdin); start Redis with the
   `requirepass` file; start MinIO with the root credentials from the store.

**Known limit, recorded, not hidden:** `/workspace` is MooseFS, which forces mode 0777 (the same
reason PGDATA lives on the overlay, ARCHITECTURE §3.3). The persistent copy is therefore readable
by any process on the pod. On the single-tenant pilot pod everything runs as root anyway, which is
why §12's remedy is "rotate; don't share pods". The production host (E1-S10) must keep the store
on a filesystem that honours modes, or in a KMS.

**First boot after this change on an existing pod:** MinIO's data directory was created with the
old default root credentials. The bootstrap starts MinIO with the new ones; confirm `/healthz`
reports `s3: ok` and that existing objects are still readable. This has not been run yet — it is
part of the pod verification that is still pending (the pod is not available to this build).

## Rotating a credential

```bash
python -m cdi_adapter.secretstore --dir /workspace/secrets rotate cdi_database_url
bash /workspace/cdi/infra/runpod/start_all.sh   # re-copies the store, re-applies the DB password
```

Restart every process that uses the rotated secret (webapp, worker, watcher).

## Rotating the exposed `RUNPOD_API_KEY` (owner action — 🔒)

ARCHITECTURE §12 flags that `RUNPOD_API_KEY` is readable in `/proc/1/environ` on the pod. Only the
RunPod account owner can rotate it:

1. RunPod console → Settings → API Keys → create a new key with the least scope needed.
2. Update anything that uses the key (none of this repository's code does).
3. Delete the old key in the console.
4. Do not share the pod (the key stays readable by root on it).

## Secret scan

`python scripts/secret_scan.py` (also the `secrets` stage of `scripts/ci.py`) fails on RunPod /
Hugging Face / GitHub / AWS keys, private keys, and connection URLs carrying a real password
anywhere; and on the dev defaults outside the files that are explicitly dev/CI-only. Findings are
masked. It scans git history too once the project is a git repository (today it is not).

## Misuse tests (`tests/test_secrets_unit.py`)

| Misuse | Result |
|---|---|
| Store readable by group/other | `get_settings()` raises `PermissionError`; `check` fails |
| `CDI_SECRETS_DIR` points at a missing directory | `FileNotFoundError` |
| Dev default (`cdi`, `minioadmin`), empty, short, or password-less URL in the store | `check` fails, value never echoed |
| Credential printed via `Settings` repr/str | not present (`repr=False`) |
| Credential committed to the pod config or pod scripts | `secret_scan.py` fails the CI gate |
