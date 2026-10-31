"""SQLAlchemy models (spec section 6).

Conventions:
- Public user identifier is ``users.id`` ("#id" in the bot, ``/u/{id}`` on the site); a profile's
  primary key is its ``user_id``.
- Deleting a user row wipes all their data via ON DELETE CASCADE. Conversations survive with the
  deleted side set to NULL so the partner sees "собеседник удалил аккаунт".
- Only many-to-one relationships (plus ``User.profile``) are mapped; collections are queried in
  services. Relationships are lazy: services eager-load what they return (documented in
  docs/ARCHITECTURE.md); elsewhere use ``await obj.awaitable_attrs.<name>``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.clock import utcnow
from app.enums import (
    ConversationCloseReason,
    Education,
    Employment,
    Gender,
    GuardianStatus,
    InterestStatus,
    LoginTokenKind,
    Madhab,
    MaritalStatus,
    MessageOrigin,
    NikahTimeline,
    Prayer,
    ProfileStatus,
    Relocation,
    ReportAction,
    ReportReason,
    ReportStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def enum_type(enum_cls: type[StrEnum]) -> Enum:
    """Enums are stored as VARCHAR values (not native PG enums) so adding a value needs no DDL."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


def user_fk(ondelete: str = "CASCADE") -> ForeignKey:
    return ForeignKey("users.id", ondelete=ondelete)


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=false())
    is_banned: Mapped[bool] = mapped_column(default=False, server_default=false())
    banned_at: Mapped[datetime | None]
    notify_telegram: Mapped[bool] = mapped_column(default=True, server_default=true())
    consent_at: Mapped[datetime | None]
    # Chat the user is currently replying to in the bot; persisted so it survives bot restarts.
    bot_active_conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="SET NULL", use_alter=True), index=True
    )
    last_seen_at: Mapped[datetime | None]
    # Stored in the site's session cookie at login; raising it ends every web session of the user.
    session_version: Mapped[int] = mapped_column(default=0, server_default=sql_text("0"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    profile: Mapped[Profile | None] = relationship(back_populates="user", passive_deletes=True)

    @property
    def display_name(self) -> str:
        """Telegram name for service texts; profile name is preferred where a profile exists."""
        return self.first_name or (f"@{self.username}" if self.username else f"#{self.id}")


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, user_fk(), primary_key=True)
    # Required fields
    gender: Mapped[Gender] = mapped_column(enum_type(Gender))
    name: Mapped[str] = mapped_column(String(40))
    birth_year: Mapped[int] = mapped_column(SmallInteger)
    city: Mapped[str] = mapped_column(String(60))
    marital_status: Mapped[MaritalStatus] = mapped_column(enum_type(MaritalStatus))
    children: Mapped[int] = mapped_column(SmallInteger)
    education: Mapped[Education] = mapped_column(enum_type(Education))
    employment: Mapped[Employment] = mapped_column(enum_type(Employment))
    wishes: Mapped[str] = mapped_column(Text)
    # Optional fields
    height: Mapped[int | None] = mapped_column(SmallInteger)
    weight: Mapped[int | None] = mapped_column(SmallInteger)
    occupation: Mapped[str | None] = mapped_column(String(100))
    nationality: Mapped[str | None] = mapped_column(String(50))
    madhab: Mapped[Madhab | None] = mapped_column(enum_type(Madhab))
    prayer: Mapped[Prayer | None] = mapped_column(enum_type(Prayer))
    nikah_timeline: Mapped[NikahTimeline | None] = mapped_column(enum_type(NikahTimeline))
    relocation: Mapped[Relocation | None] = mapped_column(enum_type(Relocation))
    about: Mapped[str | None] = mapped_column(Text)
    # Moderation
    status: Mapped[ProfileStatus] = mapped_column(
        enum_type(ProfileStatus), default=ProfileStatus.PENDING, server_default=ProfileStatus.PENDING.value
    )
    moderation_note: Mapped[str | None] = mapped_column(Text)
    flagged: Mapped[bool] = mapped_column(default=False, server_default=false())
    flag_reason: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None]
    # NULL while the current content has not been approved (new, edited or rejected).
    approved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="profile")

    __table_args__ = (
        Index("ix_profiles_catalog", "status", "gender", "birth_year"),
        # Matches the catalog's city filter expression (case- and ё-insensitive).
        Index("ix_profiles_city_key", sql_text("replace(lower(city), 'ё', 'е')")),
        Index("ix_profiles_pending", "submitted_at", postgresql_where=sql_text("status = 'pending'")),
    )


class Interest(Base):
    __tablename__ = "interests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    to_user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[InterestStatus] = mapped_column(
        enum_type(InterestStatus), default=InterestStatus.PENDING, server_default=InterestStatus.PENDING.value
    )
    flagged: Mapped[bool] = mapped_column(default=False, server_default=false())
    flag_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    decided_at: Mapped[datetime | None]

    from_user: Mapped[User] = relationship(foreign_keys=[from_user_id])
    to_user: Mapped[User] = relationship(foreign_keys=[to_user_id])

    __table_args__ = (
        CheckConstraint("from_user_id <> to_user_id", name="not_self"),
        # At most one pending/accepted interest per unordered pair.
        Index(
            "uq_interests_active_pair",
            sql_text("LEAST(from_user_id, to_user_id)"),
            sql_text("GREATEST(from_user_id, to_user_id)"),
            unique=True,
            postgresql_where=sql_text("status IN ('pending', 'accepted')"),
        ),
        Index("ix_interests_to_user_status", "to_user_id", "status"),
        Index("ix_interests_from_user_created", "from_user_id", "created_at"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Normalized pair: user_a_id < user_b_id. A side becomes NULL when that user deletes the account.
    user_a_id: Mapped[int | None] = mapped_column(BigInteger, user_fk("SET NULL"))
    user_b_id: Mapped[int | None] = mapped_column(BigInteger, user_fk("SET NULL"))
    created_from_interest_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("interests.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    last_message_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    closed_by_id: Mapped[int | None] = mapped_column(BigInteger, user_fk("SET NULL"), index=True)
    close_reason: Mapped[ConversationCloseReason | None] = mapped_column(enum_type(ConversationCloseReason))

    user_a: Mapped[User | None] = relationship(foreign_keys=[user_a_id])
    user_b: Mapped[User | None] = relationship(foreign_keys=[user_b_id])

    __table_args__ = (
        UniqueConstraint("user_a_id", "user_b_id"),
        CheckConstraint("user_a_id < user_b_id", name="ordered_pair"),
        Index("ix_conversations_user_b_id", "user_b_id"),
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def is_participant(self, user_id: int) -> bool:
        return user_id in (self.user_a_id, self.user_b_id)

    def partner_id(self, user_id: int) -> int | None:
        """The other participant's id, or None if they deleted their account."""
        return self.user_b_id if self.user_a_id == user_id else self.user_a_id


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    text: Mapped[str] = mapped_column(Text)
    origin: Mapped[MessageOrigin] = mapped_column(enum_type(MessageOrigin))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    read_at: Mapped[datetime | None]
    flagged: Mapped[bool] = mapped_column(default=False, server_default=false())
    flag_reason: Mapped[str | None] = mapped_column(Text)

    sender: Mapped[User] = relationship()
    conversation: Mapped[Conversation] = relationship()

    __table_args__ = (
        Index("ix_messages_conversation_id_id", "conversation_id", "id"),
        Index("ix_messages_unread", "conversation_id", "sender_id", postgresql_where=sql_text("read_at IS NULL")),
        Index("ix_messages_flagged", "created_at", postgresql_where=sql_text("flagged")),
        Index("ix_messages_sender_id", "sender_id"),
    )


class Guardian(Base):
    """A wali link: ``guardian_user`` receives copies of all of ``ward``'s conversations."""

    __tablename__ = "guardians"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ward_user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    guardian_user_id: Mapped[int | None] = mapped_column(BigInteger, user_fk())
    invite_token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[GuardianStatus] = mapped_column(
        enum_type(GuardianStatus), default=GuardianStatus.PENDING, server_default=GuardianStatus.PENDING.value
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime]
    linked_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    ward: Mapped[User] = relationship(foreign_keys=[ward_user_id])
    guardian: Mapped[User | None] = relationship(foreign_keys=[guardian_user_id])

    __table_args__ = (
        CheckConstraint("guardian_user_id <> ward_user_id", name="not_self"),
        Index("uq_guardians_active_ward", "ward_user_id", unique=True, postgresql_where=sql_text("status = 'active'")),
        Index("ix_guardians_ward_user_id", "ward_user_id"),
        Index("ix_guardians_guardian_user_id", "guardian_user_id"),
    )


class LoginToken(Base):
    """Short-lived site login token. Only the SHA-256 of the token is stored."""

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[LoginTokenKind] = mapped_column(enum_type(LoginTokenKind))
    user_id: Mapped[int | None] = mapped_column(BigInteger, user_fk())
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime]
    confirmed_at: Mapped[datetime | None]
    consumed_at: Mapped[datetime | None]

    __table_args__ = (
        Index("ix_login_tokens_user_id", "user_id"),
        Index("ix_login_tokens_expires_at", "expires_at"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    target_user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    message_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("messages.id", ondelete="SET NULL"))
    reason: Mapped[ReportReason] = mapped_column(enum_type(ReportReason))
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        enum_type(ReportStatus), default=ReportStatus.OPEN, server_default=ReportStatus.OPEN.value
    )
    resolution: Mapped[ReportAction | None] = mapped_column(enum_type(ReportAction))
    resolved_by_id: Mapped[int | None] = mapped_column(BigInteger, user_fk("SET NULL"), index=True)
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    reporter: Mapped[User] = relationship(foreign_keys=[reporter_id])
    target_user: Mapped[User] = relationship(foreign_keys=[target_user_id])
    message: Mapped[Message | None] = relationship()

    __table_args__ = (
        CheckConstraint("reporter_id <> target_user_id", name="not_self"),
        Index("ix_reports_status_created_at", "status", "created_at"),
        Index("ix_reports_reporter_id", "reporter_id"),
        Index("ix_reports_target_user_id", "target_user_id"),
        Index("ix_reports_message_id", "message_id"),
    )


class Outbox(Base):
    """Transactional outbox: one row per domain event, delivered to Telegram by the bot worker."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    attempts: Mapped[int] = mapped_column(default=0, server_default=sql_text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    # Set when delivery is abandoned after too many attempts.
    failed_at: Mapped[datetime | None]

    __table_args__ = (
        Index(
            "ix_outbox_pending", "next_attempt_at", postgresql_where=sql_text("sent_at IS NULL AND failed_at IS NULL")
        ),
        # Recent events of one kind (rate limits, e.g. profile submissions per hour).
        Index("ix_outbox_kind_created_at", "kind", "created_at"),
    )
