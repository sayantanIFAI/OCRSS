"""Patient registry: the clinic's own master list, seeded with 4 dummy patients (ARCHITECTURE §5).

Composite key (mobile, lower(name), dob). All seed data is synthetic.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SEED = [
    # patient_id (CareFlow id), name, mobile, dob, gender, address, abha_id
    (
        "CFP-2025-900001",
        "Ramesh Kumar",
        "9000000001",
        "1971-03-14",
        "M",
        "12 Test Lane, Kolkata",
        "91-0000-0000-0001",
    ),
    (
        "CFP-2025-900002",
        "Sunita Sharma",
        "9000000002",
        "1985-07-02",
        "F",
        "34 Sample Road, Pune",
        "91-0000-0000-0002",
    ),
    (
        "CFP-2025-900003",
        "Arjun Mehta",
        "9000000003",
        "1992-11-23",
        "M",
        "56 Demo Street, Jaipur",
        None,
    ),
    (
        "CFP-2025-900004",
        "Priya Nair",
        "9000000004",
        "1968-01-30",
        "F",
        "78 Example Nagar, Kochi",
        "91-0000-0000-0004",
    ),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE patient_registry (
            patient_id  text PRIMARY KEY,               -- CareFlow id
            name        text NOT NULL,
            mobile      text NOT NULL,
            dob         date NOT NULL,
            gender      text CHECK (gender IN ('M', 'F', 'O', 'U')),
            address     text,
            abha_id     text UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_patient_registry_key ON patient_registry (mobile, lower(name), dob)"
    )
    op.execute(
        "CREATE INDEX ix_patient_registry_mobile_digits "
        "ON patient_registry (regexp_replace(mobile, '\\D', '', 'g'))"
    )
    for pid, name, mobile, dob, gender, address, abha in SEED:
        abha_sql = f"'{abha}'" if abha else "NULL"
        op.execute(
            "INSERT INTO patient_registry "
            "(patient_id, name, mobile, dob, gender, address, abha_id) VALUES "
            f"('{pid}', '{name}', '{mobile}', '{dob}', '{gender}', '{address}', {abha_sql})"
        )


def downgrade() -> None:
    op.execute("DROP TABLE patient_registry")
