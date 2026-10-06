"""Epic 4 invariants tested through direct PostgreSQL writes."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.training import calibration
from tests.test_evidence_constraint_integration import _fact, _patient, _provenance
from tests.test_ocr_agreement_integration import _crop_read_twice, _record

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["delete", "move", "truncate"])
def test_provenance_cannot_be_removed_after_auto_acceptance(db_env, operation):
    with session_scope() as c:
        patient = _patient(c)
        fact = _fact(c, patient, "auto_accepted")
        other = _fact(c, patient)
        _provenance(c, fact)
    sql = {
        "delete": "DELETE FROM fact_provenance WHERE fact_id=:f",
        "move": "UPDATE fact_provenance SET fact_id=:other WHERE fact_id=:f",
        "truncate": "TRUNCATE fact_provenance",
    }[operation]
    with pytest.raises(IntegrityError), get_engine().begin() as c:
        c.execute(text(sql), {"f": fact, "other": other})


def test_concurrent_evidence_deletions_cannot_leave_an_accepted_fact_unwitnessed(db_env):
    with session_scope() as c:
        fact = _fact(c, _patient(c), "auto_accepted")
        _provenance(c, fact)
        _provenance(c, fact)
        ids = list(
            c.execute(
                text("SELECT id FROM fact_provenance WHERE fact_id=:f"), {"f": fact}
            ).scalars()
        )

    ready = Barrier(2)

    def remove(pid):
        try:
            with get_engine().begin() as c:
                ready.wait(timeout=10)
                c.execute(text("DELETE FROM fact_provenance WHERE id=:p"), {"p": pid})
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(remove, ids)) == [False, True]
    with session_scope() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM fact_provenance WHERE fact_id=:f"), {"f": fact}
            ).scalar_one()
            == 1
        )


def test_holding_a_fact_allows_evidence_removal_in_the_same_transaction(db_env):
    with session_scope() as c:
        fact = _fact(c, _patient(c), "auto_accepted")
        _provenance(c, fact)
    with session_scope() as c:
        c.execute(text("DELETE FROM fact_provenance WHERE fact_id=:f"), {"f": fact})
        c.execute(
            text("UPDATE clinical_fact SET review_state='in_review' WHERE id=:f"), {"f": fact}
        )


def test_unsigned_review_state_is_not_training_adjudication(db_env):
    with session_scope() as c:
        fact = _fact(c, _patient(c), "clinician_confirmed")
        _provenance(c, fact)
        assert fact not in {r["proposed_fact_id"] for r in calibration.collect(c)}


def test_reversed_observation_order_cannot_duplicate_agreement(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
        _record(c, a, b)
    with pytest.raises(IntegrityError), session_scope() as c:
        _record(c, b, a)


def test_database_rejects_comparing_different_crops_on_one_page(db_env):
    with session_scope() as c:
        _, (a, b) = _crop_read_twice(c)
        # Append another immutable reading of a different crop on the same page.
        other = c.execute(
            text("""
            INSERT INTO ocr_observation (document_id,page_id,block_type,raw_text,bbox,engine,
                                        region_kind,field_domain,crop_hash,reading_order)
            SELECT document_id,page_id,block_type,raw_text,bbox,engine,region_kind,field_domain,
                   repeat('0',64),reading_order FROM ocr_observation WHERE id=:b RETURNING id
        """),
            {"b": b},
        ).scalar_one()
    with pytest.raises(IntegrityError, match="same handwritten crop"), session_scope() as c:
        c.execute(
            text("""
            INSERT INTO ocr_agreement(observation_id_a,observation_id_b,disagreement_state,
                                      agreement_score,algorithm_version)
            VALUES (:a,:b,'AGREE',1,'test.v1')
        """),
            {"a": a, "b": other},
        )


def test_candidate_cannot_reference_missing_or_cross_document_observations(db_env):
    with session_scope() as c:
        doc, _ = _crop_read_twice(c)
        _, (other, _) = _crop_read_twice(c)
    for obs in (other, str(uuid.uuid4())):
        with pytest.raises(IntegrityError, match="same document"), session_scope() as c:
            c.execute(
                text("""
                INSERT INTO interpretation_candidate(document_id,observation_ids,field,source)
                VALUES (:d,CAST(:obs AS uuid[]),'strength','extractor')
            """),
                {"d": doc, "obs": [obs]},
            )


def test_threshold_storage_does_not_round_down_a_derived_cutoff(db_env):
    with session_scope() as c:
        kind = c.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='gate_threshold' AND column_name='threshold'"
            )
        ).scalar_one()
    assert kind == "double precision"


def test_training_keeps_signed_corrections_after_reextraction(db_env):
    from cdi_adapter import repo
    from tests.test_calibration_training_integration import _adjudicated_mock_document

    doc, _, _ = _adjudicated_mock_document()
    with session_scope() as c:
        before = [r for r in calibration.collect(c) if r["document_id"] == doc]
        assert len(before) == 4
        repo.purge_document_facts(c, doc)
        after = [r for r in calibration.collect(c) if r["document_id"] == doc]
        assert after == before
        published = calibration.export(c)
        loaded = calibration.load(c, published["version"])
        assert {r["proposed_fact_id"] for r in before} <= {
            str(r["proposed_fact_id"]) for r in loaded
        }
    for sql in (
        "UPDATE calibration_adjudication SET example='{}'",
        "DELETE FROM calibration_adjudication",
        "TRUNCATE calibration_adjudication",
    ):
        with pytest.raises(IntegrityError, match="immutable"), session_scope() as c:
            c.execute(text(sql))


def test_legacy_models_and_threshold_sets_require_refitting(db_env):
    from cdi_adapter.config import get_settings
    from cdi_adapter.training import calibrator, thresholds
    from tests.test_calibrator_integration import _mock_dataset

    with session_scope() as c:
        cal = calibrator.fit_calibrator(
            c,
            _mock_dataset(
                [("lab_report", "lab_result", 0.5, {"value_num": 1}, {"value_num": 1}, "confirmed")]
            ),
        )["version"]
        legacy = c.execute(
            text("""
            INSERT INTO calibration_model(version,dataset_version,method,min_examples,
                tolerance,tolerance_basis,summary,created_by)
            SELECT version+1,dataset_version,'isotonic_pav',min_examples,
                tolerance,tolerance_basis,summary,created_by
            FROM calibration_model WHERE version=:v RETURNING version
        """),
            {"v": cal},
        ).scalar_one()
    with pytest.raises(ValueError, match="refitted"):
        calibrator.gate_score(
            legacy, get_settings().database_url, "prescription", "medication", 0.9
        )
    with session_scope() as c:
        with pytest.raises(ValueError, match="refitted"):
            thresholds.derive_thresholds(c, legacy)
        current = thresholds.derive_thresholds(c, cal)["version"]
        old_set = c.execute(
            text("""
            INSERT INTO gate_threshold_set(version,calibrator_version,target_default,target_numeric,
                target_basis,bound,min_accepted,summary,created_by)
            SELECT version+1,calibrator_version,target_default,target_numeric,
                target_basis,'wilson-upper(z=1.645)',min_accepted,summary,created_by
            FROM gate_threshold_set WHERE version=:v RETURNING version
        """),
            {"v": current},
        ).scalar_one()
        with pytest.raises(ValueError, match="rederived"):
            thresholds.config_for(c, old_set)
