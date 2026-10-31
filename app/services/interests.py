"""Interests (отклики): pending -> accepted / declined / cancelled. Accepting opens a conversation.

Each direction of a pair has its own history (legacy bot: UNIQUE(from, to)): a sender may not
send the same person another interest after an accepted or declined one, only after withdrawing
(cancelling) their own; the reverse direction is independent. Accepting is refused when either
person already has MAX_OPEN_CONVERSATIONS open conversations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import utcnow
from app.config import get_settings
from app.enums import ConversationCloseReason, InterestStatus, ProfileStatus
from app.models import Conversation, Interest, Profile, User
from app.schemas import InterestIn, first_error
from app.services import chat, events
from app.services.errors import (
    ConsentRequired,
    InterestAlreadyExists,
    InterestBlockedByWitness,
    InterestDeclinedBefore,
    InterestLimitReached,
    InterestNotFound,
    InterestNotPending,
    InterestSentBefore,
    InterestToSelf,
    InvalidInput,
    OpenConversationLimit,
    ProfileNotFound,
    UserBanned,
)
from app.services.profiles import approved_content, get_profile, has_approved_content, require_active
from app.services.relations import lock_users, report_exists_between
from app.services.stopwords import flag_reason

LIMIT_WINDOW = timedelta(hours=24)


def _between(user_id: int, other_id: int) -> ColumnElement[bool]:
    return or_(
        and_(Interest.from_user_id == user_id, Interest.to_user_id == other_id),
        and_(Interest.from_user_id == other_id, Interest.to_user_id == user_id),
    )


@dataclass(frozen=True, slots=True)
class PairRelation:
    """What exists between the viewer and another user (for the buttons on a profile page)."""

    interest: Interest | None  # the latest interest in either direction
    conversation: Conversation | None  # the open one, else the latest closed one


async def get_relation(session: AsyncSession, viewer: User, other_user_id: int) -> PairRelation:
    interest = await session.scalar(
        select(Interest).where(_between(viewer.id, other_user_id)).order_by(Interest.id.desc()).limit(1)
    )
    conversation = await chat.get_conversation_between(session, viewer.id, other_user_id)
    return PairRelation(interest=interest, conversation=conversation)


async def send_interest(
    session: AsyncSession, from_user: User, to_user_id: int, message: str | None = None
) -> Interest:
    """Send an interest with an optional message (≤500 chars, no contacts).

    Rules: sender not banned, has given consent and has an ACTIVE profile (ProfileRequired /
    ProfilePending / ProfileRejected / ProfileHidden); the recipient's profile is ACTIVE, of the
    opposite gender, owner not banned, no report between the two (else ProfileNotFound); no pending
    interest in either direction and no open conversation for the pair (InterestAlreadyExists); no
    conversation of the pair closed by the witness (InterestBlockedByWitness); the sender's earlier
    interest to this person was not declined (InterestDeclinedBefore) or accepted
    (InterestSentBefore) — a cancelled one may be sent again; at most INTEREST_DAILY_LIMIT
    interests per sender in a rolling 24 hours (cancelled ones count).
    """
    if from_user.is_banned:
        raise UserBanned()
    if from_user.consent_at is None:
        raise ConsentRequired()
    if to_user_id == from_user.id:
        raise InterestToSelf()
    try:
        clean_message = InterestIn(message=message).message
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    # Serialize everything that concerns the two users (pair rules, the sender's daily limit).
    await lock_users(session, from_user.id, to_user_id)

    own = require_active(await get_profile(session, from_user.id))
    target = await get_profile(session, to_user_id)
    if (
        target is None
        or target.status != ProfileStatus.ACTIVE
        or target.gender != own.gender.opposite
        or target.user.is_banned
        or await report_exists_between(session, from_user.id, to_user_id)
    ):
        raise ProfileNotFound()

    pending = exists().where(_between(from_user.id, to_user_id), Interest.status == InterestStatus.PENDING)
    if await session.scalar(select(pending)) or await chat.get_open_conversation_between(
        session, from_user.id, to_user_id
    ):
        raise InterestAlreadyExists()
    low, high = sorted((from_user.id, to_user_id))
    closed_by_witness = exists().where(
        Conversation.user_a_id == low,
        Conversation.user_b_id == high,
        Conversation.close_reason == ConversationCloseReason.CLOSED_BY_WITNESS,
    )
    if await session.scalar(select(closed_by_witness)):
        raise InterestBlockedByWitness()
    earlier = set(
        (
            await session.scalars(
                select(Interest.status).where(
                    Interest.from_user_id == from_user.id,
                    Interest.to_user_id == to_user_id,
                    Interest.status.in_((InterestStatus.DECLINED, InterestStatus.ACCEPTED)),
                )
            )
        ).all()
    )
    if InterestStatus.DECLINED in earlier:
        raise InterestDeclinedBefore()
    if earlier:
        raise InterestSentBefore()
    sent_recently = await session.scalar(
        select(func.count())
        .select_from(Interest)
        .where(Interest.from_user_id == from_user.id, Interest.created_at > func.now() - LIMIT_WINDOW)
    )
    if (sent_recently or 0) >= get_settings().INTEREST_DAILY_LIMIT:
        raise InterestLimitReached()

    reason = flag_reason(clean_message)
    interest = Interest(
        from_user_id=from_user.id,
        to_user_id=to_user_id,
        message=clean_message,
        flagged=reason is not None,
        flag_reason=reason,
    )
    try:
        async with session.begin_nested():
            session.add(interest)
    except IntegrityError as exc:  # unique pending per direction (cannot happen under the locks)
        raise InterestAlreadyExists() from exc
    await events.emit(
        session,
        events.EventKind.INTEREST_CREATED,
        {"interest_id": interest.id, "from_user_id": from_user.id, "to_user_id": to_user_id},
        notify_user_ids=[to_user_id, from_user.id],
    )
    return interest


async def _lock_for_recipient(session: AsyncSession, interest_id: int, user: User) -> Interest:
    """The pending interest addressed to ``user``, locked after both users' rows (lock order)."""
    if user.is_banned:
        raise UserBanned()
    if user.consent_at is None:
        raise ConsentRequired()
    interest = await session.get(Interest, interest_id)
    if interest is None or interest.to_user_id != user.id:
        raise InterestNotFound()
    await lock_users(session, interest.from_user_id, interest.to_user_id)
    interest = await session.get(Interest, interest_id, with_for_update={"key_share": True}, populate_existing=True)
    if interest is None:
        raise InterestNotFound()
    if interest.status != InterestStatus.PENDING:
        raise InterestNotPending()
    return interest


async def accept(session: AsyncSession, interest_id: int, user: User) -> Conversation:
    """Recipient accepts: the interest becomes accepted and a conversation is opened.

    The sender's profile must have approved content: a profile a moderator rejected (or that waits
    for moderation after an edit) cannot gain a chat through interests sent before. Refused while
    the pair has an open conversation (InterestAlreadyExists) or either person has
    MAX_OPEN_CONVERSATIONS open conversations (OpenConversationLimit); both users' rows are locked
    first, so concurrent accepts cannot exceed the limit."""
    interest = await _lock_for_recipient(session, interest_id, user)
    sender = await get_profile(session, interest.from_user_id)
    if sender is None or sender.user.is_banned or not has_approved_content(sender):
        raise ProfileNotFound()
    if await chat.get_open_conversation_between(session, interest.from_user_id, interest.to_user_id):
        raise InterestAlreadyExists("У вас уже есть открытый чат с этим человеком.")
    open_counts = await chat.count_open_conversations(session, [interest.from_user_id, interest.to_user_id])
    if max(open_counts.values()) >= get_settings().MAX_OPEN_CONVERSATIONS:
        raise OpenConversationLimit()
    interest.status = InterestStatus.ACCEPTED
    interest.decided_at = utcnow()
    conversation = await chat.create_conversation(session, interest.from_user_id, interest.to_user_id, interest.id)
    await events.emit(
        session,
        events.EventKind.INTEREST_ACCEPTED,
        {
            "interest_id": interest.id,
            "conversation_id": conversation.id,
            "from_user_id": interest.from_user_id,
            "to_user_id": interest.to_user_id,
        },
        notify_user_ids=[interest.from_user_id, interest.to_user_id],
    )
    await events.emit(
        session,
        events.EventKind.WITNESS_CONVERSATION_OPENED,
        {
            "conversation_id": conversation.id,
            "interest_id": interest.id,
            "user_a_id": conversation.user_a_id,
            "user_b_id": conversation.user_b_id,
        },
    )
    return conversation


async def decline(session: AsyncSession, interest_id: int, user: User) -> Interest:
    """Recipient declines. The sender cannot send this person another interest afterwards."""
    interest = await _lock_for_recipient(session, interest_id, user)
    interest.status = InterestStatus.DECLINED
    interest.decided_at = utcnow()
    await session.flush()
    await events.emit(
        session,
        events.EventKind.INTEREST_DECLINED,
        {"interest_id": interest.id, "from_user_id": interest.from_user_id, "to_user_id": interest.to_user_id},
        notify_user_ids=[interest.from_user_id, interest.to_user_id],
    )
    return interest


async def cancel(session: AsyncSession, interest_id: int, user: User) -> Interest:
    """Sender withdraws a pending interest (no notification; allowed without consent). The sender
    may send this person a new interest afterwards."""
    interest = await session.get(Interest, interest_id, with_for_update={"key_share": True}, populate_existing=True)
    if interest is None or interest.from_user_id != user.id:
        raise InterestNotFound()
    if interest.status != InterestStatus.PENDING:
        raise InterestNotPending()
    interest.status = InterestStatus.CANCELLED
    interest.decided_at = utcnow()
    await session.flush()
    return interest


async def get_interest(session: AsyncSession, interest_id: int, user: User) -> Interest:
    """An interest ``user`` sent or received, with both users and their profiles loaded."""
    interest = await session.scalar(
        select(Interest)
        .where(Interest.id == interest_id)
        .options(
            selectinload(Interest.from_user).selectinload(User.profile),
            selectinload(Interest.to_user).selectinload(User.profile),
        )
    )
    if interest is None or user.id not in (interest.from_user_id, interest.to_user_id):
        raise InterestNotFound()
    return interest


async def list_incoming(
    session: AsyncSession, user: User, statuses: Sequence[InterestStatus] = (InterestStatus.PENDING,)
) -> list[Interest]:
    """Received interests (newest first), ``from_user.profile`` loaded. Interests of banned senders
    and of senders whose profile content is not approved (edited, rejected) are not shown."""
    result = await session.scalars(
        select(Interest)
        .join(User, User.id == Interest.from_user_id)
        .join(Profile, Profile.user_id == Interest.from_user_id)
        .where(
            Interest.to_user_id == user.id,
            Interest.status.in_(statuses),
            User.is_banned.is_(False),
            approved_content(),
        )
        .options(selectinload(Interest.from_user).selectinload(User.profile))
        .order_by(Interest.id.desc())
    )
    return list(result.all())


async def list_outgoing(session: AsyncSession, user: User, limit: int = 100) -> list[Interest]:
    """Sent interests in all statuses (newest first), ``to_user.profile`` loaded."""
    result = await session.scalars(
        select(Interest)
        .where(Interest.from_user_id == user.id)
        .options(selectinload(Interest.to_user).selectinload(User.profile))
        .order_by(Interest.id.desc())
        .limit(limit)
    )
    return list(result.all())


async def pending_incoming_count(session: AsyncSession, user: User) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Interest)
        .join(User, User.id == Interest.from_user_id)
        .join(Profile, Profile.user_id == Interest.from_user_id)
        .where(
            Interest.to_user_id == user.id,
            Interest.status == InterestStatus.PENDING,
            User.is_banned.is_(False),
            approved_content(),
        )
    )
    return count or 0
