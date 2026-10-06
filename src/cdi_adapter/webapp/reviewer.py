"""``/reviewer`` workbasket -> worklist console API (ARCHITECTURE §4.5, §7.1).

| Method / path                                   | Purpose                                     |
|-------------------------------------------------|---------------------------------------------|
| GET  /api/reviewer/workbasket                   | shared queue                                |
| GET  /api/reviewer/worklist?reviewer=           | that reviewer's claimed items               |
| POST /api/reviewer/items/{id}/claim|approve|reject | move / decide an item (approve promotes)  |
| GET  /api/reviewer/items/{id}/elements          | the item's facts (current versions)         |
| GET  /api/reviewer/items/{id}/bundle            | the generated canonical FHIR bundle         |

Held facts inside an item are decided with the S8 endpoint (``POST /api/facts/{id}/review``).
The reviewer identity comes from the request until backlog E7-S1 adds sessions; the Customer-360
read model is backlog E7-S8.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from cdi_adapter.canon import promote
from cdi_adapter.canon import repo as canon
from cdi_adapter.db import session_scope
from cdi_adapter.webapp.reviewer_page import reviewer_page

router = APIRouter()


class Who(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)


def _call(fn):
    try:
        return fn()
    except promote.WorkflowError as e:
        raise HTTPException(e.status, e.detail) from e


@router.get("/reviewer", response_class=HTMLResponse)
def page() -> str:
    return reviewer_page()


@router.get("/api/reviewer/workbasket")
def workbasket() -> list[dict[str, Any]]:
    with session_scope() as c:
        return canon.list_items(c, status="workbasket")


@router.get("/api/reviewer/worklist")
def worklist(reviewer: str) -> list[dict[str, Any]]:
    with session_scope() as c:
        return canon.list_items(c, status="claimed", assignee=reviewer)


@router.post("/api/reviewer/items/{item_id}/claim")
def claim(item_id: UUID, who: Who) -> dict[str, Any]:
    with session_scope() as c:
        return _call(lambda: promote.claim(c, item_id, who.reviewer))


@router.post("/api/reviewer/items/{item_id}/approve")
def approve(item_id: UUID, who: Who) -> dict[str, Any]:
    with session_scope() as c:
        return _call(lambda: promote.promote_review_item(c, item_id, who.reviewer))


@router.post("/api/reviewer/items/{item_id}/reject")
def reject(item_id: UUID, who: Who) -> dict[str, Any]:
    with session_scope() as c:
        return _call(lambda: promote.reject_item(c, item_id, who.reviewer, who.reason))


@router.get("/api/reviewer/items/{item_id}/elements")
def item_elements(item_id: UUID) -> dict[str, Any]:
    with session_scope() as c:
        item = canon.get_item(c, item_id)
        if item is None:
            raise HTTPException(404, "review item not found")
        return {"item": item, "elements": canon.elements(c, item_id)}


@router.get("/api/reviewer/items/{item_id}/bundle")
def item_bundle(item_id: UUID) -> dict[str, Any]:
    with session_scope() as c:
        row = c.execute(text("SELECT * FROM cn_fhir_bundle WHERE rv_item_id = :i "
                             "ORDER BY created_at DESC LIMIT 1"), {"i": str(item_id)}
                        ).mappings().first()  # fmt: skip
    if row is None:
        raise HTTPException(404, "no bundle yet — approve the item first")
    return dict(row)
