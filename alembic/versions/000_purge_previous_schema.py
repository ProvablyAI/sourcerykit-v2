"""Drop the tables of the SDK before 1.0 (the ``provably`` package).

Its ``trusted_endpoints`` had an integer id; the table of 1.0 on has a uuid one, and is kept: a
database whose tables were created without migrations (by ``bootstrap_system`` or a hosted
sandbox) runs every migration from here, and must keep its trusted endpoints.

Revision ID: 000
Revises:
Create Date: 2026-05-13 18:43:24.189215
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'trusted_endpoints'
                  AND column_name = 'id' AND data_type <> 'uuid'
            ) THEN
                DROP TABLE trusted_endpoints CASCADE;
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE IF EXISTS provably_intercepts CASCADE;")


def downgrade() -> None:
    pass
