"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-31 11:44:27+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(name: str, *values: str) -> sa.Enum:
    """Enums are VARCHAR(32) columns without CHECK constraints (see app.models.enum_type)."""
    return sa.Enum(*values, name=name, native_enum=False, length=32)


def timestamp(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if now else None,
        nullable=nullable,
    )


def user_fk(column: str, table: str, ondelete: str = "CASCADE") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], ["users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete=ondelete)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_banned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        timestamp("banned_at"),
        sa.Column("notify_telegram", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        timestamp("consent_at"),
        sa.Column("bot_active_conversation_id", sa.BigInteger(), nullable=True),
        timestamp("last_seen_at"),
        timestamp("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_index(op.f("ix_users_bot_active_conversation_id"), "users", ["bot_active_conversation_id"], unique=False)

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        timestamp("created_at", nullable=False, now=True),
        timestamp("next_attempt_at", nullable=False, now=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        timestamp("sent_at"),
        timestamp("failed_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL AND failed_at IS NULL"),
    )

    op.create_table(
        "profiles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("gender", enum("gender", "male", "female"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("birth_year", sa.SmallInteger(), nullable=False),
        sa.Column("city", sa.String(length=60), nullable=False),
        sa.Column("marital_status", enum("maritalstatus", "never_married", "divorced", "widowed"), nullable=False),
        sa.Column("children", sa.SmallInteger(), nullable=False),
        sa.Column(
            "education",
            enum(
                "education",
                "secondary",
                "vocational",
                "incomplete_higher",
                "higher",
                "academic_degree",
                "religious",
            ),
            nullable=False,
        ),
        sa.Column("employment", enum("employment", "employed", "business", "student", "unemployed"), nullable=False),
        sa.Column("wishes", sa.Text(), nullable=False),
        sa.Column("height", sa.SmallInteger(), nullable=True),
        sa.Column("weight", sa.SmallInteger(), nullable=True),
        sa.Column("occupation", sa.String(length=100), nullable=True),
        sa.Column("nationality", sa.String(length=50), nullable=True),
        sa.Column("madhab", enum("madhab", "hanafi", "shafii", "maliki", "hanbali", "unspecified"), nullable=True),
        sa.Column("prayer", enum("prayer", "always", "mostly", "sometimes", "learning"), nullable=True),
        sa.Column("nikah_timeline", enum("nikahtimeline", "asap", "six_months", "year", "undecided"), nullable=True),
        sa.Column("relocation", enum("relocation", "ready", "not_ready", "negotiable"), nullable=True),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column(
            "status",
            enum("profilestatus", "pending", "active", "hidden", "rejected"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("moderation_note", sa.Text(), nullable=True),
        sa.Column("flagged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        timestamp("submitted_at"),
        timestamp("approved_at"),
        timestamp("created_at", nullable=False, now=True),
        timestamp("updated_at", nullable=False, now=True),
        user_fk("user_id", "profiles"),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_profiles")),
    )
    op.create_index("ix_profiles_catalog", "profiles", ["status", "gender", "birth_year"], unique=False)
    op.create_index(
        "ix_profiles_city_key", "profiles", [sa.literal_column("replace(lower(city), 'ё', 'е')")], unique=False
    )
    op.create_index(
        "ix_profiles_pending",
        "profiles",
        ["submitted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "interests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("from_user_id", sa.BigInteger(), nullable=False),
        sa.Column("to_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "status",
            enum("intereststatus", "pending", "accepted", "declined", "cancelled"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("flagged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        timestamp("created_at", nullable=False, now=True),
        timestamp("decided_at"),
        sa.CheckConstraint("from_user_id <> to_user_id", name=op.f("ck_interests_not_self")),
        user_fk("from_user_id", "interests"),
        user_fk("to_user_id", "interests"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interests")),
    )
    op.create_index("ix_interests_from_user_created", "interests", ["from_user_id", "created_at"], unique=False)
    op.create_index("ix_interests_to_user_status", "interests", ["to_user_id", "status"], unique=False)
    op.create_index(
        "uq_interests_active_pair",
        "interests",
        [
            sa.literal_column("LEAST(from_user_id, to_user_id)"),
            sa.literal_column("GREATEST(from_user_id, to_user_id)"),
        ],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'accepted')"),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_a_id", sa.BigInteger(), nullable=True),
        sa.Column("user_b_id", sa.BigInteger(), nullable=True),
        sa.Column("created_from_interest_id", sa.BigInteger(), nullable=True),
        timestamp("created_at", nullable=False, now=True),
        timestamp("last_message_at"),
        timestamp("closed_at"),
        sa.Column("closed_by_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "close_reason",
            enum("conversationclosereason", "closed_by_user", "account_deleted", "user_banned"),
            nullable=True,
        ),
        sa.CheckConstraint("user_a_id < user_b_id", name=op.f("ck_conversations_ordered_pair")),
        user_fk("closed_by_id", "conversations", "SET NULL"),
        sa.ForeignKeyConstraint(
            ["created_from_interest_id"],
            ["interests.id"],
            name=op.f("fk_conversations_created_from_interest_id_interests"),
            ondelete="SET NULL",
        ),
        user_fk("user_a_id", "conversations", "SET NULL"),
        user_fk("user_b_id", "conversations", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("user_a_id", "user_b_id", name=op.f("uq_conversations_user_a_id_user_b_id")),
    )
    op.create_index(op.f("ix_conversations_closed_by_id"), "conversations", ["closed_by_id"], unique=False)
    op.create_index(
        op.f("ix_conversations_created_from_interest_id"), "conversations", ["created_from_interest_id"], unique=False
    )
    op.create_index("ix_conversations_user_b_id", "conversations", ["user_b_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_users_bot_active_conversation_id_conversations"),
        "users",
        "conversations",
        ["bot_active_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", enum("messageorigin", "web", "telegram"), nullable=False),
        timestamp("created_at", nullable=False, now=True),
        timestamp("read_at"),
        sa.Column("flagged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        user_fk("sender_id", "messages"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index("ix_messages_conversation_id_id", "messages", ["conversation_id", "id"], unique=False)
    op.create_index(
        "ix_messages_flagged", "messages", ["created_at"], unique=False, postgresql_where=sa.text("flagged")
    )
    op.create_index("ix_messages_sender_id", "messages", ["sender_id"], unique=False)
    op.create_index(
        "ix_messages_unread",
        "messages",
        ["conversation_id", "sender_id"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )

    op.create_table(
        "guardians",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ward_user_id", sa.BigInteger(), nullable=False),
        sa.Column("guardian_user_id", sa.BigInteger(), nullable=True),
        sa.Column("invite_token", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            enum("guardianstatus", "pending", "active", "revoked"),
            server_default="pending",
            nullable=False,
        ),
        timestamp("created_at", nullable=False, now=True),
        timestamp("expires_at", nullable=False),
        timestamp("linked_at"),
        timestamp("revoked_at"),
        sa.CheckConstraint("guardian_user_id <> ward_user_id", name=op.f("ck_guardians_not_self")),
        user_fk("guardian_user_id", "guardians"),
        user_fk("ward_user_id", "guardians"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guardians")),
        sa.UniqueConstraint("invite_token", name=op.f("uq_guardians_invite_token")),
    )
    op.create_index("ix_guardians_guardian_user_id", "guardians", ["guardian_user_id"], unique=False)
    op.create_index("ix_guardians_ward_user_id", "guardians", ["ward_user_id"], unique=False)
    op.create_index(
        "uq_guardians_active_ward",
        "guardians",
        ["ward_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "login_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", enum("logintokenkind", "login", "magic"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        timestamp("created_at", nullable=False, now=True),
        timestamp("expires_at", nullable=False),
        timestamp("confirmed_at"),
        timestamp("consumed_at"),
        user_fk("user_id", "login_tokens"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_login_tokens_token_hash")),
    )
    op.create_index("ix_login_tokens_expires_at", "login_tokens", ["expires_at"], unique=False)
    op.create_index("ix_login_tokens_user_id", "login_tokens", ["user_id"], unique=False)

    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("reporter_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "reason",
            enum("reportreason", "fraud", "married", "fake", "insult", "indecent", "other"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", enum("reportstatus", "open", "resolved"), server_default="open", nullable=False),
        sa.Column("resolution", enum("reportaction", "dismiss", "hide_profile", "ban_user"), nullable=True),
        sa.Column("resolved_by_id", sa.BigInteger(), nullable=True),
        timestamp("resolved_at"),
        timestamp("created_at", nullable=False, now=True),
        sa.CheckConstraint("reporter_id <> target_user_id", name=op.f("ck_reports_not_self")),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_reports_message_id_messages"), ondelete="SET NULL"
        ),
        user_fk("reporter_id", "reports"),
        user_fk("resolved_by_id", "reports", "SET NULL"),
        user_fk("target_user_id", "reports"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_message_id", "reports", ["message_id"], unique=False)
    op.create_index("ix_reports_reporter_id", "reports", ["reporter_id"], unique=False)
    op.create_index(op.f("ix_reports_resolved_by_id"), "reports", ["resolved_by_id"], unique=False)
    op.create_index("ix_reports_status_created_at", "reports", ["status", "created_at"], unique=False)
    op.create_index("ix_reports_target_user_id", "reports", ["target_user_id"], unique=False)


def downgrade() -> None:
    op.drop_constraint(op.f("fk_users_bot_active_conversation_id_conversations"), "users", type_="foreignkey")
    for table in (
        "reports",
        "login_tokens",
        "guardians",
        "messages",
        "conversations",
        "interests",
        "profiles",
        "outbox",
        "users",
    ):
        op.drop_table(table)
