"""S7 longitudinal reconciliation across encounters (backlog E5-S1): medication continuity and
the problem list. ``reconcile_patient`` runs both; the S6 gate calls it after every pass and the
Reviewer calls the half its decided fact belongs to.

**Medications.** A medication reordered or changed at a later encounter supersedes the earlier
order.
``reconcile_patient_medications(conn, patient_id)`` derives the patient's supersession links from
scratch (idempotent, so it runs after every S6 gate pass and every reviewer decision):

* only **governed**, **current** medication orders take part (``auto_accepted`` /
  ``clinician_confirmed`` / ``corrected``): a held fact never removes a verified order;
* the **same medication** is the same bound code **or** the same normalized medicine name (the
  catalogue's name key: the name without form words and strength) - either way round, because
  terminology binding is probabilistic: the same medicine is routinely coded at one encounter and
  left uncoded at the next, and matching on the code alone would leave both in the active list;
* orders are placed on their **encounter date**; an order without one is left alone. The orders of
  a later date supersede those of the latest earlier date - so an older document uploaded
  afterwards never supersedes a newer order - and orders of one date never supersede each other;
* **nothing is deleted** from ``clinical_fact``: the earlier order stays, in
  ``v_medication_history``, linked in ``medication_supersession``; ``v_medication_list`` (the
  active list) shows the latest order only. Each new link is written to ``audit_log``.

**Problems** (migration 0023). A condition is not reordered, it is *mentioned again*: the same
problem recorded at three encounters is one line on the list, not three. A later mention
**continues** the latest earlier mention of the same problem (same code or same normalized text),
every link of one problem carries the same **chain head** - its earliest mention - and
``v_problem_list`` shows the latest mention with ``first_onset`` and the mention count, so the
clinician reads the **current** status against the problem's real start. ``v_problem_history``
keeps every mention. Governed facts only; nothing is deleted.

``clinical_fact.supersedes`` is untouched in both halves: it remains a reviewer's correction.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.catalogue.normalize import split_name

GOVERNED = ("auto_accepted", "clinician_confirmed", "corrected")
_CHANGES = (("strength_num", "strength_changed"), ("dose_num", "dose_changed"),
            ("frequency_code", "frequency_changed"),
            ("duration_days", "duration_changed"))  # fmt: skip


def match_key(order: dict[str, Any]) -> str | None:
    """What makes two orders the same medication: the bound code, else the medicine's name key."""
    if order.get("code_system") and order.get("code"):
        return f"code:{order['code_system']}|{order['code']}"
    name, _ = split_name(order.get("drug_text") or order.get("local_text"))
    return f"name:{name}" if name else None


def name_key(order: dict[str, Any]) -> str | None:
    """The medicine's name key alone, whether or not the order is coded."""
    name, _ = split_name(order.get("drug_text") or order.get("local_text"))
    return f"name:{name}" if name else None


def code_key(order: dict[str, Any]) -> str | None:
    if order.get("code_system") and order.get("code"):
        return f"code:{order['code_system']}|{order['code']}"
    return None


def same_medication(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Are these two orders the same medication?

    * **Both coded:** the codes decide. Two different codes are two different medicines, whatever
      their text looks like - a binding is the stronger signal and is not overruled by a name.
    * **Either uncoded:** the medicine's name key decides. Terminology binding is probabilistic
      (E3), so the same medicine is routinely coded at one encounter and left uncoded at the next;
      without this an order would be matched only to another order of the same coding luck, and
      both would sit in the active list as duplicates.
    """
    ca, cb = code_key(a), code_key(b)
    if ca is not None and cb is not None:
        return ca == cb
    na, nb = name_key(a), name_key(b)
    return na is not None and na == nb


def groups_of(orders: list[dict[str, Any]], same: Any) -> list[list[dict[str, Any]]]:
    """Orders joined into groups by ``same`` - transitively, so a coded order and an uncoded one
    of the same medicine end up in one group even when a third order links them."""
    parent = list(range(len(orders)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(orders)):
        for j in range(i + 1, len(orders)):
            if same(orders[i], orders[j]):
                parent[find(i)] = find(j)
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, o in enumerate(orders):
        out[find(i)].append(o)
    return list(out.values())


def group_key(group: list[dict[str, Any]]) -> str:
    """What to record a group's links under: its code if any order carries one, else its name."""
    for o in group:
        k = code_key(o)
        if k:
            return k
    for o in group:
        k = name_key(o)
        if k:
            return k
    return "name:"


def change_kind(new: dict[str, Any], old: dict[str, Any]) -> str:
    changed = [kind for col, kind in _CHANGES if _differs(new.get(col), old.get(col))]
    if not changed:
        return "reordered"
    return changed[0] if len(changed) == 1 else "changed"


def _differs(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False  # not written on one of them: not a change we can assert
    if isinstance(a, int | float) or isinstance(b, int | float):
        try:
            return abs(float(a) - float(b)) > 1e-9
        except (TypeError, ValueError):
            return str(a) != str(b)
    return str(a).strip().lower() != str(b).strip().lower()


def _orders(conn: Connection, patient_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT f.id, f.code_system, f.code, f.local_text, f.source_doc_ids, f.created_at, "
             "  e.period_start AS encounter_date, m.drug_text, m.strength_num, m.dose_num, "
             "  m.frequency_code, m.duration_days "
             "FROM clinical_fact f "
             "JOIN encounter e ON e.id = f.encounter_id "
             "LEFT JOIN medication_detail m ON m.fact_id = f.id "
             "WHERE f.patient_id = :p AND f.fact_type = 'medication' AND f.is_current "
             "  AND f.review_state IN ('auto_accepted', 'clinician_confirmed', 'corrected') "
             "  AND e.period_start IS NOT NULL"),
        {"p": str(patient_id)},
    ).mappings()  # fmt: skip
    return [dict(r) for r in rows]


def desired_links(orders: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(newer fact, superseded fact) -> link, for every medication: the orders of each encounter
    date supersede those of the latest earlier date."""
    known = [o for o in orders if match_key(o)]
    links: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups_of(known, same_medication):
        key = group_key(group)
        dates = sorted({o["encounter_date"] for o in group})
        for earlier, later in zip(dates, dates[1:], strict=False):
            for new in (o for o in group if o["encounter_date"] == later):
                for old in (o for o in group if o["encounter_date"] == earlier):
                    links[(str(new["id"]), str(old["id"]))] = {
                        "match_key": key, "change_kind": change_kind(new, old),
                        "encounter_date": later, "superseded_date": earlier,
                    }  # fmt: skip
    return links


def reconcile_patient_medications(conn: Connection, patient_id: UUID | str | None,
                                  *, actor: str = "system") -> dict[str, int]:  # fmt: skip
    """Bring the patient's supersession links in line with their governed orders."""
    if patient_id is None:
        return {"added": 0, "removed": 0}
    want = desired_links(_orders(conn, patient_id))
    have = {(str(r.fact_id), str(r.supersedes_fact_id)): r.id for r in conn.execute(
        text("SELECT id, fact_id, supersedes_fact_id FROM medication_supersession "
             "WHERE patient_id = :p"), {"p": str(patient_id)},
    )}  # fmt: skip
    stale = [have[k] for k in have if k not in want]  # an order corrected, rejected or re-dated
    if stale:
        conn.execute(text("DELETE FROM medication_supersession WHERE id = ANY(CAST(:i AS uuid[]))"),
                     {"i": [str(i) for i in stale]})  # fmt: skip
    added = 0
    for (new, old), link in want.items():
        if (new, old) in have:
            continue
        conn.execute(
            text("INSERT INTO medication_supersession (patient_id, fact_id, supersedes_fact_id, "
                 "  match_key, change_kind, encounter_date, superseded_date) "
                 "VALUES (:p, :n, :o, :k, :c, :d, :sd)"),
            {"p": str(patient_id), "n": new, "o": old, "k": link["match_key"],
             "c": link["change_kind"], "d": link["encounter_date"],
             "sd": link["superseded_date"]},
        )  # fmt: skip
        repo.write_audit(conn, actor=actor, action="update", entity="clinical_fact", entity_id=old,
                         detail={"medication_superseded_by": new, **{k: str(v) for k, v in
                                 link.items()}})  # fmt: skip
        added += 1
    return {"added": added, "removed": len(stale)}


# ------------------------------------------------------------------ the problem list (E5-S1)

_PROBLEM_CHANGES = (("clinical_status", "status_changed"),
                    ("verification", "verification_changed"))  # fmt: skip


def text_key(value: Any) -> str | None:
    """A condition's text, reduced to what two mentions of one problem share: lower case, no
    punctuation, single spaces. Unlike a medicine there is no form or strength to strip."""
    if not value:
        return None
    out = " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in str(value)).split())
    return out.lower() or None


def problem_key(fact: dict[str, Any]) -> str | None:
    """What makes two mentions the same problem: the bound code, else the normalized text."""
    k = code_key(fact)
    if k:
        return k
    t = text_key(fact.get("code_display") or fact.get("local_text"))
    return f"text:{t}" if t else None


def same_problem(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Both coded: the codes decide. Either uncoded: the normalized text does - for the same
    reason as :func:`same_medication`, a condition coded at one encounter and left uncoded at the
    next is still one problem."""
    ca, cb = code_key(a), code_key(b)
    if ca is not None and cb is not None:
        return ca == cb
    ta = text_key(a.get("code_display") or a.get("local_text"))
    tb = text_key(b.get("code_display") or b.get("local_text"))
    return ta is not None and ta == tb


def problem_change(new: dict[str, Any], old: dict[str, Any]) -> str:
    changed = [kind for col, kind in _PROBLEM_CHANGES if _differs(new.get(col), old.get(col))]
    if not changed:
        return "unchanged"
    return changed[0] if len(changed) == 1 else "changed"


def _conditions(conn: Connection, patient_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT f.id, f.code_system, f.code, f.code_display, f.local_text, f.onset, "
             "  f.clinical_status, f.verification, e.period_start AS encounter_date "
             "FROM clinical_fact f "
             "JOIN encounter e ON e.id = f.encounter_id "
             "WHERE f.patient_id = :p AND f.fact_type = 'condition' AND f.is_current "
             "  AND f.review_state IN ('auto_accepted', 'clinician_confirmed', 'corrected') "
             "  AND e.period_start IS NOT NULL"),
        {"p": str(patient_id)},
    ).mappings()  # fmt: skip
    return [dict(r) for r in rows]


def desired_problem_links(facts: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(later mention, continued mention) -> link. Each encounter date's mentions continue those
    of the latest earlier date, and every link of one problem carries the same chain head - the
    earliest mention - so the list can count the mentions and date the problem from its start."""
    known = [f for f in facts if problem_key(f)]
    links: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups_of(known, same_problem):
        head = sorted(group, key=lambda f: (f["encounter_date"], str(f["id"])))[0]
        key = problem_key(head)
        dates = sorted({f["encounter_date"] for f in group})
        for earlier, later in zip(dates, dates[1:], strict=False):
            for new in (f for f in group if f["encounter_date"] == later):
                for old in (f for f in group if f["encounter_date"] == earlier):
                    links[(str(new["id"]), str(old["id"]))] = {
                        "match_key": key or "text:", "chain_head": str(head["id"]),
                        "status_change": problem_change(new, old),
                        "encounter_date": later, "continued_date": earlier,
                    }  # fmt: skip
    return links


def reconcile_patient_problems(conn: Connection, patient_id: UUID | str | None,
                               *, actor: str = "system") -> dict[str, int]:  # fmt: skip
    """Bring the patient's problem-continuity links in line with their governed conditions. Like
    the medication side: derived from scratch, so it is idempotent and a corrected, rejected or
    re-dated mention drops its stale links."""
    if patient_id is None:
        return {"added": 0, "removed": 0}
    want = desired_problem_links(_conditions(conn, patient_id))
    have = {(str(r.fact_id), str(r.continues_fact_id)): r.id for r in conn.execute(
        text("SELECT id, fact_id, continues_fact_id FROM problem_continuity "
             "WHERE patient_id = :p"), {"p": str(patient_id)},
    )}  # fmt: skip
    stale = [have[k] for k in have if k not in want]
    if stale:
        conn.execute(text("DELETE FROM problem_continuity WHERE id = ANY(CAST(:i AS uuid[]))"),
                     {"i": [str(i) for i in stale]})  # fmt: skip
    added = 0
    for (new, old), link in want.items():
        if (new, old) in have:
            continue
        conn.execute(
            text("INSERT INTO problem_continuity (patient_id, fact_id, continues_fact_id, "
                 "  chain_head_fact_id, match_key, status_change, encounter_date, "
                 "  continued_date) VALUES (:p, :n, :o, :h, :k, :c, :d, :cd)"),
            {"p": str(patient_id), "n": new, "o": old, "h": link["chain_head"],
             "k": link["match_key"], "c": link["status_change"],
             "d": link["encounter_date"], "cd": link["continued_date"]},
        )  # fmt: skip
        detail = {"problem_continued_by": new, "status_change": link["status_change"],
                  "match_key": link["match_key"]}  # fmt: skip
        repo.write_audit(conn, actor=actor, action="update", entity="clinical_fact",
                         entity_id=old, detail=detail)  # fmt: skip
        added += 1
    return {"added": added, "removed": len(stale)}


def reconcile_patient(conn: Connection, patient_id: UUID | str | None, *,
                      actor: str = "system") -> dict[str, dict[str, int]]:  # fmt: skip
    """Both halves of S7 for one patient: medication continuity and the problem list."""
    return {"medications": reconcile_patient_medications(conn, patient_id, actor=actor),
            "problems": reconcile_patient_problems(conn, patient_id, actor=actor)}  # fmt: skip
