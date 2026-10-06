"""Web-app authentication: staff accounts + server-side sessions (backlog E11-S1; ARCHITECTURE §12).

  web_user      one row per staff login; role is intake | reviewer | admin; the password is stored
                only as a salted scrypt hash; failed_logins + locked_until throttle guessing
  web_session   one row per signed-in browser; the cookie carries a random token, the table keeps
                only its sha256, so a database read never yields a usable session

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE web_user (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        username            text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9._-]{3,64}$'),
        display_name        text NOT NULL,
        role                text NOT NULL CHECK (role IN ('intake', 'reviewer', 'admin')),
        password_hash       text NOT NULL,
        active              boolean NOT NULL DEFAULT true,
        failed_logins       integer NOT NULL DEFAULT 0,
        locked_until        timestamptz,
        password_changed_at timestamptz NOT NULL DEFAULT now(),
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE web_session (
        token_sha256    char(64) PRIMARY KEY,
        user_id         uuid NOT NULL REFERENCES web_user (id) ON DELETE CASCADE,
        created_at      timestamptz NOT NULL DEFAULT now(),
        last_seen_at    timestamptz NOT NULL DEFAULT now(),
        expires_at      timestamptz NOT NULL,
        client          text
    )
    """,
    "CREATE INDEX ix_web_session_user ON web_session (user_id)",
    "CREATE INDEX ix_web_session_expiry ON web_session (expires_at)",
]


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS web_session")
    op.execute("DROP TABLE IF EXISTS web_user")
