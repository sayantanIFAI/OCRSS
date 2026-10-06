"""The Reviewer's "Replace" for a medicine line (owner request 2026-09-28).

The reviewer sees the products the medicine catalogue (Tata 1mg) and the medication agent offer
for one line, or searches the catalogue, and picks the one written on the prescription. The pick
goes through the same correction the Edit form makes (``review.decide``, action ``correct``): the
fact is superseded by a corrected one whose medicine is the chosen product, and the catalogue
identification of the new fact is that product, recorded as chosen by the reviewer
(method ``reviewer``). Nothing is replaced without the reviewer's click; dose, frequency and the
other details of the line stay as they are.

* ``options(conn, fact_id, q)``: ``catalogue`` (the line's match, its closest products and the
  catalogue shortlist for its reading), ``agent`` (the agent's product and the options it chose
  from) and, with ``q``, ``search`` (catalogue products for the typed name or generic).
* ``replace(conn, fact_id, ...)``: apply one choice.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.agent.medication import latest_for_document
from cdi_adapter.catalogue import service as cat
from cdi_adapter.catalogue.normalize import split_name, strength_agrees
from cdi_adapter.webapp.review import ReviewDecision, ReviewError, _approved, decide

SHOWN = 12  # products per list
SOURCES = ("catalogue", "agent", "search")
_GENERIC = re.compile(r"\s*\(generic\)$")


def _line(conn: Connection, fact_id: UUID | str) -> dict[str, Any]:
    f = conn.execute(
        text("SELECT f.*, m.drug_text, m.strength_num, m.strength_unit, m.form "
             "FROM clinical_fact f LEFT JOIN medication_detail m ON m.fact_id = f.id "
             "WHERE f.id = :i"), {"i": str(fact_id)}
    ).mappings().first()  # fmt: skip
    if f is None:
        raise ReviewError(404, "fact not found")
    if f["fact_type"] != "medication":
        raise ReviewError(422, "only a medication line can be replaced from the catalogue")
    return dict(f)


def _by_ids(conn: Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = conn.execute(text("SELECT * FROM med_catalogue WHERE id = ANY(:ids)"), {"ids": ids})
    return {r["id"]: cat._product(r) for r in rows.mappings()}


def _by_names(conn: Connection, names: list[str]) -> dict[str, dict[str, Any]]:
    if not names:
        return {}
    rows = conn.execute(
        text("SELECT DISTINCT ON (name) * FROM med_catalogue WHERE name = ANY(:n) "
             "ORDER BY name, discontinued, id"), {"n": names}
    ).mappings()  # fmt: skip
    return {r["name"]: cat._product(r) for r in rows}


def _option(p: dict[str, Any], why: str, strengths: tuple[float, ...]) -> dict[str, Any]:
    """One product the reviewer may pick; a generic (no catalogue id) is picked by its name."""
    if p.get("id") is None:
        return {"id": None, "generic": _GENERIC.sub("", p.get("name") or ""),
                "name": p.get("name"), "manufacturer": None, "composition": None,
                "dosage_form": None, "pack_size_label": None, "discontinued": False,
                "strength_match": None, "why": why}  # fmt: skip
    return {"id": p["id"], "generic": None, "name": p["name"],
            "manufacturer": p.get("manufacturer"), "composition": p.get("composition"),
            "dosage_form": p.get("dosage_form"), "pack_size_label": p.get("pack_size_label"),
            "discontinued": bool(p.get("discontinued")),
            "strength_match": strength_agrees(strengths, p.get("strengths") or []),
            "why": why}  # fmt: skip


def _key(o: dict[str, Any]) -> Any:
    return o["id"] if o["id"] is not None else "g:" + (o["generic"] or "").lower()


def _add(out: list[dict[str, Any]], opt: dict[str, Any]) -> None:
    if len(out) < SHOWN and all(_key(o) != _key(opt) for o in out):
        out.append(opt)


def _agent_for(conn: Connection, f: dict[str, Any]) -> dict[str, Any] | None:
    docs = [str(d) for d in f.get("source_doc_ids") or []]
    if not docs:
        return None
    by_fact = latest_for_document(conn, docs[0])["by_fact"]
    # the agent's suggestion, also for the fact a correction replaced (as the Reviewer shows it)
    return by_fact.get(str(f["id"])) or by_fact.get(str(f.get("supersedes") or ""))


def options(conn: Connection, fact_id: UUID | str, q: str | None = None) -> dict[str, Any]:
    f = _line(conn, fact_id)
    written = f.get("drug_text") or f.get("local_text") or ""
    strengths, form = cat.fact_strengths(f), cat.fact_form(f)
    catalogue_opts: list[dict[str, Any]] = []
    agent_opts: list[dict[str, Any]] = []
    search_opts: list[dict[str, Any]] = []
    empty = cat.catalogue_size(conn) == 0
    match = cat.matches_for_facts(conn, [str(f["id"])]).get(str(f["id"]))
    if match and not empty:
        cands = match.get("candidates") or []
        found = _by_ids(conn, [match["catalogue_id"]] if match.get("catalogue_id") else []
                        ) | _by_ids(conn, [c["id"] for c in cands if c.get("id")])  # fmt: skip
        if match.get("status") == "matched":
            mp = found.get(match.get("catalogue_id") or 0)
            _add(catalogue_opts, _option(mp or {"id": None, "name": match.get("display_name")},
                                         "catalogue match", strengths))  # fmt: skip
        for c in cands:
            cp = found.get(c["id"]) if c.get("id") else c
            if cp:
                _add(catalogue_opts, _option(cp, "closest in catalogue", strengths))
    if not empty and written.strip():
        for p in cat.shortlist(conn, names=[written], generics=[], strengths=strengths,
                               form=form, limit=SHOWN):  # fmt: skip
            _add(catalogue_opts, _option(p, "catalogue shortlist", strengths))
    agent = _agent_for(conn, f)
    if agent and not empty:
        prod = agent.get("product") or {}
        label = {"suggested": "agent suggests", "confirmed": "agent confirmed",
                 "vetoed": "agent unsure"}.get(str(agent.get("status")), "agent")  # fmt: skip
        named = _by_names(conn, list(agent.get("options") or [])
                          + list(agent.get("knowledge_options") or []))  # fmt: skip
        # the options the recall step led to (the model's knowledge + the diagnoses) come first
        known = list(agent.get("knowledge_options") or [])
        reason = ((agent.get("recalled") or {}).get("reason") or "").strip()
        why = "agent: from its knowledge and the diagnoses" + (f" ({reason})" if reason else "")
        if prod.get("name") in known:  # the suggestion is also what the model's knowledge named
            label += " · also from its knowledge and the diagnoses" + (
                f" ({reason})" if reason else "")  # fmt: skip
        if prod.get("id"):
            ap = _by_ids(conn, [prod["id"]]).get(prod["id"])
            if ap:
                _add(agent_opts, _option(ap, label, strengths))
        elif prod.get("name"):  # a generic the agent confirmed
            _add(agent_opts, _option({"id": None, "name": prod["name"]}, label, strengths))
        for name in known + [n for n in agent.get("options") or [] if n not in known]:
            if name in named:
                _add(agent_opts, _option(named[name], why if name in known else "agent's options",
                                         strengths))  # fmt: skip
    if q and q.strip() and not empty:
        for p in cat.shortlist(conn, names=[q.strip()], generics=[q.strip()],
                               strengths=(), form=None, limit=SHOWN):  # fmt: skip
            _add(search_opts, _option(p, "search", strengths))
    return {"fact_id": str(f["id"]), "written": written, "catalogue_empty": empty,
            "current": match, "catalogue": catalogue_opts, "agent": agent_opts,
            "search": search_opts}  # fmt: skip


def replace(conn: Connection, fact_id: UUID | str, *, catalogue_id: int | None,
            generic: str | None, source: str, reviewer: str,
            note: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Replace the line's medicine with one catalogue product (or catalogue generic)."""
    if source not in SOURCES:
        raise ReviewError(422, f"source must be one of {list(SOURCES)}")
    if (catalogue_id is None) == (generic is None or not generic.strip()):
        raise ReviewError(422, "choose exactly one: a catalogue product or a generic")
    f = _line(conn, fact_id)
    if catalogue_id is not None:
        product = _by_ids(conn, [catalogue_id]).get(catalogue_id)
        if product is None:
            raise ReviewError(404, "catalogue product not found")
        name = product["name"]
    else:
        key = split_name(generic)[0]
        generics = cat._generics(conn)
        if key not in generics:
            raise ReviewError(422, "not a generic of the catalogue")
        product, name = None, generics[key]
    before = f.get("drug_text") or f.get("local_text")
    why = note or f"replaced with {name} ({source})"
    if (before or "").strip() == name:
        # already written as this product: nothing to correct, the choice pins the identification
        if f["review_state"] == "rejected" or not f["is_current"]:
            raise ReviewError(409, f"fact is {f['review_state']}; already adjudicated")
        if _approved(conn, f):
            raise ReviewError(409, "its review item is approved: the fact is in the canonical "
                                   "EMR and can no longer be corrected here")  # fmt: skip
        result = {"fact_id": str(f["id"]), "result_fact_id": str(f["id"]),
                  "review_state": f["review_state"], "tasks_closed": 0}  # fmt: skip
    else:
        result = decide(conn, f["id"], ReviewDecision(action="correct",
                                                      corrections={"drug_text": name},
                                                      note=why), reviewer)  # fmt: skip
    new_id = result["result_fact_id"]
    if product is not None:
        prior = cat.matches_for_facts(conn, [new_id]).get(new_id) or {}
        imp = cat.latest_import(conn)
        cat.save_identification(conn, new_id, cat.Identification(
            "matched", name, product["id"], 1.0, "reviewer",
            strength_agrees(cat.fact_strengths(f), product["strengths"]), product,
            prior.get("candidates") or [], before or ""), imp["id"] if imp else None)  # fmt: skip
    item = conn.execute(
        text("SELECT id FROM rv_item WHERE source_document_id = ANY(:d) "
             "AND status IN ('workbasket', 'claimed') ORDER BY created_at DESC LIMIT 1"),
        {"d": [str(x) for x in f.get("source_doc_ids") or []]},
    ).scalar_one_or_none()  # fmt: skip
    if item is not None:
        from cdi_adapter.canon.repo import add_action

        add_action(conn, item, reviewer, "correct",
                   {"replace_medication": True, "fact_id": str(f["id"]),
                    "result_fact_id": new_id, "from": before, "to": name,
                    "catalogue_id": catalogue_id, "source": source})  # fmt: skip
    return result | {"medicine": name, "catalogue_id": catalogue_id, "source": source}
