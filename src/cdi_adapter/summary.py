"""The derived patient summary (backlog E5-S2; migration 0026, §4.1, §8.1).

One row per patient - the current problem list, the active medications and the allergies - built
**from the §4.1 read-model views**, so it inherits E5-S1's continuity without repeating it: the
active order of each medicine (a superseded order is history) and one line per problem (the latest
mention, dated from its first).

:func:`refresh_patient_summary` is called on the same connection, inside the same transaction, as
the governed fact that changed it:

* the S6 gate, right after it reconciles the patient (``validate/service.py``), so by the time the
  job projects FHIR in its live-reprojection window the summary is already current;
* each Reviewer decision (``webapp/review.py``), for the same reason.

There is **no refresh step to run**. ``REFRESH MATERIALIZED VIEW`` could not have done this -
it rebuilds every patient under an ``AccessExclusiveLock``, and the ``CONCURRENTLY`` form cannot
run in a transaction at all - which is why the summary is a derived table; migration 0026 has the
reasoning. A trigger marks a patient's row ``stale_at`` whenever a governed fact changes, so a
write path that ever forgets to refresh is visible (:func:`stale_patients`) rather than silent;
nothing depends on it for correctness.

Backfill or repair with::

    python -m cdi_adapter.summary rebuild        # every patient that has facts
    python -m cdi_adapter.summary stale          # what drifted, if anything
"""

from __future__ import annotations

import json
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.db import session_scope
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

# only governed facts reach the summary: a held fact is not part of the patient's record yet
GOVERNED = ("auto_accepted", "clinician_confirmed", "corrected")

# Built from the read-model views, one aggregate per list. jsonb_agg returns NULL for no rows,
# hence the coalesce; the ORDER BY inside each aggregate keeps the lists stable between refreshes
# (a summary that only reorders itself must not look changed).
_REFRESH = """
WITH prob AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'fact_id', fact_id, 'problem', problem, 'code_system', code_system, 'code', code,
               'clinical_status', clinical_status, 'verification', verification,
               'first_onset', first_onset, 'mentions', mentions,
               'last_seen', encounter_date, 'review_state', review_state)
           ORDER BY first_onset NULLS LAST, problem), '[]'::jsonb) AS rows
    FROM v_problem_list WHERE patient_id = :p AND review_state = ANY(:g)
), med AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'fact_id', fact_id, 'medication', medication, 'code_system', code_system,
               'code', code, 'strength_num', strength_num, 'strength_unit', strength_unit,
               'dose_num', dose_num, 'dose_unit', dose_unit_ucum, 'route', route,
               'frequency_code', frequency_code, 'frequency_per_day', frequency_per_day,
               'duration_days', duration_days, 'prn', prn, 'instructions', instructions,
               'ordered_on', encounter_date, 'review_state', review_state)
           ORDER BY medication), '[]'::jsonb) AS rows
    FROM v_medication_list WHERE patient_id = :p AND review_state = ANY(:g)
), alg AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'fact_id', fact_id, 'substance', substance, 'code_system', code_system,
               'code', code, 'reaction', reaction, 'clinical_status', clinical_status,
               'review_state', review_state)
           ORDER BY substance), '[]'::jsonb) AS rows
    FROM v_allergy_list WHERE patient_id = :p AND review_state = ANY(:g)
), enc AS (
    SELECT max(e.period_start) AS last_date
    FROM encounter e WHERE e.patient_id = :p
)
INSERT INTO patient_summary (patient_id, problems, medications, allergies, problem_count,
    medication_count, allergy_count, last_encounter_date, stale_at, refreshed_at)
SELECT :p, prob.rows, med.rows, alg.rows,
       jsonb_array_length(prob.rows), jsonb_array_length(med.rows), jsonb_array_length(alg.rows),
       enc.last_date, NULL, now()
FROM prob, med, alg, enc
ON CONFLICT (patient_id) DO UPDATE SET
    problems = EXCLUDED.problems, medications = EXCLUDED.medications,
    allergies = EXCLUDED.allergies, problem_count = EXCLUDED.problem_count,
    medication_count = EXCLUDED.medication_count, allergy_count = EXCLUDED.allergy_count,
    last_encounter_date = EXCLUDED.last_encounter_date, stale_at = NULL, refreshed_at = now()
RETURNING problem_count, medication_count, allergy_count
"""


def refresh_patient_summary(conn: Connection, patient_id: UUID | str | None) -> dict[str, int]:
    """Rebuild one patient's summary on this connection, in the caller's transaction.

    Cheap enough to run on every governed fact change: three aggregates over that patient's rows,
    never a whole-table rebuild. Returns the counts it wrote (zeros when there is no patient).
    """
    if patient_id is None:
        return {"problems": 0, "medications": 0, "allergies": 0}
    row = conn.execute(text(_REFRESH), {"p": str(patient_id), "g": list(GOVERNED)}).first()
    out = {"problems": row[0], "medications": row[1], "allergies": row[2]} if row else {
        "problems": 0, "medications": 0, "allergies": 0}  # fmt: skip
    log.debug("patient_summary_refreshed", patient_id=str(patient_id), **out)
    return out


def patient_summary(conn: Connection, patient_id: UUID | str) -> dict[str, Any] | None:
    """The stored summary as the reader sees it (``v_patient_summary``)."""
    row = conn.execute(text("SELECT * FROM v_patient_summary WHERE patient_id = :p"),
                       {"p": str(patient_id)}).mappings().first()  # fmt: skip
    return dict(row) if row else None


def stale_patients(conn: Connection, limit: int = 100) -> list[str]:
    """Patients whose summary a governed fact changed without a refresh following it. Empty is the
    invariant; anything here is a write path that forgot, not a normal state."""
    return [str(r) for r in conn.execute(
        text("SELECT patient_id FROM patient_summary WHERE stale_at IS NOT NULL "
             "ORDER BY stale_at LIMIT :n"), {"n": limit}).scalars()]  # fmt: skip


def patients_with_facts(conn: Connection) -> list[str]:
    return [str(r) for r in conn.execute(
        text("SELECT DISTINCT patient_id FROM clinical_fact "
             "WHERE patient_id IS NOT NULL AND is_current AND review_state = ANY(:g)"),
        {"g": list(GOVERNED)}).scalars()]  # fmt: skip


def rebuild_all(conn: Connection) -> int:
    """Backfill (after the migration) or repair every patient that has governed facts."""
    ids = patients_with_facts(conn)
    for pid in ids:
        refresh_patient_summary(conn, pid)
    log.info("patient_summary_rebuilt", patients=len(ids))
    return len(ids)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "stale"
    with session_scope() as c:
        if cmd == "rebuild":
            print(json.dumps({"patients": rebuild_all(c)}))
        elif cmd == "stale":
            stale = stale_patients(c)
            print(json.dumps({"stale": stale, "ok": not stale}, indent=2))
            return 0 if not stale else 1
        elif cmd == "show" and len(args) > 1:
            print(json.dumps(patient_summary(c, args[1]), indent=2, default=str))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI
    raise SystemExit(main())
