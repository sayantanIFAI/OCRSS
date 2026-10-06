"""Patient MPI: durable CareFlow id CFP-<YYYY>-<seq> + identity confidence (ARCHITECTURE §5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE patient_mpi_seq START 1")
    op.execute(
        """
        ALTER TABLE patient_identity
            ADD COLUMN mpi_id text UNIQUE,
            ADD COLUMN identity_confidence real
                CHECK (identity_confidence BETWEEN 0 AND 1)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE patient_identity DROP COLUMN identity_confidence, DROP COLUMN mpi_id")
    op.execute("DROP SEQUENCE patient_mpi_seq")
