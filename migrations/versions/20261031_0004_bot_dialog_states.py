"""bot dialog states: the bot's FSM in the database (dialogs survive restarts)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31 22:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_dialog_states",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=200), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_bot_dialog_states")),
    )
    op.create_index(
        op.f("ix_bot_dialog_states_telegram_user_id"), "bot_dialog_states", ["telegram_user_id"], unique=False
    )
    op.create_index(op.f("ix_bot_dialog_states_updated_at"), "bot_dialog_states", ["updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bot_dialog_states_updated_at"), table_name="bot_dialog_states")
    op.drop_index(op.f("ix_bot_dialog_states_telegram_user_id"), table_name="bot_dialog_states")
    op.drop_table("bot_dialog_states")
