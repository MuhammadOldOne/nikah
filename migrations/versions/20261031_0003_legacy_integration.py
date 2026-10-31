"""legacy bot integration: profile numbers, no name, nullable new fields, witness messages,
several conversations per pair, legacy-compatible interests, app_meta

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31 20:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUMBER_SEQ = "profiles_number_seq"
NUMBER_START = 500
# Required by the spec, never asked by the legacy bot: NULL in migrated profiles.
NEW_REQUIRED_COLUMNS = (
    ("marital_status", sa.String(length=32)),
    ("children", sa.SmallInteger()),
    ("education", sa.String(length=32)),
    ("employment", sa.String(length=32)),
)
MESSAGE_KIND_CHECK = "(kind = 'user' AND sender_id IS NOT NULL) OR (kind = 'witness' AND sender_id IS NULL)"


def upgrade() -> None:
    # profiles.number: existing profiles are numbered from 500 in the order they were created.
    op.execute(sa.schema.CreateSequence(sa.Sequence(NUMBER_SEQ, start=NUMBER_START)))
    op.add_column("profiles", sa.Column("number", sa.Integer(), nullable=True))
    op.execute(
        f"""
        UPDATE profiles AS p SET number = {NUMBER_START - 1} + ordered.position
        FROM (SELECT user_id, row_number() OVER (ORDER BY created_at, user_id) AS position FROM profiles) AS ordered
        WHERE p.user_id = ordered.user_id
        """
    )
    op.execute(
        f"SELECT setval('{NUMBER_SEQ}', COALESCE(MAX(number), {NUMBER_START}), MAX(number) IS NOT NULL) FROM profiles"
    )
    op.alter_column("profiles", "number", nullable=False, server_default=sa.text(f"nextval('{NUMBER_SEQ}'::regclass)"))
    op.execute(f"ALTER SEQUENCE {NUMBER_SEQ} OWNED BY profiles.number")
    op.create_unique_constraint(op.f("uq_profiles_number"), "profiles", ["number"])

    op.drop_column("profiles", "name")
    for column, type_ in NEW_REQUIRED_COLUMNS:
        op.alter_column("profiles", column, existing_type=type_, nullable=True)

    # Witness messages: kind 'witness' and no sender.
    op.add_column("messages", sa.Column("kind", sa.String(length=32), server_default=sa.text("'user'"), nullable=False))
    op.alter_column("messages", "sender_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(op.f("ck_messages_kind_sender"), "messages", MESSAGE_KIND_CHECK)

    # A pair may have several conversations over time; only one may be open.
    op.drop_constraint(op.f("uq_conversations_user_a_id_user_b_id"), "conversations", type_="unique")
    op.create_index(
        "uq_conversations_open_pair",
        "conversations",
        ["user_a_id", "user_b_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index("ix_conversations_pair", "conversations", ["user_a_id", "user_b_id"], unique=False)

    # Interests: legacy states (both directions pending, several accepted over time) are valid.
    op.drop_index("uq_interests_active_pair", table_name="interests")
    op.create_index(
        "uq_interests_pending_direction",
        "interests",
        ["from_user_id", "to_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "app_meta",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_meta")),
    )


def _refuse(sql: str, what: str) -> None:
    count = op.get_bind().execute(sa.text(sql)).scalar() or 0
    if count:
        raise RuntimeError(f"cannot downgrade to 0002: {count} {what} cannot be represented there")


def downgrade() -> None:
    """Restores the 0002 schema. Witness messages are deleted (0002 has no such messages); data
    that 0002 cannot hold (incomplete migrated profiles, several conversations or active interests
    per pair) stops the downgrade before anything is changed."""
    _refuse(
        "SELECT count(*) FROM profiles WHERE " + " OR ".join(f"{column} IS NULL" for column, _ in NEW_REQUIRED_COLUMNS),
        "profile(s) without the fields 0002 requires",
    )
    _refuse(
        "SELECT count(*) FROM (SELECT 1 FROM conversations GROUP BY user_a_id, user_b_id HAVING count(*) > 1) AS d",
        "pair(s) with several conversations",
    )
    _refuse(
        "SELECT count(*) FROM (SELECT 1 FROM interests WHERE status IN ('pending', 'accepted') "
        "GROUP BY LEAST(from_user_id, to_user_id), GREATEST(from_user_id, to_user_id) HAVING count(*) > 1) AS d",
        "pair(s) with several pending/accepted interests",
    )

    op.drop_table("app_meta")

    op.drop_index(
        "uq_interests_pending_direction", table_name="interests", postgresql_where=sa.text("status = 'pending'")
    )
    op.create_index(
        "uq_interests_active_pair",
        "interests",
        [sa.text("LEAST(from_user_id, to_user_id)"), sa.text("GREATEST(from_user_id, to_user_id)")],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'accepted')"),
    )

    op.drop_index("ix_conversations_pair", table_name="conversations")
    op.drop_index(
        "uq_conversations_open_pair", table_name="conversations", postgresql_where=sa.text("closed_at IS NULL")
    )
    op.create_unique_constraint(
        op.f("uq_conversations_user_a_id_user_b_id"), "conversations", ["user_a_id", "user_b_id"]
    )

    op.execute("DELETE FROM messages WHERE kind = 'witness'")
    op.drop_constraint(op.f("ck_messages_kind_sender"), "messages", type_="check")
    op.alter_column("messages", "sender_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("messages", "kind")

    for column, type_ in NEW_REQUIRED_COLUMNS:
        op.alter_column("profiles", column, existing_type=type_, nullable=False)
    # 0002 requires a name; profiles are known by number since 0003.
    op.add_column("profiles", sa.Column("name", sa.String(length=40), nullable=True))
    op.execute("UPDATE profiles SET name = 'Анкета ' || number")
    op.alter_column("profiles", "name", existing_type=sa.String(length=40), nullable=False)

    op.drop_constraint(op.f("uq_profiles_number"), "profiles", type_="unique")
    op.drop_column("profiles", "number")  # drops the owned sequence too
    op.execute(f"DROP SEQUENCE IF EXISTS {NUMBER_SEQ}")
