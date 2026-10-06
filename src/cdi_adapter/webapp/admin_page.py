"""``GET /admin`` — read-only data inspector, server-rendered (ARCHITECTURE §4.5). No scripts."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote

from cdi_adapter.webapp.theme import page

GRAPH_SECTIONS = ("conditions", "medications", "lab_results", "vital_signs", "allergies", "bundles")


def _cell(v: Any) -> str:
    if isinstance(v, dict | list):
        v = json.dumps(v, default=str)
    return escape("" if v is None else str(v))


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class=muted>No rows.</p>"
    head = "".join(f"<th>{escape(k)}</th>" for k in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in r.values()) + "</tr>" for r in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def admin_page(counts: dict[str, int], *, table: str | None, rows: list[dict[str, Any]] | None,
               mpi: str | None, graph: dict[str, Any] | None) -> str:  # fmt: skip
    links = "".join(
        f'<div><a href="/admin?table={quote(t)}">{escape(t)}</a> <span class=muted>{n}</span></div>'
        for t, n in counts.items()
    )
    if graph is not None:
        p = graph["patient"]
        view = (
            f"<h2 style=margin-top:0>{escape(p['name'] or '')} <span class=muted>{escape(p['mpi_id'] or '')}</span></h2>"
            + "".join(f"<h3>{k.replace('_', ' ')}</h3>{_table(graph[k])}" for k in GRAPH_SECTIONS)
        )
    elif mpi:
        view = "<p>Patient not found.</p>"
    elif rows is not None:
        view = f"<h2 style=margin-top:0>{escape(table or '')}</h2>{_table(rows)}"
    elif table:
        view = "<p>Not an inspectable table.</p>"
    else:
        view = "<p class=muted>Pick a table.</p>"
    body = f"""
<h1 style="margin:0 0 4px">Data inspector</h1>
<p class=muted>Read-only view of the canonical (cn_*), operations/finance (ops_*/fin_*), AI provenance (ai_*) and review (rv_*) tables.</p>
<div class=grid style="grid-template-columns:260px 1fr">
  <section class=card><h2 style="margin-top:0">Tables</h2>{links}
    <h2>Patient graph</h2><form method=get action=/admin><input name=mpi placeholder="CareFlow id" value="{escape(mpi or "")}"> <button>Show</button></form></section>
  <section class=card>{view}</section>
</div>"""
    return page("Data inspector — CareFlow", body, active="/admin")
