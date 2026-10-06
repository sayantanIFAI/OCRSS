"""Patient registry: the clinic's own master list (ARCHITECTURE §5 "Patient registry").

``lookup(q)`` resolves an existing patient by **CareFlow id / ABHA id / mobile** (digits-only
match, tolerant of dashes and spaces). ``ensure_identity`` creates the ``patient_identity`` row
from the registry data if it doesn't exist yet, so all of a job's documents attach to that
patient and no new CareFlow id is minted. ``upsert_registry`` backs ``POST /api/registry``
("Complete patient details" for a new patient).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.mpi.service import digits

REGISTRY_FIELDS = ("name", "mobile", "dob", "gender", "address", "abha_id")


def lookup(conn: Connection, q: str | None) -> dict[str, Any] | None:
    q = (q or "").strip()
    if not q:
        return None
    row = conn.execute(
        text("SELECT * FROM patient_registry WHERE upper(patient_id) = upper(:q)"), {"q": q}
    ).mappings().first()  # fmt: skip
    d = digits(q)
    if row is None and len(d) >= 10:  # never match on a short digit fragment
        row = conn.execute(
            text(
                "SELECT * FROM patient_registry "
                "WHERE regexp_replace(coalesce(abha_id, ''), '\\D', '', 'g') = :d "
                "   OR regexp_replace(mobile, '\\D', '', 'g') = :d "
                "ORDER BY (regexp_replace(coalesce(abha_id, ''), '\\D', '', 'g') = :d) DESC "
                "LIMIT 1"
            ),
            {"d": d},
        ).mappings().first()  # fmt: skip
    return dict(row) if row else None


def ensure_identity(conn: Connection, reg: dict[str, Any], *, actor: str = "system") -> UUID:
    existing = conn.execute(
        text("SELECT id FROM patient_identity WHERE mpi_id = :m"), {"m": reg["patient_id"]}
    ).scalar_one_or_none()  # fmt: skip
    if existing:
        return existing
    pid = conn.execute(
        text(
            "INSERT INTO patient_identity (mpi_id, name, sex, birth_date, abha_number, "
            "  identity_confidence) VALUES (:m, :name, :sex, :dob, :abha, 1.0) RETURNING id"
        ),
        {"m": reg["patient_id"], "name": reg["name"], "sex": reg.get("gender"),
         "dob": reg.get("dob"), "abha": reg.get("abha_id")},
    ).scalar_one()  # fmt: skip
    detail = {"mpi_id": reg["patient_id"], "how": "registry"}
    repo.write_audit(
        conn, actor=actor, action="create", entity="patient_identity", entity_id=pid, detail=detail
    )
    return pid


def upsert_registry(
    conn: Connection, patient_id: str, fields: dict[str, Any], *, actor: str = "system"
) -> dict[str, Any]:
    data = {k: fields.get(k) for k in REGISTRY_FIELDS}
    # a real UPDATE for an existing row: INSERT .. ON CONFLICT checks NOT NULL on the proposed
    # row first, so a partial update (e.g. only a new address) would fail
    row = conn.execute(
        text(
            "UPDATE patient_registry SET "
            "  name = coalesce(:name, name), mobile = coalesce(:mobile, mobile), "
            "  dob = coalesce(:dob, dob), gender = coalesce(:gender, gender), "
            "  address = coalesce(:address, address), abha_id = coalesce(:abha_id, abha_id), "
            "  updated_at = now() "
            "WHERE patient_id = :pid RETURNING *"
        ),
        {"pid": patient_id, **data},
    ).mappings().first()  # fmt: skip
    if row is None:
        missing = [k for k in ("name", "mobile", "dob") if not data.get(k)]
        if missing:
            raise ValueError(f"new registry patient needs {', '.join(missing)}")
        row = conn.execute(
            text(
                "INSERT INTO patient_registry "
                "  (patient_id, name, mobile, dob, gender, address, abha_id) "
                "VALUES (:pid, :name, :mobile, :dob, :gender, :address, :abha_id) RETURNING *"
            ),
            {"pid": patient_id, **data},
        ).mappings().one()  # fmt: skip
    if data.get("abha_id"):  # keep the identity's ABHA in step with the registry
        conn.execute(
            text("UPDATE patient_identity SET abha_number = :a, updated_at = now() "
                 "WHERE mpi_id = :m AND abha_number IS NULL"),
            {"a": data["abha_id"], "m": patient_id},
        )  # fmt: skip
    repo.write_audit(conn, actor=actor, action="update", entity="patient_registry",
                     entity_id=patient_id, detail={k: v for k, v in data.items() if v})  # fmt: skip
    return dict(row)
