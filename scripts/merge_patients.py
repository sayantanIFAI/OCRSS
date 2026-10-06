"""Merge a duplicate patient record into the right one, and remove approvals made twice
(2026-09-25: one image uploaded twice was filed under a second "Rahul", and that approval was
promoted 5 times; owner: merge into the first record).

    python scripts/merge_patients.py --keep CFP-2026-000001 --drop CFP-2026-000002 \\
        [--remove-approval <rv_item id> ...] [--apply]

Without ``--apply`` it only prints the plan. With it, in ONE transaction:

1. each ``--remove-approval``: an **approved** review item of either patient, with what its
   approval wrote: the canonical ``cn_*`` rows and FHIR bundles (their foreign keys do not
   cascade), then the item (its elements, actions and AI-verification chain cascade);
2. every row that points at the dropped patient (every foreign key to ``patient_identity``, read
   from the database's own catalog, so a new table is never missed) is re-pointed to the kept one;
3. the dropped patient record is deleted, and the merge is written to ``audit_log``.

A clinical record is never edited by hand in the database: this script is the checked way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import Connection, text  # noqa: E402

FK_TO = """
SELECT cl.relname AS tbl, a.attname AS col, c.confdeltype AS on_delete
FROM pg_constraint c
JOIN pg_class cl ON cl.oid = c.conrelid
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
WHERE c.contype = 'f' AND c.confrelid = CAST(:t AS regclass)
ORDER BY 1, 2
"""


def references(conn: Connection, table: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(FK_TO), {"t": table}).mappings()]


def _patient(conn: Connection, mpi_id: str) -> Any:
    pid = conn.execute(text("SELECT id FROM patient_identity WHERE mpi_id = :m"),
                       {"m": mpi_id}).scalar_one_or_none()  # fmt: skip
    if pid is None:
        raise SystemExit(f"no patient {mpi_id}")
    return pid


def merge(conn: Connection, keep_mpi: str, drop_mpi: str, remove: list[str], *,
          actor: str = "merge_patients") -> dict[str, Any]:  # fmt: skip
    """Do the merge on ``conn`` (the caller commits or rolls back); return what was done."""
    from cdi_adapter import repo

    if keep_mpi == drop_mpi:
        raise SystemExit("--keep and --drop are the same record")
    keep, drop = _patient(conn, keep_mpi), _patient(conn, drop_mpi)
    plan: dict[str, Any] = {"keep": keep_mpi, "drop": drop_mpi, "removed_approvals": {},
                            "repointed": {}}  # fmt: skip
    for item in remove:
        row = conn.execute(text("SELECT status, patient_id FROM rv_item WHERE id = :i"),
                           {"i": item}).mappings().first()  # fmt: skip
        if row is None:
            raise SystemExit(f"no review item {item}")
        if row["status"] != "approved" or row["patient_id"] not in (keep, drop):
            raise SystemExit(f"review item {item} is {row['status']} and not an approval of "
                             f"{keep_mpi} / {drop_mpi}: refused")  # fmt: skip
        removed: dict[str, int] = {}
        for ref in references(conn, "rv_item"):
            if ref["on_delete"] == "c":  # cascades with the item
                continue
            n = conn.execute(text(f'DELETE FROM "{ref["tbl"]}" WHERE "{ref["col"]}" = :i'),
                             {"i": item}).rowcount  # fmt: skip
            if n:
                removed[ref["tbl"]] = n
        conn.execute(text("DELETE FROM rv_item WHERE id = :i"), {"i": item})
        plan["removed_approvals"][item] = removed
    for ref in references(conn, "patient_identity"):
        n = conn.execute(
            text(f'UPDATE "{ref["tbl"]}" SET "{ref["col"]}" = :k WHERE "{ref["col"]}" = :d'),
            {"k": keep, "d": drop},
        ).rowcount
        if n:
            plan["repointed"][f"{ref['tbl']}.{ref['col']}"] = n
    conn.execute(text("DELETE FROM patient_identity WHERE id = :d"), {"d": drop})
    repo.write_audit(conn, actor=actor, action="update", entity="patient_identity",
                     entity_id=keep, detail={"merged": drop_mpi, **plan})  # fmt: skip
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", required=True, help="the record that stays (mpi_id)")
    ap.add_argument("--drop", required=True, help="the duplicate record merged into it (mpi_id)")
    ap.add_argument("--remove-approval", action="append", default=[], metavar="RV_ITEM_ID")
    ap.add_argument("--apply", action="store_true", help="do it (default: print the plan only)")
    args = ap.parse_args(argv)

    from cdi_adapter.db import get_engine

    with get_engine().connect() as conn:
        tx = conn.begin()
        try:
            plan = merge(conn, args.keep, args.drop, args.remove_approval)
        except BaseException:
            tx.rollback()
            raise
        if args.apply:
            tx.commit()
        else:
            tx.rollback()
    print(("APPLIED" if args.apply else "PLAN (nothing changed; add --apply)"), plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
