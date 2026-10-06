# Sign-in and access control (backlog E11-S1)

Every page and API of the web app now needs a signed-in staff account with the right role.
The architecture names no identity provider and none exists yet, so accounts are local to the app
(`web_user` in PostgreSQL). A clinic SSO (OIDC) can replace the password check later without
changing the roles or the route policy.

## Roles

| Role | Who | Can use |
|---|---|---|
| `intake` | counter / billing staff | the upload page `/`, upload a job, follow it, download its FHIR, the patient registry |
| `reviewer` | clinicians | `/review`, `/reviewer` and their APIs, a job's facts, *generate* (keep/drop/correct is a clinical decision, §7.2), documents and evidence |
| `admin` | the EMR owner / IT | everything above, plus the data inspector `/admin` |

The full table is `POLICY` in `src/cdi_adapter/webapp/auth.py`. A route without an entry is
**refused** (fail closed), and a unit test fails if any route is missing from it.

Public without sign-in:
- `/login` (the sign-in page, which also carries the health marker)
- `/healthz` (component booleans only; the E1-S9 ingress and monitors use it)

FastAPI's `/docs` and `/openapi.json` are switched off.

## First admin (once per database)

`start_all.sh` warns while there is no account. On the pod:

```bash
cd /workspace/cdi && .venv/bin/python -m cdi_adapter.webapp.users add admin1 --name "EMR Admin" --role admin
```

The password is asked for twice, hidden. When the input is piped, the first line of stdin is
used. It is never taken from the command line. Minimum length: 12 characters.

## Everyday account work

```bash
python -m cdi_adapter.webapp.users add rdas --name "R. Das" --role intake
python -m cdi_adapter.webapp.users list
python -m cdi_adapter.webapp.users passwd rdas          # new password; signs the user out everywhere
python -m cdi_adapter.webapp.users role rdas reviewer   # signs the user out everywhere
python -m cdi_adapter.webapp.users disable rdas         # immediate: live sessions stop working
python -m cdi_adapter.webapp.users enable rdas
python -m cdi_adapter.webapp.users unlock rdas          # after 5 wrong passwords (15-minute lock)
```

Every change is written to `audit_log` (entity `web_user`, actor `users-cli`).

## How a session works

**Signing in**
- `POST /login` checks the salted scrypt hash, compared in constant time. An unknown user costs the
  same time as a known one, so timing gives nothing away.
- On success it sets the cookie `cdi_session`, a random 256-bit token with the flags `HttpOnly`,
  `Secure` and `SameSite=Strict`.
- The database keeps only the token's sha256, so a database dump holds no usable session.

**Session lifetime**
- A session ends after 30 minutes idle (`CDI_SESSION_IDLE_MINUTES`) or 12 hours
  (`CDI_SESSION_MAX_HOURS`), whichever comes first.
- It also ends at sign-out, when the user is disabled, or when their password or role changes.

**Wrong passwords**
- After 5 in a row (`CDI_LOGIN_MAX_FAILURES`), the account locks for 15 minutes
  (`CDI_LOGIN_LOCKOUT_MINUTES`).
- The message never says whether it was the username or the password that was wrong.

**Requests that change data** (POST and the like)
- These must come from the same site (the `Origin` or `Referer` header), in addition to
  SameSite=Strict. This blocks cross-site request forgery.

**When access is refused**
- No session: `/api/*` returns **401**, and pages redirect to `/login?next=…`.
- Wrong role: **403**.
- After signing in, `next` only ever returns to a page on this site, never to another website.

**Audit trail**
- Sign-in, failed sign-in and sign-out are written to `audit_log` (entity `web_session`).

## Local development over plain http

The cookie is `Secure`, so browsers only send it over HTTPS. The pod is always behind TLS (the
RunPod proxy, or the E1-S9 ingress). On a laptop over `http://127.0.0.1`, set
`CDI_SESSION_COOKIE_SECURE=false` (as in `.env.example`). Never set it on the pod.

## Load test (E1-S7)

```bash
CDI_LOAD_PASSWORD='…' python scripts/load_test.py --host https://emr.example.in --user loadtest1
```

`loadtest1` is an `intake` account. Every simulated patient signs in with its own session.

## Who a decision is recorded under (backlog E7-S1)

Every clinical decision is recorded under the **signed-in user**, never a name the browser sends:

- S8 fact decisions (`POST /api/facts/{id}/review`): `fact_provenance.agent` and the audit actor.
- The `/reviewer` workbasket: claim, approve, reject and "my worklist" (`GET /api/reviewer/worklist`).
- The Generate EMR edits (`POST /api/jobs/{id}/generate`: keep, drop, correct).

A `reviewer` field in a request body or query is ignored. Only the reviewer who claimed a review
item may approve or reject it (HTTP 403 otherwise). An `admin` may decide any claimed item, for
example when its reviewer has left; that decision is recorded under the admin's own name. The review
pages no longer have a "Reviewer id" box; they show who is signed in.

## Not in this block

- The ingest API `cdi_adapter.api` (`:8080`, `POST /ingest`) is internal and bound to
  `127.0.0.1`, so it is outside §7.1 and not part of the staff UI.
