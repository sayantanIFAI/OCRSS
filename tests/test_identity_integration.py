"""E0-S7 integration: registry + MPI against real PostgreSQL 16 (no object store involved)."""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from cdi_adapter.db import session_scope
from cdi_adapter.mpi import registry
from cdi_adapter.mpi import service as mpi

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "q",
    ["CFP-2025-900001", "cfp-2025-900001", "91-0000-0000-0001", "91 0000 0000 0001",
     "9000000001", "900-000-0001"],
)  # fmt: skip
def test_registry_lookup_by_careflow_id_abha_or_mobile(db_env, q):
    with session_scope() as c:
        row = registry.lookup(c, q)
    assert row is not None and row["name"] == "Ramesh Kumar"


@pytest.mark.parametrize("q", ["", "  ", "0001", "CFP-0000-000000", "9999999999"])
def test_registry_lookup_misses(db_env, q):
    with session_scope() as c:
        assert registry.lookup(c, q) is None


def test_ensure_identity_is_idempotent_and_uses_registry_data(db_env):
    with session_scope() as c:
        reg = registry.lookup(c, "CFP-2025-900002")
        a = registry.ensure_identity(c, reg)
        b = registry.ensure_identity(c, reg)
        p = mpi.get_patient(c, a)
    assert a == b
    assert (p["mpi_id"], p["name"], p["sex"], str(p["birth_date"])) == (
        "CFP-2025-900002", "Sunita Sharma", "F", "1985-07-02")  # fmt: skip
    assert p["abha_number"] == "91-0000-0000-0002" and p["identity_confidence"] == 1.0


def _cand(**kw) -> mpi.Candidate:
    return mpi.Candidate(**kw)


def test_resolve_mints_then_matches_by_abha_then_fuzzy(db_env):
    uniq = uuid.uuid4().hex[:6]
    abha = "7" + str(uuid.uuid4().int)[:13]
    with session_scope() as c:
        pid, how = mpi.resolve_identity(c, _cand(name=f"Kavya {uniq} Iyer", sex="F",
                                                 birth_year=1990, abha=abha))  # fmt: skip
        assert how == "minted"
        minted = mpi.get_patient(c, pid)
    assert re.fullmatch(rf"CFP-{date.today().year}-\d{{6}}", minted["mpi_id"])

    with session_scope() as c:
        assert mpi.resolve_identity(c, _cand(name="Totally Different", abha=abha)) == (pid, "abha")
        # OCR slip in the name + birth year 1 off -> still the same person
        again, how = mpi.resolve_identity(c, _cand(name=f"Kavya {uniq} Iyerr", birth_year=1991))
        assert (again, how) == (pid, "fuzzy")
        # same name but birth year 3 off -> a different person, never merged
        other, how = mpi.resolve_identity(c, _cand(name=f"Kavya {uniq} Iyer", birth_year=1987))
        assert how == "minted" and other != pid


def test_minted_ids_are_sequential(db_env):
    with session_scope() as c:
        a, _ = mpi.resolve_identity(c, _cand(name=f"Seq {uuid.uuid4().hex[:8]}"))
        b, _ = mpi.resolve_identity(c, _cand(name=f"Seq {uuid.uuid4().hex[:8]}"))
        ma, mb = mpi.get_patient(c, a)["mpi_id"], mpi.get_patient(c, b)["mpi_id"]
    assert int(mb.rsplit("-", 1)[1]) == int(ma.rsplit("-", 1)[1]) + 1


def test_merge_identity_evidence_from_three_documents(db_env):
    with session_scope() as c:
        pid, _ = mpi.resolve_identity(c, _cand(name=f"Anjali {uuid.uuid4().hex[:6]}"))
        for name in ("Anjali Das", "Anjali Das", "Anjali"):
            mpi.add_alias(c, pid, None, _cand(name=name, sex="F", dob=date(1978, 1, 1)))
        out = mpi.merge_identity_evidence(c, pid)
        p = mpi.get_patient(c, pid)
        n_alias = c.execute(text("SELECT count(*) FROM patient_identity_alias WHERE patient_id=:p"),
                            {"p": pid}).scalar_one()  # fmt: skip
    assert out["merged"] and n_alias == 3
    assert (p["name"], p["sex"], p["birth_date"]) == ("Anjali Das", "F", date(1978, 1, 1))
    assert p["identity_confidence"] == pytest.approx(0.89, abs=0.01)  # name 2/3, sex 1, dob 1


def test_full_agreement_is_capped_below_certainty(db_env):
    with session_scope() as c:
        pid, _ = mpi.resolve_identity(c, _cand(name=f"Full {uuid.uuid4().hex[:6]}"))
        for _ in range(3):
            mpi.add_alias(c, pid, None, _cand(name="Anjali Das", sex="F", dob=date(1978, 1, 1)))
        assert mpi.merge_identity_evidence(c, pid)["identity_confidence"] == 0.99  # §5: 0.99


def test_documents_never_overwrite_a_registry_identity(db_env):
    with session_scope() as c:
        pid = registry.ensure_identity(c, registry.lookup(c, "CFP-2025-900004"))
        mpi.add_alias(c, pid, None, _cand(name="Someone Else", sex="M", dob=date(2000, 1, 1)))
        out = mpi.merge_identity_evidence(c, pid)
        p = mpi.get_patient(c, pid)
    assert out == {"merged": False, "reason": "registry is the source of truth"}
    assert (p["name"], p["sex"], str(p["birth_date"])) == ("Priya Nair", "F", "1968-01-30")


def test_upsert_registry_new_patient_then_lookup(db_env):
    mobile = "8" + str(uuid.uuid4().int)[:9]
    with session_scope() as c:
        pid, _ = mpi.resolve_identity(c, _cand(name=f"New {uuid.uuid4().hex[:6]}"))
        mpi_id = mpi.get_patient(c, pid)["mpi_id"]
        registry.upsert_registry(c, mpi_id, {"name": "New Patient", "mobile": mobile,
                                             "dob": "1999-05-05", "gender": "F",
                                             "address": "1 Test Rd",
                                             "abha_id": "55-5555-5555-5555"})  # fmt: skip
        registry.upsert_registry(c, mpi_id, {"address": "2 Moved Rd"})  # partial update
    with session_scope() as c:
        row = registry.lookup(c, mobile)
        p = mpi.get_patient(c, pid)
    assert row["patient_id"] == mpi_id and row["address"] == "2 Moved Rd"
    assert row["abha_id"] == "55-5555-5555-5555" and p["abha_number"] == "55-5555-5555-5555"
    assert row["name"] == "New Patient" and row["mobile"] == mobile  # partial update kept them


def test_upsert_registry_new_patient_requires_core_fields(db_env):
    with session_scope() as c, pytest.raises(ValueError, match="name, mobile, dob"):
        registry.upsert_registry(c, f"CFP-X-{uuid.uuid4().hex[:6]}", {"address": "somewhere"})
