"""SQLAlchemy models (spec section 6, docs/LEGACY_BOT.md).

Conventions:
- The public identifier of a person is the profile number ``profiles.number`` («Анкета №512»,
  ``/p/{number}``, bot ``p_<number>``); ``users.id`` is internal. A profile's primary key is its
  ``user_id``.
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
    DDL,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Sequence,
    SmallInteger,
    String,
    Text,
    event,
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
    MessageKind,
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


# Public profile numbers continue the legacy bot's numbering (its first profile was №500).
PROFILE_NUMBER_START = 500
PROFILE_NUMBER_SEQ = Sequence("profiles_number_seq", start=PROFILE_NUMBER_START, metadata=Base.metadata)
# Message flag reasons written by services (stop words are added as a comma-separated list).
CONTACT_FILTER_REASON = "contact-leak filter"


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
    # «Анкета №N»: from PROFILE_NUMBER_SEQ; migrated profiles keep their legacy number.
    number: Mapped[int] = mapped_column(
        Integer, PROFILE_NUMBER_SEQ, server_default=PROFILE_NUMBER_SEQ.next_value(), unique=True
    )
    # Required fields. Marital status, children, education and employment are NULL in profiles
    # migrated from the legacy bot (it did not ask them): such a profile is incomplete
    # (profiles.missing_fields); new profiles must fill them (schemas.ProfileCreate).
    gender: Mapped[Gender] = mapped_column(enum_type(Gender))
    birth_year: Mapped[int] = mapped_column(SmallInteger)
    city: Mapped[str] = mapped_column(String(60))
    marital_status: Mapped[MaritalStatus | None] = mapped_column(enum_type(MaritalStatus))
    children: Mapped[int | None] = mapped_column(SmallInteger)
    education: Mapped[Education | None] = mapped_column(enum_type(Education))
    employment: Mapped[Employment | None] = mapped_column(enum_type(Employment))
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

    # ``number`` comes from the database: fetch it with the INSERT (RETURNING), no lazy load later.
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        Index("ix_profiles_catalog", "status", "gender", "birth_year"),
        # Matches the catalog's city filter expression (case- and ё-insensitive).
        Index("ix_profiles_city_key", sql_text("replace(lower(city), 'ё', 'е')")),
        Index("ix_profiles_pending", "submitted_at", postgresql_where=sql_text("status = 'pending'")),
    )


# create_all (tests) mirrors the migration: the sequence belongs to the column, so it is dropped with
# it and ``TRUNCATE … RESTART IDENTITY`` restarts the numbering at 500.
event.listen(
    Profile.__table__,
    "after_create",
    DDL("ALTER SEQUENCE profiles_number_seq OWNED BY profiles.number").execute_if(dialect="postgresql"),
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

    # Admits every state of the legacy bot (UNIQUE(from, to) there): each direction of a pair has
    # its own history, both directions may be pending at once, and a pair may have had several
    # chats. The remaining rules (no resend after a decline/accept, …) live in services.interests.
    __table_args__ = (
        CheckConstraint("from_user_id <> to_user_id", name="not_self"),
        Index(
            "uq_interests_pending_direction",
            "from_user_id",
            "to_user_id",
            unique=True,
            postgresql_where=sql_text("status = 'pending'"),
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

    # A pair may have several conversations over time, but only one open at a time.
    __table_args__ = (
        CheckConstraint("user_a_id < user_b_id", name="ordered_pair"),
        Index(
            "uq_conversations_open_pair",
            "user_a_id",
            "user_b_id",
            unique=True,
            postgresql_where=sql_text("closed_at IS NULL"),
        ),
        Index("ix_conversations_pair", "user_a_id", "user_b_id"),
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
    # USER messages have a sender; WITNESS messages (written by the witness to both participants)
    # have none.
    kind: Mapped[MessageKind] = mapped_column(
        enum_type(MessageKind), default=MessageKind.USER, server_default=MessageKind.USER.value
    )
    sender_id: Mapped[int | None] = mapped_column(BigInteger, user_fk())
    text: Mapped[str] = mapped_column(Text)
    origin: Mapped[MessageOrigin] = mapped_column(enum_type(MessageOrigin))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    read_at: Mapped[datetime | None]
    flagged: Mapped[bool] = mapped_column(default=False, server_default=false())
    flag_reason: Mapped[str | None] = mapped_column(Text)

    sender: Mapped[User | None] = relationship()
    conversation: Mapped[Conversation] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(kind = 'user' AND sender_id IS NOT NULL) OR (kind = 'witness' AND sender_id IS NULL)",
            name="kind_sender",
        ),
        Index("ix_messages_conversation_id_id", "conversation_id", "id"),
        Index("ix_messages_unread", "conversation_id", "sender_id", postgresql_where=sql_text("read_at IS NULL")),
        Index("ix_messages_flagged", "created_at", postgresql_where=sql_text("flagged")),
        Index("ix_messages_sender_id", "sender_id"),
    )

    @property
    def is_witness(self) -> bool:
        return self.kind == MessageKind.WITNESS

    @property
    def contacts_hidden(self) -> bool:
        """The contact filter replaced phones/links/@usernames in this message (chat.send_message)."""
        return CONTACT_FILTER_REASON in (self.flag_reason or "").split(", ")


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


class AppMeta(Base):
    """Small key-value store for one-off facts about the installation (services.meta), e.g. the
    completed legacy import (key ``legacy_import``)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())


class BotDialogState(Base):
    """The bot's dialog state (aiogram FSM, app.bot.storage): kept in the database so a dialog in
    progress (a report comment, an interest message, a profile edit) survives a restart instead of
    letting its next text fall through to the chat relay. Rows unused for a while are ignored and
    purged; the person's rows are deleted with the account."""

    __tablename__ = "bot_dialog_states"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sql_text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now(), index=True)
