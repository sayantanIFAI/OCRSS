"""E5-S2 on real PostgreSQL: the derived patient summary is current without a refresh step.

The acceptance is about *when*, not only what: the summary must be up to date "within the same
job's live-reprojection window as a new governed fact lands - no separate manual refresh step".
So these tests never call the refresh themselves except where they say so: they run the pipeline,
or a Reviewer decision, and then read the summary.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cdi_adapter import summary
from cdi_adapter.db import session_scope
from cdi_adapter.webapp.review import ReviewDecision, decide
from tests.test_longitudinal_integration import _name, _visit

pytestmark = pytest.mark.integration


def _summary(patient: str) -> dict:
    with session_scope() as c:
        out = summary.patient_summary(c, patient)
    assert out is not None, "the job left no summary for this patient"
    return out


def _stale() -> list[str]:
    with session_scope() as c:
        return summary.stale_patients(c)


def _held_or_governed(patient: str, fact_type: str) -> dict | None:
    with session_scope() as c:
        row = c.execute(text("SELECT id, review_state, patient_id, fact_type FROM clinical_fact "
                             "WHERE patient_id = :p AND fact_type = :t AND is_current "
                             "ORDER BY created_at LIMIT 1"),
                        {"p": patient, "t": fact_type}).mappings().first()  # fmt: skip
    return dict(row) if row else None


# ------------------------------------------------------------------ the acceptance


def test_the_summary_is_current_when_the_job_ends_with_no_refresh_step(db_env):
    """One visit through S4 -> S6. Nothing calls a refresh: the gate did it in its own
    transaction, so the summary exists and carries the patient's lists already."""
    name = _name()
    _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days",
                                "Tab Atorvastatin 10 mg 0-0-1"])  # fmt: skip
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])

    out = _summary(patient)
    assert out["stale"] is False  # nothing drifted: the refresh followed the facts
    assert out["refreshed_at"] is not None
    meds = [m["medication"] for m in out["medications"]]
    assert any("mlodipine" in m for m in meds), meds
    assert out["medication_count"] == len(out["medications"]) > 0
    assert out["problem_count"] == len(out["problems"])
    assert any("ypertension" in (p["problem"] or "") for p in out["problems"]), out["problems"]
    assert str(out["last_encounter_date"]) == "2026-01-05"
    assert out["patient_id"] and out["name"]
    assert _stale() == []


def test_a_later_visit_updates_the_summary_in_the_same_window(db_env):
    """A second visit's governed facts land, and the summary moves with them - the active order
    is the new one, and the problem is still one line (E5-S1 continuity, inherited)."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    before = _summary(patient)
    assert [m["strength_num"] for m in before["medications"]] == [5]

    _visit(name, "05/04/2026", ["Tab Amlodipine 10 mg OD x 30 days"])
    after = _summary(patient)
    assert [m["strength_num"] for m in after["medications"]] == [10]  # the superseded order is gone
    assert after["medication_count"] == 1  # not two amlodipines
    assert len([p for p in after["problems"] if "ypertension" in (p["problem"] or "")]) == 1
    assert after["problems"][0]["mentions"] >= 2  # one line, with its history behind it
    assert str(after["last_encounter_date"]) == "2026-04-05"
    assert after["refreshed_at"] > before["refreshed_at"]
    assert _stale() == []


def test_a_reviewer_decision_updates_the_summary_too(db_env):
    """The other way a governed fact lands: a human accepting a held one."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    fact = _held_or_governed(patient, "medication")
    assert fact is not None
    before = _summary(patient)

    with session_scope() as c:
        decide(c, fact["id"], ReviewDecision(action="accept", note="E5-S2 test"),
               reviewer="reviewer-1")  # fmt: skip
    after = _summary(patient)
    assert after["refreshed_at"] >= before["refreshed_at"]
    assert after["stale"] is False and _stale() == []


# ------------------------------------------------------------------ what it holds


def test_only_governed_facts_reach_the_summary(db_env):
    """A held fact is not part of the record yet, so it is not in the patient's summary - the
    same rule E5-S1 applies to supersession."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    with session_scope() as c:
        held = c.execute(text(
            "UPDATE clinical_fact SET review_state = 'in_review' WHERE patient_id = :p "
            "AND fact_type = 'medication' AND is_current RETURNING id"),
            {"p": patient}).scalars().all()  # fmt: skip
        assert held
        summary.refresh_patient_summary(c, patient)
    out = _summary(patient)
    assert out["medications"] == [] and out["medication_count"] == 0


def test_a_patient_with_nothing_governed_has_an_empty_summary_not_a_missing_one(db_env):
    with session_scope() as c:
        pid = c.execute(text("INSERT INTO patient_identity (name, sex) VALUES ('Empty Das', 'F') "
                             "RETURNING id")).scalar_one()  # fmt: skip
        summary.refresh_patient_summary(c, pid)
    out = _summary(str(pid))
    assert (out["problems"], out["medications"], out["allergies"]) == ([], [], [])
    assert out["problem_count"] == out["medication_count"] == out["allergy_count"] == 0
    assert out["last_encounter_date"] is None and out["stale"] is False


def test_the_refresh_is_idempotent_and_the_lists_are_stable(db_env):
    """Refreshing twice must not look like a change: the lists are ordered, so a reader diffing
    two summaries sees only real movement."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days",
                                             "Tab Atorvastatin 10 mg 0-0-1"])  # fmt: skip
    first = _summary(patient)
    with session_scope() as c:
        summary.refresh_patient_summary(c, patient)
    second = _summary(patient)
    for key in ("problems", "medications", "allergies", "problem_count", "medication_count"):
        assert first[key] == second[key], key


def test_refreshing_nothing_is_not_an_error(db_env):
    with session_scope() as c:
        assert summary.refresh_patient_summary(c, None) == {"problems": 0, "medications": 0,
                                                            "allergies": 0}  # fmt: skip


# ------------------------------------------------------------------ the drift guard


def test_a_governed_fact_written_without_a_refresh_is_visible_as_stale(db_env):
    """The trigger's job: nothing depends on it for correctness, but a write path that forgets to
    refresh must not drift silently."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    assert _summary(patient)["stale"] is False
    with session_scope() as c:  # a governed fact changes behind the summary's back
        c.execute(text("UPDATE clinical_fact SET review_state = 'corrected' WHERE patient_id = :p "
                       "AND fact_type = 'medication' AND is_current"), {"p": patient})  # fmt: skip
    assert _summary(patient)["stale"] is True
    assert patient in _stale()
    with session_scope() as c:  # and the refresh clears it
        summary.refresh_patient_summary(c, patient)
    assert _summary(patient)["stale"] is False
    assert patient not in _stale()


def test_a_held_fact_changing_does_not_mark_the_summary_stale(db_env):
    """Only governed facts are in the summary, so only they can make it stale."""
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    with session_scope() as c:
        c.execute(text("INSERT INTO clinical_fact (patient_id, fact_type, local_text, "
                       "review_state) VALUES (:p, 'condition', 'Held thing', 'pending')"),
                  {"p": patient})  # fmt: skip
    assert _summary(patient)["stale"] is False


def test_rebuild_all_backfills_every_patient_with_facts(db_env):
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    with session_scope() as c:
        c.execute(text("DELETE FROM patient_summary WHERE patient_id = :p"), {"p": patient})
        assert summary.patient_summary(c, patient) is None
        n = summary.rebuild_all(c)
    assert n >= 1
    assert _summary(patient)["medication_count"] >= 1
    assert _stale() == []


def _printed(out: str) -> dict:
    """The CLI's own JSON, which the pipeline's log lines share stdout with: it is printed last,
    so the last object in the stream is the one to read."""
    import json

    i = out.rfind(chr(10) + "{")
    return json.loads(out[i + 1 :] if i >= 0 else out)


def test_the_cli_reports_drift(db_env, capsys):
    assert summary.main(["stale"]) == 0
    assert _printed(capsys.readouterr().out)["ok"] is True
    name = _name()
    _, patient = _visit(name, "05/01/2026", ["Tab Amlodipine 5 mg OD x 30 days"])
    with session_scope() as c:
        c.execute(text("UPDATE patient_summary SET stale_at = now() WHERE patient_id = :p"),
                  {"p": patient})  # fmt: skip
    assert summary.main(["stale"]) == 1  # drift is an exit code, not a log line
    out = _printed(capsys.readouterr().out)
    assert patient in out["stale"] and out["ok"] is False
    assert summary.main(["show", patient]) == 0
    assert _printed(capsys.readouterr().out)["patient_id"] == patient
    assert summary.main(["rebuild"]) == 0
    assert uuid.UUID(patient)
