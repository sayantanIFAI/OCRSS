"""Medicine catalogue CLI (docs/medicine-catalogue.md).

python -m cdi_adapter.catalogue import --file tata1mg_catalogue.csv [--source tata1mg]
python -m cdi_adapter.catalogue stats
python -m cdi_adapter.catalogue identify "Tab. Montina-FX" [--strength 10]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cdi_adapter.catalogue import service
from cdi_adapter.db import session_scope


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.catalogue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="load (or reload) a catalogue CSV snapshot")
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--source", default="tata1mg")
    sub.add_parser("stats", help="catalogue size and the latest import")
    idf = sub.add_parser("identify", help="identify one medicine name against the catalogue")
    idf.add_argument("text")
    idf.add_argument("--strength", type=float, action="append", default=[])
    a = ap.parse_args(argv)

    with session_scope() as c:
        if a.cmd == "import":
            out: object = service.import_csv(c, a.file, source=a.source, actor="cli")
        elif a.cmd == "stats":
            out = {"products": service.catalogue_size(c), "latest_import": service.latest_import(c)}
        else:
            out = service.identify(c, a.text, tuple(a.strength)).as_json()
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
