"""``GET /admin`` — data inspector over the canonical / ops / ai / review tables (§4.5).

One server-rendered page (§7.1 lists only ``GET /admin``): ``?table=<name>`` shows rows of one
table, ``?mpi=<CareFlow id>`` shows ``patient_graph``. Read-only; table names are checked against an
allow-list built from the database catalogue. ``patient_graph`` reads ``cn_lab_result.test_code``
(the §4.5 bug fix: the column is not ``code``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import Connection, text

from cdi_adapter.db import session_scope
from cdi_adapter.webapp.admin_page import admin_page

router = APIRouter()

PREFIXES = ("cn_", "ops_", "fin_", "ai_", "rv_")
ROW_LIMIT = 50


class NotInspectable(LookupError):
    pass


def inspectable_tables(conn: Connection) -> list[str]:
    names = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    ).scalars()
    return [n for n in names if n.startswith(PREFIXES)]


def table_counts(conn: Connection) -> dict[str, int]:
    return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()
            for t in inspectable_tables(conn)}  # fmt: skip


def table_rows(conn: Connection, name: str, limit: int = ROW_LIMIT) -> list[dict[str, Any]]:
    if name not in inspectable_tables(conn):
        raise NotInspectable(name)
    rows = conn.execute(text(f'SELECT * FROM "{name}" LIMIT :n'), {"n": limit}).mappings()
    return [dict(r) for r in rows]


def patient_graph(conn: Connection, mpi_id: str) -> dict[str, Any] | None:
    p = conn.execute(text("SELECT id, mpi_id, name, abha_number, legacy_mrn FROM patient_identity "
                          "WHERE mpi_id = :m"), {"m": mpi_id}).mappings().first()  # fmt: skip
    if p is None:
        return None
    q = {
        "conditions": "SELECT code, display, clinical_status FROM cn_condition",
        "medications": "SELECT code, drug_text, strength_num, strength_unit, frequency_per_day "
                       "FROM cn_medication",
        "lab_results": "SELECT test_code, test_display, value_num, unit_ucum, abnormal_flag "
                       "FROM cn_lab_result",
        "vital_signs": "SELECT code, display, value_num, value_text, unit_ucum FROM cn_vital_sign",
        "allergies": "SELECT code, substance, reaction FROM cn_allergy",
        "bundles": "SELECT artifact_type, status, created_at FROM cn_fhir_bundle",
    }  # fmt: skip
    out: dict[str, Any] = {"patient": dict(p)}
    for key, sql in q.items():
        out[key] = [dict(r) for r in conn.execute(text(sql + " WHERE patient_id = :p"),
                                                  {"p": p["id"]}).mappings()]  # fmt: skip
    return out


@router.get("/admin", response_class=HTMLResponse)
def page(table: str | None = None, mpi: str | None = None) -> HTMLResponse:
    with session_scope() as c:
        counts = table_counts(c)
        rows, graph, status = None, None, 200
        if table:
            try:
                rows = table_rows(c, table)
            except NotInspectable:
                status = 404
        if mpi:
            graph = patient_graph(c, mpi)
            status = 404 if graph is None else status
    return HTMLResponse(admin_page(counts, table=table, rows=rows, mpi=mpi, graph=graph),
                        status_code=status)  # fmt: skip
