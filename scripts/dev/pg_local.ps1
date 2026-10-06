# Local DEV/TEST PostgreSQL 16 as a native process (no Docker, mirrors ARCHITECTURE §3.3).
# Not for production: the pod runs Postgres via infra/runpod/bootstrap_pod.sh.
#   powershell -File scripts/dev/pg_local.ps1 start|stop|status
param([ValidateSet('start', 'stop', 'status')] [string]$Action = 'status')

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
# 16.14 (official EDB zip): its unsigned initdb.exe has Smart App Control reputation; 16.15's did not.
$bin = Join-Path $root '.tools\pg16_14\pgsql\bin'
$data = Join-Path $root '.tools\pgdata'
$log = Join-Path $root '.tools\pg.log'

if (-not (Test-Path "$bin\pg_ctl.exe")) { throw "PostgreSQL binaries not found in $bin" }

switch ($Action) {
    'start' {
        if (-not (Test-Path "$data\PG_VERSION")) {
            # dev-only credentials, matching .env.example (cdi/cdi)
            $pw = Join-Path $root '.tools\pwfile.tmp'
            Set-Content -Path $pw -Value 'cdi' -NoNewline -Encoding ascii
            & "$bin\initdb.exe" -D $data -U cdi --pwfile=$pw -A scram-sha-256 -E UTF8 --locale=C | Out-Null
            Remove-Item $pw
        }
        & "$bin\pg_ctl.exe" -D $data -l $log -o "-p 5432 -c listen_addresses=127.0.0.1" -w start
        $env:PGPASSWORD = 'cdi'
        $exists = & "$bin\psql.exe" -h 127.0.0.1 -U cdi -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='cdi'"
        if ($exists -ne '1') { & "$bin\psql.exe" -h 127.0.0.1 -U cdi -d postgres -c 'CREATE DATABASE cdi' | Out-Null }
    }
    'stop' { & "$bin\pg_ctl.exe" -D $data -m fast stop }
    'status' { & "$bin\pg_ctl.exe" -D $data status }
}
