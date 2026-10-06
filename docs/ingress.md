# Stable counter address — ingress runbook (backlog E1-S9)

RunPod gives a pod a new IP/port on every restart (ARCHITECTURE §3.3), and so a new
`https://<POD_ID>-8888.proxy.runpod.net`. A billing counter cannot chase that. From E1-S9 on,
**the address of record is one fixed hostname** (below: `emr.example.in`). The per-pod `LIVE URL`
is only for diagnosing that pod.

```
staff browser ──TLS──▶ Caddy on the ingress VM   (emr.example.in, Let's Encrypt, India region)
                          │ reverse_proxy 127.0.0.1:18888, health-checked
                          ▼
                    SSH reverse tunnel, opened BY the pod (autossh, outbound only)
                          ▼
                    web app 127.0.0.1:8888 on whichever pod / fallback host is live
```

Why this shape, per the architecture:
- **Native processes, no Docker:** Caddy runs as a systemd service on the VM; autossh runs as a
  process on the pod.
- **Patient data is decrypted only on hosts we run (§12):** TLS ends on our own VM in India, the
  same residency rule as E1-S10. No CDN or tunnel service sits in the data path.
- **Works on RunPod:** the pod needs no inbound port and no public IP. It dials out over SSH.
- **Replacing the pod changes nothing staff see:** the new pod opens the same tunnel. A dead pod's
  forward is dropped within about 45 s (sshd `ClientAlive*`). The new pod's autossh retries until
  it gets the port.

| Piece | File |
|---|---|
| Caddy site (TLS, allowlist, health check, 503 page) | `infra/ingress/Caddyfile` |
| VM setup, idempotent | `infra/ingress/setup_ingress_vm.sh` |
| VM settings template | `infra/ingress/cdi-ingress.env.example` |
| Pinned Caddy release + sha512 | `infra/ingress/versions.env` |
| Pod tunnel (run by `start_all.sh`) | `infra/runpod/start_tunnel.sh` |
| Which layer is failing? | `scripts/check_ingress.py` |

## One-time setup

1. **VM.** A small Ubuntu 24.04 VM in an India region (1 vCPU / 1 GB is enough; it only relays
   traffic). It needs a static public IP. Open ports 22, 80 and 443 in the provider's firewall.
2. **DNS.** Create an `A` record `emr.example.in → <VM public IP>` at your domain's DNS provider.
   Use your real domain everywhere this runbook says `emr.example.in`.
3. **First pod boot.** Set `CDI_INGRESS_HOSTNAME=emr.example.in` and
   `CDI_INGRESS_SSH=cdi-tunnel@<VM IP or name>` in `/workspace/cdi/.env`, then run
   `start_all.sh`. It prints the new tunnel public key (`ssh-ed25519 … cdi-pod-tunnel`). The tunnel
   can't connect yet, which is expected at this point.
4. **Configure the VM:**
   ```bash
   sudo cp infra/ingress/cdi-ingress.env.example /etc/cdi-ingress.env && sudo chmod 600 /etc/cdi-ingress.env
   sudo nano /etc/cdi-ingress.env      # hostname, ACME e-mail, clinic CIDRs, the pod key from step 3
   sudo bash infra/ingress/setup_ingress_vm.sh /etc/cdi-ingress.env
   ```
   It prints `CDI_INGRESS_HOST_KEY="ssh-ed25519 …"`.
5. **Pin the VM's host key on the pod:** put that line in `/workspace/cdi/.env` and run
   `start_all.sh` again. It ends with
   `COUNTER URL: https://emr.example.in   (address of record …)`.
6. **Point staff at `https://emr.example.in`** and bookmark it. Don't use the `proxy.runpod.net` URL.

`CDI_INGRESS_ALLOW` is required, and it must not be `0.0.0.0/0`. Staff sign in (E11-S1), and the
UI is still only reachable from the clinic's networks, as a second layer in front of patient data. `/healthz` is the one path open to everyone:
monitors need it, and it returns component booleans only.

## Every day / after a restart

Nothing to do: `start_all.sh` restarts the tunnel. It is idempotent, and a running tunnel is left
alone. Check from anywhere:

```bash
python scripts/check_ingress.py https://emr.example.in
```

| Output | Meaning | Look at |
|---|---|---|
| `UP` (exit 0) | Everything is serving | nothing |
| `DEGRADED` (exit 2) | The web app answers, but db / s3 / mlserve is down | the pod: `/workspace/logs/*.log` |
| `BACKEND_DOWN` (exit 3) | The VM is fine, but no pod is behind it | Pod stopped? `start_all.sh`. Tunnel: `/workspace/logs/tunnel.log` |
| `UNREACHABLE` (exit 4) | The ingress itself is not answering | VM, DNS, certificate: `journalctl -u caddy` |

When no backend is serving, staff see a plain **503** page starting `CDI_BACKEND_DOWN`, with
header `X-CDI-Ingress: backend-down`. It is never an HTTP 200 error page like RunPod's fake 502
(§10.4). Caddy checks the public sign-in page `/login` for the page marker `Agentic EMR generation` every 10 s, and marks the
backend down on the first failed connection.

## Replacing the pod (or moving to the fallback host, E1-S3)

1. Copy `/workspace/secrets/tunnel/` to the new pod or host, and `.env` along with it. If you don't,
   the new host makes a new key: add that key to `CDI_INGRESS_POD_KEYS` and re-run the VM setup.
2. Run `start_all.sh` on the new host. Within about a minute of the old one going away, the same
   `https://emr.example.in` serves from the new host.

## Changing things

- **New clinic network or new pod key:** edit `/etc/cdi-ingress.env` and re-run
  `setup_ingress_vm.sh`. It validates the Caddy config before reloading.
- **Upgrade Caddy:** set `CADDY_VERSION` and `CADDY_SHA512` in `infra/ingress/versions.env`. Take the
  checksum from `caddy_<version>_checksums.txt` of that release. Then re-run the setup.

## Acceptance test (E1-S9)

Run it from a clinic machine:
1. `check_ingress.py https://emr.example.in` → `UP`.
2. Stop the web app on the pod (`pkill -f cdi_adapter.webapp`). Within 10 s the check shows
   `BACKEND_DOWN`, and the browser shows the `CDI_BACKEND_DOWN` 503 page.
3. Run `start_all.sh` again → `UP` at the **same** URL.
4. Replace the backend: stop the pod and start a new one (or the fallback host) with the same
   `/workspace/secrets/tunnel/`, then run `start_all.sh` → `UP` at the **same** URL.
