"""Queue a folder of documents through the Celery pipeline (TECH_STACK §14, folder-watch path).

python scripts/ingest_batch.py /path/to/scans [--pattern "*.pdf"]
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="queue a folder of scans")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--pattern", default="*")
    args = ap.parse_args()
    from cdi_adapter.worker import submit_file

    files = sorted(p for p in args.folder.glob(args.pattern) if p.is_file())
    for f in files:
        submit_file(f.resolve(), source_channel="batch")
        print(f"queued {f}")
    print(f"{len(files)} file(s) queued")


if __name__ == "__main__":
    main()
