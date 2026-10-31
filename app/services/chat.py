"""Conversations and messages. A conversation exists only after an interest was accepted.

A pair may have several conversations over time, at most one of them open. Every participant
message is copied to the witness (``witness.copy``); contacts in it are hidden first
(app.text.hide_contacts). Witness actions (flag, write, close) live in services.witness.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.interfaces import LoaderOption

from app.clock import utcnow
from app.config import get_settings
from app.db import affected_rows
from app.enums import ConversationCloseReason, MessageOrigin
from app.models import CONTACT_FILTER_REASON, Conversation, Interest, Message, Profile, User
from app.schemas import CHAT_MESSAGE_MAX, ChatMessageIn, first_error
from app.services import events, guardians
from app.services.errors import (
    ConsentRequired,
    ConversationClosed,
    ConversationNotFound,
    InvalidInput,
    MessageRateLimited,
    PartnerDeleted,
    UserBanned,
)
from app.services.stopwords import find_stop_words
from app.text import hide_contacts

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# CHAT_RATE_LIMIT_PER_MIN messages per sender in this window, across chats and channels: generous
# for people, a brake on floods (each message is a Telegram notification and a witness copy).
MESSAGE_RATE_WINDOW = timedelta(minutes=1)
# Shown to the sender (site and bot) when ``message.contacts_hidden``.
CONTACTS_HIDDEN_NOTICE = "Часть текста была скрыта фильтром (телефоны/ссылки/@). Свидетель уведомлён."
# What closing a chat means for the person closing it (decisions 5 and 17: only the sender of the
# accepted interest that opened it can never send this person an interest again).
CLOSE_NOTE_SENDER = (
    "Писать в этот чат будет нельзя. Отправить этой анкете отклик ещё раз вы не сможете: "
    "новый чат возможен, только если отклик отправит собеседник."
)
CLOSE_NOTE_ACCEPTOR = (
    "Писать в этот чат будет нельзя. Повторить свой отклик собеседник не сможет: "
    "новый чат возможен, только если отклик отправите вы."
)
CLOSE_NOTE_UNKNOWN = (  # chats moved from the legacy bot without their interest
    "Писать в этот чат будет нельзя. Принятый отклик повторить нельзя: "
    "новый чат возможен, только если отклик отправит тот, кто его не отправлял."
)


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    conversation: Conversation
    partner: User | None  # None if the partner deleted the account; ``partner.profile`` is loaded
    last_message: Message | None
    unread_count: int
    partner_has_guardian: bool

    @property
    def partner_profile(self) -> Profile | None:
        return self.partner.profile if self.partner is not None else None


def _participant_filter(user_id: int) -> ColumnElement[bool]:
    return or_(Conversation.user_a_id == user_id, Conversation.user_b_id == user_id)


def _with_participants() -> tuple[LoaderOption, ...]:
    return (
        selectinload(Conversation.user_a).selectinload(User.profile),
        selectinload(Conversation.user_b).selectinload(User.profile),
    )


def _pair(user_id: int, other_id: int) -> ColumnElement[bool]:
    low, high = sorted((user_id, other_id))
    return and_(Conversation.user_a_id == low, Conversation.user_b_id == high)


async def get_conversation_between(session: AsyncSession, user_id: int, other_id: int) -> Conversation | None:
    """The pair's open conversation, else its latest closed one, else None."""
    return await session.scalar(
        select(Conversation)
        .where(_pair(user_id, other_id))
        .order_by(Conversation.closed_at.is_not(None), Conversation.id.desc())
        .limit(1)
    )


async def get_open_conversation_between(session: AsyncSession, user_id: int, other_id: int) -> Conversation | None:
    return await session.scalar(select(Conversation).where(_pair(user_id, other_id), Conversation.closed_at.is_(None)))


async def open_conversations(session: AsyncSession, user: User) -> list[Conversation]:
    """The user's open conversations, most recently active first, ``user_a``/``user_b`` (with
    profiles) loaded. The bot sends plain text to the only one when there is exactly one."""
    result = await session.scalars(
        select(Conversation)
        .where(_participant_filter(user.id), Conversation.closed_at.is_(None))
        .options(*_with_participants())
        .order_by(func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(), Conversation.id.desc())
    )
    return list(result.all())


async def count_open_conversations(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """{user_id: number of open conversations} for each of ``user_ids`` (0 included)."""
    ids = set(user_ids)
    counts = dict.fromkeys(ids, 0)
    for column in (Conversation.user_a_id, Conversation.user_b_id):
        rows = await session.execute(
            select(column, func.count()).where(column.in_(ids), Conversation.closed_at.is_(None)).group_by(column)
        )
        for user_id, count in rows.all():
            counts[user_id] += count
    return counts


async def create_conversation(
    session: AsyncSession, user_id: int, other_id: int, interest_id: int | None = None
) -> Conversation:
    """Internal: used by interests.accept. The pair is stored ordered (user_a_id < user_b_id)."""
    low, high = sorted((user_id, other_id))
    conversation = Conversation(user_a_id=low, user_b_id=high, created_from_interest_id=interest_id)
    session.add(conversation)
    await session.flush()
    return conversation


async def list_conversations(session: AsyncSession, user: User) -> list[ConversationSummary]:
    """All of the user's conversations, most recently active first."""
    conversations = list(
        (
            await session.scalars(
                select(Conversation).where(_participant_filter(user.id)).options(*_with_participants())
            )
        ).all()
    )
    if not conversations:
        return []
    ids = [conversation.id for conversation in conversations]
    last_messages = {
        message.conversation_id: message
        for message in (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id.in_(ids))
                .ext(distinct_on(Message.conversation_id))
                .order_by(Message.conversation_id, Message.id.desc())
            )
        ).all()
    }
    unread = dict(
        (
            await session.execute(
                select(Message.conversation_id, func.count())
                .where(Message.conversation_id.in_(ids), Message.sender_id != user.id, Message.read_at.is_(None))
                .group_by(Message.conversation_id)
            )
        ).all()
    )
    partner_ids = {pid for c in conversations if (pid := c.partner_id(user.id)) is not None}
    with_guardian = await guardians.users_with_active_guardian(session, partner_ids)

    summaries = []
    for conversation in conversations:
        partner_id = conversation.partner_id(user.id)
        partner = conversation.user_a if conversation.user_a_id == partner_id else conversation.user_b
        summaries.append(
            ConversationSummary(
                conversation=conversation,
                partner=partner if partner_id is not None else None,
                last_message=last_messages.get(conversation.id),
                unread_count=unread.get(conversation.id, 0),
                partner_has_guardian=partner_id in with_guardian,
            )
        )
    summaries.sort(
        key=lambda s: (s.conversation.last_message_at or s.conversation.created_at, s.conversation.id),
        reverse=True,
    )
    return summaries


async def get_conversation_for_participant(session: AsyncSession, conversation_id: int, user: User) -> Conversation:
    """The conversation with ``user_a``/``user_b`` (and their profiles) loaded.

    Raises ConversationNotFound if it does not exist or ``user`` is not a participant.
    """
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id).options(*_with_participants())
    )
    if conversation is None or not conversation.is_participant(user.id):
        raise ConversationNotFound()
    return conversation


async def send_message(
    session: AsyncSession, conversation_id: int, sender: User, text: str, origin: MessageOrigin
) -> Message:
    """Add a message from a participant to an open conversation (both channels).

    Phones, links, t.me/…, @usernames and e-mails are replaced by «[скрыто свидетелем]» and the
    message is flagged (``flag_reason`` 'contact-leak filter'): check ``message.contacts_hidden``
    and show CONTACTS_HIDDEN_NOTICE to the sender. Stop words only flag. At most
    CHAT_RATE_LIMIT_PER_MIN messages per sender a minute. Emits ``message.created`` (recipient,
    walis, SSE) and ``witness.copy`` (the witness)."""
    if sender.is_banned:
        raise UserBanned()
    if sender.consent_at is None:
        raise ConsentRequired()
    conversation = await session.get(
        Conversation, conversation_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if conversation is None or not conversation.is_participant(sender.id):
        raise ConversationNotFound()
    partner_id = conversation.partner_id(sender.id)
    if partner_id is None:
        raise PartnerDeleted()
    if not conversation.is_open:
        raise ConversationClosed()
    try:
        clean = ChatMessageIn(text=text).text
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    sent_recently = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.sender_id == sender.id, Message.created_at > func.now() - MESSAGE_RATE_WINDOW)
    )
    if (sent_recently or 0) >= get_settings().CHAT_RATE_LIMIT_PER_MIN:
        raise MessageRateLimited()

    clean, hidden = hide_contacts(clean)
    if len(clean) > CHAT_MESSAGE_MAX:  # many short contacts: the placeholders made it longer
        raise InvalidInput("Сообщение слишком длинное: в нём много ссылок или номеров. Сократите его.")
    reasons = ([CONTACT_FILTER_REASON] if hidden else []) + find_stop_words(clean)
    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        text=clean,
        origin=origin,
        flagged=bool(reasons),
        flag_reason=", ".join(dict.fromkeys(reasons))[:500] or None,
    )
    session.add(message)
    conversation.last_message_at = utcnow()
    await session.flush()

    links = await guardians.active_guardians_for(session, [sender.id, partner_id])
    await events.emit(
        session,
        events.EventKind.MESSAGE_CREATED,
        {
            "conversation_id": conversation.id,
            "message_id": message.id,
            "sender_id": sender.id,
            "recipient_id": partner_id,
            "origin": origin.value,
            "guardians": [
                {"ward_user_id": ward_id, "guardian_user_id": guardian_id}
                for ward_id, guardian_id in sorted(links.items())
                if guardian_id not in (sender.id, partner_id)
            ],
        },
        notify_user_ids=[sender.id, partner_id],
    )
    await events.emit(
        session,
        events.EventKind.WITNESS_COPY,
        {
            "conversation_id": conversation.id,
            "message_id": message.id,
            "sender_id": sender.id,
            "recipient_id": partner_id,
            "origin": origin.value,
        },
    )
    return message


def flag_message(message: Message, reason: str) -> None:
    """Flag ``message`` for the moderators, appending ``reason`` to its comma-separated
    ``flag_reason`` once (the caller flushes)."""
    reasons = [part for part in (message.flag_reason or "").split(", ") if part]
    if reason not in reasons:
        reasons.append(reason)
    message.flagged = True
    message.flag_reason = ", ".join(reasons)[:500]


async def list_messages(
    session: AsyncSession,
    conversation: Conversation,
    *,
    after_id: int | None = None,
    before_id: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[Message]:
    """Messages in ascending id order.

    - ``after_id``: the next ``limit`` messages after it (catch-up after SSE reconnects);
    - otherwise the latest ``limit`` messages (before ``before_id`` if given, to load history).
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    stmt = select(Message).where(Message.conversation_id == conversation.id)
    if after_id is not None:
        stmt = stmt.where(Message.id > after_id).order_by(Message.id).limit(limit)
        return list((await session.scalars(stmt)).all())
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    latest = (await session.scalars(stmt.order_by(Message.id.desc()).limit(limit))).all()
    return list(reversed(latest))


async def mark_read(session: AsyncSession, conversation: Conversation, reader: User) -> int:
    """Mark the partner's messages as read. Returns how many were updated."""
    result = await session.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.sender_id != reader.id,
            Message.read_at.is_(None),
        )
        .values(read_at=utcnow())
    )
    return affected_rows(result)


async def force_close(
    session: AsyncSession,
    conversation: Conversation,
    reason: ConversationCloseReason,
    closed_by_id: int | None = None,
) -> bool:
    """Internal: close without permission checks (user actions, bans, deletions, the witness).
    Emits ``conversation.closed`` (participants) and ``witness.conversation_closed``.

    Returns False (and changes nothing) when a concurrent close got there first: the row is
    re-read under a lock, so two closes never both succeed and the first reason stays (a witness
    close is never overwritten by a user's close a moment later)."""
    # Lock order: users rows before the conversation row, as when a message is sent (the bot and
    # the site update the sender's last_seen_at first). Changing the conversation first would let
    # autoflush lock it before the users rows, and a reply sent at the same moment would deadlock.
    await session.execute(
        update(User).where(User.bot_active_conversation_id == conversation.id).values(bot_active_conversation_id=None)
    )
    locked = await session.get(
        Conversation, conversation.id, with_for_update={"key_share": True}, populate_existing=True
    )
    if locked is None or not locked.is_open:
        return False
    conversation.closed_at = utcnow()
    conversation.closed_by_id = closed_by_id
    conversation.close_reason = reason
    participants = [uid for uid in (conversation.user_a_id, conversation.user_b_id) if uid is not None]
    await session.flush()
    payload = {"conversation_id": conversation.id, "closed_by_id": closed_by_id, "reason": reason.value}
    await events.emit(session, events.EventKind.CONVERSATION_CLOSED, payload, notify_user_ids=participants)
    await events.emit(session, events.EventKind.WITNESS_CONVERSATION_CLOSED, payload)
    return True


async def close_note(session: AsyncSession, conversation: Conversation, user: User) -> str:
    """The confirmation text before ``user`` closes ``conversation``, by their role in it."""
    interest = (
        await session.get(Interest, conversation.created_from_interest_id)
        if conversation.created_from_interest_id is not None
        else None
    )
    if interest is None:
        return CLOSE_NOTE_UNKNOWN
    return CLOSE_NOTE_SENDER if interest.from_user_id == user.id else CLOSE_NOTE_ACCEPTOR


async def close_conversation(session: AsyncSession, conversation: Conversation, user: User) -> None:
    """«Завершить общение»: any participant may close an open conversation (also without consent)."""
    if not conversation.is_participant(user.id):
        raise ConversationNotFound()
    if not conversation.is_open or not await force_close(
        session, conversation, ConversationCloseReason.CLOSED_BY_USER, closed_by_id=user.id
    ):
        raise ConversationClosed()


async def unread_total(session: AsyncSession, user: User) -> int:
    """Unread messages addressed to ``user`` across all conversations."""
    count = await session.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(_participant_filter(user.id), Message.sender_id != user.id, Message.read_at.is_(None))
    )
    return count or 0


async def set_bot_active_conversation(session: AsyncSession, user: User, conversation_id: int | None) -> None:
    """Select the chat the user replies to in the bot («Ответить» / «💬 Чаты»); None = /stop."""
    if conversation_id is not None:
        conversation = await get_conversation_for_participant(session, conversation_id, user)
        if conversation.partner_id(user.id) is None:
            raise PartnerDeleted()
        if not conversation.is_open:
            raise ConversationClosed()
    user.bot_active_conversation_id = conversation_id
    await session.flush()
