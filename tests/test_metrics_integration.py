"""E10-S1 on real PostgreSQL: the S6 gate counters, reviewer throughput counters and the
review-backlog query read by ``/metrics``."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from cdi_adapter import metrics
from cdi_adapter.canon.repo import create_rv_item_for_document
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_metrics_unit import value
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

HELD = ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"]


def _validated_doc():
    doc, blocks = _doc_with_ocr(_rx()[:4] + HELD)
    _write(doc, blocks)
    bind_document(doc)
    return doc


def test_gate_decisions_count_what_the_gate_decided(db_env):
    doc = _validated_doc()
    auto0 = value("cdi_gate_decisions_total", decision="auto_accepted")
    held0 = value("cdi_gate_decisions_total", decision="in_review")
    out = validate_document(doc)
    assert out["in_review"] >= 1  # the 5000 mg dose is held
    assert (
        value("cdi_gate_decisions_total", decision="auto_accepted") == auto0 + out["auto_accepted"]
    )
    assert value("cdi_gate_decisions_total", decision="in_review") == held0 + out["in_review"]


def test_reviewer_throughput_and_backlog(seeded_scans, db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    doc = _validated_doc()
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc))
    reg = CollectorRegistry()
    reg.register(metrics.ReviewBacklogCollector())  # the real query
    held_tasks = value("cdi_review_backlog", reg, queue="held_fact_tasks")
    basket = value("cdi_review_backlog", reg, queue="workbasket")
    assert held_tasks >= 1 and basket >= 1

    client = signed_in(app, "reviewer")
    claims = value("cdi_review_items_total", action="claim")
    accepts = value("cdi_review_decisions_total", action="accept")
    approves = value("cdi_review_items_total", action="approve")
    assert client.post(f"/api/reviewer/items/{item}/claim", json={}).status_code == 200
    assert value("cdi_review_items_total", action="claim") == claims + 1
    assert value("cdi_review_backlog", reg, queue="workbasket") == basket - 1
    assert value("cdi_review_backlog", reg, queue="claimed") >= 1

    held = next(e for e in client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
                if e["state"] == "held")  # fmt: skip
    r = client.post(f"/api/facts/{held['fact']['id']}/review", json={"action": "accept"})
    assert r.status_code == 200
    assert value("cdi_review_decisions_total", action="accept") == accepts + 1
    assert value("cdi_review_backlog", reg, queue="held_fact_tasks") == held_tasks - 1

    assert client.post(f"/api/reviewer/items/{item}/approve", json={}).status_code == 200
    assert value("cdi_review_items_total", action="approve") == approves + 1


def test_each_extraction_counts_as_complete_or_partial(db_env):
    """E10-S2: the input of the _partial-rate alert."""
    from cdi_adapter.extract.service import extract_document
    from cdi_adapter.ml.client import StubMLClient
    from tests.test_batch_integration import RX, MemoryStore

    complete0 = value("cdi_extractions_total", outcome="complete")
    partial0 = value("cdi_extractions_total", outcome="partial")
    out = extract_document(_doc_with_ocr(RX)[0], client=StubMLClient(), store=MemoryStore())
    key = "partial" if out["partial"] else "complete"
    assert value("cdi_extractions_total", outcome=key) == (partial0 if out["partial"]
                                                           else complete0) + 1  # fmt: skip
    other = "complete" if out["partial"] else "partial"
    assert value("cdi_extractions_total", outcome=other) == (complete0 if out["partial"]
                                                             else partial0)  # fmt: skip


def test_terminology_bindings_count_bound_and_unmapped(db_env):
    """E10-S4: the input of the unmapped-code rate."""
    bound0 = value("cdi_terminology_bindings_total", outcome="bound")
    local0 = value("cdi_terminology_bindings_total", outcome="local_only")
    doc, blocks = _doc_with_ocr(_rx()[:4] + HELD)
    _write(doc, blocks)
    out = bind_document(doc)
    assert out["facts"] >= 1
    assert value("cdi_terminology_bindings_total", outcome="bound") == bound0 + out["bound"]
    assert (
        value("cdi_terminology_bindings_total", outcome="local_only") == local0 + out["local_only"]
    )


def test_every_gated_fact_observes_its_confidence(db_env):
    """E10-S4: the input of the confidence-drift alert, one observation per gated fact."""
    doc = _validated_doc()

    def total() -> float:
        return sum(value("cdi_fact_confidence_count", fact_type=f)
                   for f in (*metrics.FACT_TYPES, "other"))  # fmt: skip

    n0 = total()
    out = validate_document(doc)
    assert out["facts"] >= 1
    assert total() == n0 + out["auto_accepted"] + out["in_review"]
    assert value("cdi_fact_confidence_count", fact_type="medication") >= 1
