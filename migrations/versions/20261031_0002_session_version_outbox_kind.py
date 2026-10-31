"""users.session_version; outbox (kind, created_at) index

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("session_version", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.create_index("ix_outbox_kind_created_at", "outbox", ["kind", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_kind_created_at", table_name="outbox")
    op.drop_column("users", "session_version")
