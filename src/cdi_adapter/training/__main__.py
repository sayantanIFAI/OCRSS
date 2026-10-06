"""``python -m cdi_adapter.training export [--force]`` | ``versions`` (backlog E4-S1)
| ``fit [--dataset-version N] [--min-examples N] [--tolerance T] [--report FILE.html]``
| ``calibrators`` (backlog E4-S2)
| ``thresholds --calibrator-version N [--target T] [--target-numeric T] [--min-accepted N]
  [--write FILE.json]`` | ``threshold-sets`` (backlog E4-S3).

``export`` writes the next calibration-training version when the adjudicated facts changed (the
nightly cron in ``infra/runpod/bootstrap_pod.sh`` runs it); ``versions`` lists what is published.
``fit`` fits the next isotonic calibrator on a published version (default: the latest) and can
write its reliability diagrams as an HTML page; ``calibrators`` lists the fitted versions. A
calibrator is used by the gate only when ``CDI_CALIBRATOR_VERSION`` names it. ``thresholds``
derives the per-class gate cut-offs on a calibrator's data and writes the checked-in JSON config
(``CDI_GATE_THRESHOLDS``). Prints JSON; exit 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text

from cdi_adapter.db import session_scope
from cdi_adapter.training import calibrator, thresholds
from cdi_adapter.training.calibration import export_dataset


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdi_adapter.training")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="publish the next version if the data changed")
    ex.add_argument("--force", action="store_true", help="publish even when unchanged")
    ex.add_argument("--actor", default="calibration-export")
    sub.add_parser("versions", help="list the published versions")
    ft = sub.add_parser("fit", help="fit the next calibrator (E4-S2)")
    ft.add_argument("--dataset-version", type=int, default=None)
    ft.add_argument("--min-examples", type=int, default=calibrator.DEFAULT_MIN_EXAMPLES)
    ft.add_argument("--tolerance", type=float, default=calibrator.DEFAULT_TOLERANCE)
    ft.add_argument("--report", type=Path, default=None, help="write reliability diagrams (HTML)")
    ft.add_argument("--actor", default="calibrator-fit")
    sub.add_parser("calibrators", help="list the fitted calibrators")
    th = sub.add_parser("thresholds", help="derive the per-class gate thresholds (E4-S3)")
    th.add_argument("--calibrator-version", type=int, required=True)
    th.add_argument("--target", type=float, default=thresholds.DEFAULT_TARGET)
    th.add_argument("--target-numeric", type=float, default=thresholds.DEFAULT_TARGET_NUMERIC)
    th.add_argument("--min-accepted", type=int, default=thresholds.DEFAULT_MIN_ACCEPTED)
    th.add_argument("--write", type=Path, default=None, help="the JSON config to check in")
    th.add_argument("--actor", default="threshold-derive")
    sub.add_parser("threshold-sets", help="list the derived threshold sets")
    args = ap.parse_args(argv)
    if args.cmd == "export":
        out = export_dataset(actor=args.actor, force=args.force)
    elif args.cmd == "fit":
        with session_scope() as c:
            out = calibrator.fit_calibrator(c, args.dataset_version,
                                            min_examples=args.min_examples,
                                            tolerance=args.tolerance, actor=args.actor)  # fmt: skip
            if args.report:
                out["report"] = str(calibrator.write_report(c, out["version"], args.report))
    elif args.cmd == "thresholds":
        with session_scope() as c:
            out = thresholds.derive_thresholds(
                c, args.calibrator_version, target=args.target,
                target_numeric=args.target_numeric, min_accepted=args.min_accepted,
                actor=args.actor)  # fmt: skip
            if args.write:
                out["config"] = str(thresholds.write_config(c, out["version"], args.write))
    elif args.cmd == "threshold-sets":
        with session_scope() as c:
            out = {"threshold_sets": [dict(r) for r in c.execute(text(
                "SELECT version, calibrator_version, target_default, target_numeric, "
                "target_basis, summary, created_at FROM gate_threshold_set "
                "ORDER BY version")).mappings()]}  # fmt: skip
    elif args.cmd == "calibrators":
        with session_scope() as c:
            out = {"calibrators": [dict(r) for r in c.execute(text(
                "SELECT version, dataset_version, method, min_examples, tolerance, summary, "
                "created_at FROM calibration_model ORDER BY version")).mappings()]}  # fmt: skip
    else:
        with session_scope() as c:
            out = {"versions": [dict(r) for r in c.execute(text(
                "SELECT version, schema_version, row_count, content_sha256, created_by, "
                "created_at FROM calibration_dataset ORDER BY version")).mappings()]}  # fmt: skip
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
