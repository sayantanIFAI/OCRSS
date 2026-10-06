"""Probe the stable counter address and say which layer is failing (backlog E1-S9).

    python scripts/check_ingress.py https://emr.example.in

``GET /healthz`` through the ingress (open to monitors; everything else is clinic-only) is
classified by status code AND content, never the code alone (a RunPod dead port answered HTTP 200
with a fake 502 page, ARCHITECTURE §10.4):

    exit 0  UP            web app answered {"status":"ok"}
    exit 2  DEGRADED      web app answered, a component is down ("degraded")
    exit 3  BACKEND_DOWN  the ingress answered 503 + X-CDI-Ingress: backend-down
    exit 4  UNREACHABLE   no valid answer from the ingress itself (DNS, TLS, VM down, anything else)

Stdlib only, so it runs from any clinic machine or monitor.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

EXIT = {"UP": 0, "DEGRADED": 2, "BACKEND_DOWN": 3, "UNREACHABLE": 4}
MARKER = "CDI_BACKEND_DOWN"


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str

    @property
    def code(self) -> int:
        return EXIT[self.state]


def classify(status: int | None, headers: Mapping[str, str], body: str) -> Verdict:
    lower = {k.lower(): v for k, v in headers.items()}
    if status == 503 and lower.get("x-cdi-ingress") == "backend-down" and MARKER in body:
        return Verdict("BACKEND_DOWN", "ingress up; no backend serving (tunnel, pod or web app)")
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and {"db", "s3", "mlserve"} <= data.keys():
        if status == 200 and data.get("status") == "ok":
            return Verdict("UP", "web app healthy behind the stable address")
        if status == 503 and data.get("status") == "degraded":
            down = ", ".join(k for k in ("db", "s3", "mlserve") if data.get(k) is not True)
            return Verdict("DEGRADED", f"web app reachable; down: {down}")
    shown = body.strip().replace("\n", " ")[:120]
    return Verdict("UNREACHABLE", f"unexpected answer (HTTP {status}): {shown!r}")


def probe(base_url: str, timeout: float = 10.0) -> Verdict:
    url = base_url.rstrip("/") + "/healthz"
    req = urllib.request.Request(url, headers={"User-Agent": "cdi-check-ingress"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator URL
            return classify(resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        return classify(err.code, dict(err.headers), err.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as err:
        return Verdict("UNREACHABLE", f"no answer from {url}: {err}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", help="the stable address, e.g. https://emr.example.in")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)
    verdict = probe(args.url, args.timeout)
    print(f"{verdict.state}: {verdict.detail}")
    return verdict.code


if __name__ == "__main__":
    sys.exit(main())
