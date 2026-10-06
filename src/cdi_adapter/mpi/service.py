"""Identity (MPI), fetched from the documents (ARCHITECTURE §5 "Identity").

* ``candidate_from_payload`` pulls name / sex / age_text / dob from an extracted ``patient``
  block; ``parse_name`` strips titles (Dr/Mr/S/o/...), ``parse_sex`` -> M/F/O, ``parse_age`` ->
  approximate birth year.
* ``resolve_identity``: (1) ABHA match -> existing patient; (2) else fuzzy: ``SequenceMatcher`` on
  the normalised name + birth year within 1 -> existing patient at ratio >= 0.90; (3) else mint a
  new record ``mpi_id = CFP-<YYYY>-<seq>`` (Postgres ``patient_mpi_seq``).
* Every document's raw reading is stored in ``patient_identity_alias``.
* ``merge_identity_evidence`` picks the most-supported name / sex / DOB (mode, longest on ties)
  and sets ``identity_confidence`` from how strongly the documents agreed. A registry-backed
  patient's demographics are **never** overwritten: registration is the source of truth and
  document demographics are evidence only (backlog §0.5).
* ``names_match`` (token subset or fuzzy >= 0.72) and ``dob_match`` (+-1 yr) power the
  existing-patient mismatch guard used by S4.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.dates import parse_date

FUZZY_NAME_RATIO = 0.90
MISMATCH_NAME_RATIO = 0.72
MAX_DOCUMENT_CONFIDENCE = 0.99  # identity read from documents is never certain

_TITLES = re.compile(
    r"^(?:(?:dr|mr|mrs|ms|miss|master|mstr|baby|smt|shri|sri|kum|km|prof|late)\.?\s+)+", re.I
)
_RELATION = re.compile(r"\s+(?:s|d|w|c)\s*/\s*o\b.*$", re.I)


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def parse_name(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", str(raw))  # "(55/M)" style annotations
    s = _RELATION.sub("", s.strip())
    s = _TITLES.sub("", s)
    s = re.sub(r"[^A-Za-z .'-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s.title() if s else None


def normalize_name(name: str | None) -> str:
    return re.sub(r"[^a-z ]", "", (parse_name(name) or "").lower()).strip()


def parse_sex(raw: str | None) -> str | None:
    s = (raw or "").strip().lower()
    if s in ("m", "male", "man", "boy"):
        return "M"
    if s in ("f", "female", "woman", "girl"):
        return "F"
    if s in ("o", "other", "transgender", "tg", "others"):
        return "O"
    return None


def parse_age(age_text: str | None, on: date | None = None) -> int | None:
    """Approximate birth year from '48 Y', '48 yrs', '48', '6 months', '10 D'."""
    if not age_text:
        return None
    m = re.search(r"(\d{1,3})\s*(y|yr|yrs|year|years|m|mo|mon|month|months|d|day|days)?\b",
                  str(age_text).lower())  # fmt: skip
    if not m:
        return None
    n, unit = int(m.group(1)), (m.group(2) or "y")
    ref = on or date.today()
    if unit.startswith("y"):
        return ref.year - n if n <= 120 else None
    if unit.startswith("m"):  # n months before the reference month
        return (ref.year * 12 + (ref.month - 1) - n) // 12
    return ref.year


@dataclass
class Candidate:
    name: str | None = None
    sex: str | None = None
    age_text: str | None = None
    dob: date | None = None
    birth_year: int | None = None
    abha: str | None = None
    mrn: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.__dict__.items()}


def candidate_from_payload(payload: dict[str, Any] | None, on: date | None = None) -> Candidate:
    p = (payload or {}).get("patient") or {}
    parsed = parse_date(p.get("dob"))
    dob = parsed[0] if parsed and parsed[1] == "day" else None
    birth_year = parsed[0].year if parsed else parse_age(p.get("age_text"), on)
    abha = p.get("abha")
    return Candidate(
        name=parse_name(p.get("name")),
        sex=parse_sex(p.get("sex")),
        age_text=p.get("age_text"),
        dob=dob,
        birth_year=birth_year,
        abha=digits(abha) if abha and len(digits(abha)) == 14 else None,
        mrn=p.get("mrn"),
    )


def names_match(a: str | None, b: str | None) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= MISMATCH_NAME_RATIO


def dob_match(a: date | int | None, b: date | int | None) -> bool:
    if a is None or b is None:
        return True  # nothing to contradict
    ya = a.year if isinstance(a, date) else a
    yb = b.year if isinstance(b, date) else b
    return abs(ya - yb) <= 1


# ------------------------------------------------------------------ DB operations


def _mint(conn: Connection, cand: Candidate) -> UUID:
    seq = conn.execute(text("SELECT nextval('patient_mpi_seq')")).scalar_one()
    mpi_id = f"CFP-{date.today().year}-{seq:06d}"
    pid = conn.execute(
        text(
            "INSERT INTO patient_identity (mpi_id, name, sex, birth_date, abha_number, legacy_mrn) "
            "VALUES (:mpi, :name, :sex, :dob, :abha, :mrn) RETURNING id"
        ),
        {"mpi": mpi_id, "name": cand.name, "sex": cand.sex,
         "dob": cand.dob or (date(cand.birth_year, 1, 1) if cand.birth_year else None),
         "abha": cand.abha, "mrn": cand.mrn},
    ).scalar_one()  # fmt: skip
    repo.write_audit(conn, actor="system", action="create", entity="patient_identity",
                     entity_id=pid, detail={"mpi_id": mpi_id, "how": "minted"})  # fmt: skip
    return pid


# pg advisory-lock key serialising resolve_identity (any fixed bigint; ascii 'CDI MPI')
RESOLVE_LOCK_KEY = 0x434449204D5049


def resolve_identity(conn: Connection, cand: Candidate) -> tuple[UUID, str]:
    """Return ``(patient_id, how)`` with ``how`` in ``abha`` | ``fuzzy`` | ``minted``.

    Look-up and mint are one step under a transaction-scoped advisory lock: jobs extract in
    parallel with vLLM (backlog E2-S2), and two documents of the same new patient must not both
    miss the look-up and mint two records. The lock is released when the caller's transaction ends.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": RESOLVE_LOCK_KEY})
    if cand.abha:
        pid = conn.execute(
            text("SELECT id FROM patient_identity "
                 "WHERE regexp_replace(abha_number, '\\D', '', 'g') = :a"), {"a": cand.abha}
        ).scalar_one_or_none()  # fmt: skip
        if pid:
            return pid, "abha"
    if cand.name and cand.birth_year:
        rows = conn.execute(
            text("SELECT id, name FROM patient_identity WHERE birth_date IS NOT NULL "
                 "AND abs(extract(year FROM birth_date) - :y) <= 1"), {"y": cand.birth_year}
        ).mappings()  # fmt: skip
        target = normalize_name(cand.name)
        best = max(
            ((r["id"], SequenceMatcher(None, target, normalize_name(r["name"])).ratio())
             for r in rows),
            key=lambda t: t[1], default=None,
        )  # fmt: skip
        if best and best[1] >= FUZZY_NAME_RATIO:
            return best[0], "fuzzy"
    return _mint(conn, cand), "minted"


def add_alias(
    conn: Connection, patient_id: UUID | str, document_id: UUID | str | None, cand: Candidate
) -> UUID:
    return conn.execute(
        text(
            "INSERT INTO patient_identity_alias "
            "  (patient_id, source_document_id, name, sex, birth_date, age_text, raw) "
            "VALUES (:p, :d, :name, :sex, :dob, :age, CAST(:raw AS jsonb)) RETURNING id"
        ),
        {"p": patient_id, "d": document_id, "name": cand.name, "sex": cand.sex,
         "dob": cand.dob, "age": cand.age_text, "raw": repo._json(cand.as_json())},
    ).scalar_one()  # fmt: skip


def _mode(values: list[Any]) -> tuple[Any, float]:
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return None, 0.0
    counts = Counter(vals)
    top = max(counts.values())
    winner = max((v for v in counts if counts[v] == top), key=lambda v: len(str(v)))
    return winner, top / len(vals)


def is_registry_backed(conn: Connection, patient_id: UUID | str) -> bool:
    return bool(conn.execute(
        text("SELECT 1 FROM patient_identity i JOIN patient_registry r ON r.patient_id = i.mpi_id "
             "WHERE i.id = :p"), {"p": patient_id}
    ).first())  # fmt: skip


def merge_identity_evidence(conn: Connection, patient_id: UUID | str) -> dict[str, Any]:
    aliases = conn.execute(
        text("SELECT name, sex, birth_date FROM patient_identity_alias WHERE patient_id = :p"),
        {"p": patient_id},
    ).mappings().all()  # fmt: skip
    if is_registry_backed(conn, patient_id):
        return {"merged": False, "reason": "registry is the source of truth"}
    if not aliases:
        return {"merged": False, "reason": "no document evidence"}
    name, n_agree = _mode([a["name"] for a in aliases])
    sex, s_agree = _mode([a["sex"] for a in aliases])
    dob, d_agree = _mode([a["birth_date"] for a in aliases])
    present = [x for x, v in ((n_agree, name), (s_agree, sex), (d_agree, dob)) if v is not None]
    confidence = (
        round(min(MAX_DOCUMENT_CONFIDENCE, sum(present) / len(present)), 2) if present else 0.0
    )
    conn.execute(
        text(
            "UPDATE patient_identity SET name = coalesce(:name, name), sex = coalesce(:sex, sex), "
            "birth_date = coalesce(:dob, birth_date), identity_confidence = :c, "
            "updated_at = now() WHERE id = :p"
        ),
        {"p": patient_id, "name": name, "sex": sex, "dob": dob, "c": confidence},
    )
    repo.write_audit(conn, actor="system", action="update", entity="patient_identity",
                     entity_id=patient_id,
                     detail={"merge": {"name": name, "sex": sex, "dob": str(dob) if dob else None},
                             "identity_confidence": confidence})  # fmt: skip
    return {"merged": True, "name": name, "sex": sex, "birth_date": dob,
            "identity_confidence": confidence}  # fmt: skip


def get_patient(conn: Connection, patient_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM patient_identity WHERE id = :p"), {"p": patient_id}
    ).mappings().first()  # fmt: skip
    return dict(row) if row else None


def patient_summary(conn: Connection, patient_id: UUID | str) -> dict[str, Any] | None:
    """The identity as staff see it after an upload. ``age_text`` is the age as printed on the
    patient's latest document that states one. ``birth_date`` is given only when it is known: a
    document printed that full date, or the registry holds it. A minted identity with only an
    age keeps 1 January of the estimated year for matching (``_mint``); shown or prefilled, that
    estimate would be saved into the registry as if it were the real date of birth."""
    row = conn.execute(
        text(
            "SELECT i.mpi_id, i.name, i.sex, i.identity_confidence, "
            "  CASE WHEN EXISTS (SELECT 1 FROM patient_identity_alias a "
            "                    WHERE a.patient_id = i.id AND a.birth_date = i.birth_date) "
            "         OR EXISTS (SELECT 1 FROM patient_registry r "
            "                    WHERE r.patient_id = i.mpi_id AND r.dob = i.birth_date) "
            "       THEN i.birth_date END AS birth_date, "
            "  (SELECT a.age_text FROM patient_identity_alias a "
            "   WHERE a.patient_id = i.id AND a.age_text IS NOT NULL "
            "   ORDER BY a.created_at DESC LIMIT 1) AS age_text "
            "FROM patient_identity i WHERE i.id = :p"
        ),
        {"p": patient_id},
    ).mappings().first()  # fmt: skip
    return dict(row) if row else None
