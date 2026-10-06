"""E4-S11: the ocr_agreement table (real PostgreSQL, migration 0018), with MOCK readings.

Two independent readings of one handwritten crop (mock TrOCR and Qwen ``ocr_observation`` rows)
are compared; the result is stored append-only, one row per crop pair, field and algorithm
version. No real second engine exists yet (E2-S11)."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.agreement import agreements_for, record_agreement
from cdi_adapter.db import get_engine, session_scope

pytestmark = pytest.mark.integration


def _crop_read_twice(c, doc_pages: int = 1) -> tuple[str, list[str]]:
    """A handwritten page read by mock TrOCR and mock Qwen: one observation each."""
    sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    doc = repo.insert_source_document(c, sha256=sha, filename="hw.png", mime_type="image/png",
                                      byte_size=1000, storage_key="unused")  # fmt: skip
    pages = [repo.insert_document_page(c, document_id=doc, page_no=n, storage_key=f"p{n}",
                                       thumb_key="t", width=800, height=1000, dpi=200,
                                       preproc={}) for n in range(1, doc_pages + 1)]  # fmt: skip
    crop = hashlib.sha256(f"{sha}:crop".encode()).hexdigest()
    repo.insert_ocr_blocks(c, [
        {"document_id": doc, "page_id": pages[0], "block_type": "line", "text": t,
         "bbox": [10, 10, 300, 40], "polygon": None, "ocr_conf": conf, "reading_order": 0,
         "engine": engine, "pipeline_run_id": None, "crop_hash": crop,
         "region_kind": "handwritten", "field_domain": "strength"}
        for engine, t, conf in (("trocr", "Telma 4O", 0.71), ("vlm", "Telma 40", 0.83))
    ])  # fmt: skip
    obs = [str(i) for i in c.execute(
        text("SELECT id FROM ocr_observation WHERE document_id = :d ORDER BY engine"),
        {"d": doc}).scalars()]  # fmt: skip
    return str(doc), obs


def _record(c, a, b, **kw):
    args = {"observation_id_a": a, "observation_id_b": b, "disagreement_state": "PARTIAL",
            "agreement_score": 0.86, "algorithm_version": "concordance.v1",
            "field_domain": "strength", "trocr_confidence": 0.71, "qwen_confidence": 0.83,
            "reason": "O vs 0 in the strength"} | kw  # fmt: skip
    return record_agreement(c, **args)


def test_the_table_has_exactly_the_specified_columns(db_env):
    with get_engine().connect() as c:
        cols = c.execute(text("SELECT column_name FROM information_schema.columns "
                              "WHERE table_name = 'ocr_agreement' ORDER BY ordinal_position"),
                         ).scalars().all()  # fmt: skip
    assert cols == ["id", "observation_id_a", "observation_id_b", "field_domain",
                    "disagreement_state", "agreement_score", "trocr_confidence",
                    "qwen_confidence", "reason", "algorithm_version", "computed_at"]  # fmt: skip


def test_a_comparison_of_two_readings_is_recorded(db_env):
    with session_scope() as c:
        _, (trocr, qwen) = _crop_read_twice(c)
        rid = _record(c, trocr, qwen)
        got = agreements_for(c, qwen)
    assert [str(g["id"]) for g in got] == [str(rid)]
    row = got[0]
    assert (str(row["observation_id_a"]), str(row["observation_id_b"])) == (trocr, qwen)
    assert row["disagreement_state"] == "PARTIAL" and row["agreement_score"] == pytest.approx(0.86)
    assert row["trocr_confidence"] == pytest.approx(0.71)
    assert row["qwen_confidence"] == pytest.approx(0.83)
    assert row["field_domain"] == "strength" and row["computed_at"] is not None


def test_a_result_is_never_updated_or_deleted(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
        rid = _record(c, a, b)
    for sql in (
        "UPDATE ocr_agreement SET disagreement_state = 'AGREE' WHERE id = :i",
        "DELETE FROM ocr_agreement WHERE id = :i",
    ):
        with pytest.raises(IntegrityError, match="append-only"), get_engine().begin() as c:
            c.execute(text(sql), {"i": str(rid)})
    with pytest.raises(IntegrityError, match="append-only"), get_engine().begin() as c:
        c.execute(text("TRUNCATE ocr_agreement"))
    with session_scope() as c:
        assert agreements_for(c, a)[0]["disagreement_state"] == "PARTIAL"


def test_a_re_run_writes_a_new_algorithm_version_never_overwrites(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
        _record(c, a, b)
    with pytest.raises(IntegrityError), session_scope() as c:
        _record(c, a, b, disagreement_state="AGREE")  # same version again: refused
    with session_scope() as c:
        _record(c, a, b, disagreement_state="AGREE", agreement_score=0.97,
                algorithm_version="concordance.v2")  # fmt: skip
        got = agreements_for(c, a)
    assert sorted(g["algorithm_version"] for g in got) == ["concordance.v1", "concordance.v2"]
    assert {g["disagreement_state"] for g in got} == {"PARTIAL", "AGREE"}  # both kept


def test_the_rules_hold_in_the_database_itself(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
    insert = (
        "INSERT INTO ocr_agreement (observation_id_a, observation_id_b, disagreement_state, "
        "agreement_score, algorithm_version) VALUES (:a, :b, :s, :sc, 'v1')"
    )
    for args in (
        {"a": a, "b": b, "s": "MAYBE", "sc": 0.5},  # not a state
        {"a": a, "b": b, "s": "AGREE", "sc": 1.5},  # score out of range
        {"a": a, "b": a, "s": "AGREE", "sc": 0.9},  # an observation with itself
        {"a": a, "b": str(uuid.uuid4()), "s": "AGREE", "sc": 0.9},
    ):  # no such reading
        with pytest.raises(IntegrityError), get_engine().begin() as c:
            c.execute(text(insert), args)


def test_the_writer_checks_its_input(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
        _, (other, _) = _crop_read_twice(c)
        with pytest.raises(ValueError, match="disagreement_state"):
            _record(c, a, b, disagreement_state="agree")
        with pytest.raises(ValueError, match="between 0 and 1"):
            _record(c, a, b, qwen_confidence=1.2)
        with pytest.raises(ValueError, match="two different"):
            _record(c, a, a)
        with pytest.raises(ValueError, match="same page"):
            _record(c, a, other)  # readings of different documents are not one crop
        with pytest.raises(LookupError):
            _record(c, a, str(uuid.uuid4()))
        with pytest.raises(ValueError, match="algorithm_version"):
            _record(c, a, b, algorithm_version="")
