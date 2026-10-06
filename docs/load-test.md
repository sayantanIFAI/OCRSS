# Load test: 30 patients × 5–10 documents (backlog E1-S7, ADR-0002)

The harness measures the counter's end-to-end latency during the design burst from ADR-0002:
**30 patients arrive at once, each with 5–10 documents.** The SLA is 5 minutes from the last upload
to governed JSON/FHIR, or to an explicit hold with an ETA.

| Piece | File |
|---|---|
| Runner (defaults: 30 patients, 5–10 docs, SLA 300 s) | `scripts/load_test.py` |
| Locust scenario, one user = one patient | `scripts/loadtest/locustfile.py` |
| Synthetic patients (no real data, §11) | `scripts/loadtest/synthetic.py` |
| Percentiles, histogram, SLA verdict | `scripts/loadtest/report.py` |

It is a dev/test tool (TECH_STACK: "k6 / Locust load harness"). Install it on the machine that
generates load, never on the pod: `pip install -e ".[load]"`.

## Run

```bash
python scripts/load_test.py --host https://emr.example.in            # the full burst
python scripts/load_test.py --host http://127.0.0.1:8888 --patients 3 --docs-max 5   # a quick check
```

Behind the E1-S9 ingress, the UI accepts only clinic networks, so run the test from an allowlisted
machine.

Each patient does what the counter does (§7.1/§7.2):
1. `POST /api/jobs` with all of its files.
2. Poll `GET /api/jobs/{id}` until the job leaves `running`.
3. `GET /api/jobs/{id}/fhir`.

All patients are spawned together, and the run stops by itself when the last job finishes.
Documents are generated before the clock starts, and the same `--seed` gives byte-identical
documents, so two runs are comparable.

## What counts as ready

| Job outcome | Counted as |
|---|---|
| `review`, no failed document, bundles returned | **ready**: the explicit hold of the SLA (facts wait for the reviewer, §7.2) |
| `review` with failed documents | not ready (a failed document is neither governed nor held) |
| every document failed | `documents_failed` |
| `mismatch` | not ready: the identity guard (§5); the time to that decision is recorded |
| `error`, `timeout` (default 30 min), any failed request | not ready |

**The SLA is met** only if every patient is ready **and** the p95 latency is within `--sla-s`.

## Output

`load_report.txt` / `load_report.json` (prefix `--report`):
- exact nearest-rank **p50 / p95 / p99** and max, from the end of the upload to the bundles being
  ready
- a histogram in 30 s bins, with bins past the SLA marked `!`
- counts by outcome, failed documents, and upload time
- every sample, including the first error of each failed job

Locust's own summary table is printed as well (`E2E upload -> bundle-ready` row); its percentiles
are rounded, so the report's figures are the ones of record.

Exit code 0 means every patient reached a ready state with document bundles. Any functional
failure (including timeout, login failure, mismatch, failed documents, or empty bundles) exits 1.
With `--enforce-sla`, a latency threshold miss also exits 1.

## Expected result today

ADR-0002 and infra backlog §2.1 give the honest limit: one CDI GPU at about 21 s per document,
serial, cannot clear 300 documents in 5 minutes. **Expect `SLA NOT MET` until E2-S9 (CPU-first
routing) and batching land.** The value of this harness is the measured number, recorded in
*Sizing & Capacity* (E13-S1) and re-run by E9-S6 and E14-S5.
