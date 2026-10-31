"""Interests (отклики): pending -> accepted / declined / cancelled. Accepting opens a conversation."""

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
from app.enums import InterestStatus, ProfileStatus
from app.models import Conversation, Interest, User
from app.schemas import InterestIn, first_error
from app.services import chat, events
from app.services.errors import (
    InterestAlreadyExists,
    InterestDeclinedBefore,
    InterestLimitReached,
    InterestNotFound,
    InterestNotPending,
    InterestToSelf,
    InvalidInput,
    ProfileNotActive,
    ProfileNotFound,
    UserBanned,
)
from app.services.profiles import get_profile
from app.services.relations import report_exists_between
from app.services.stopwords import flag_reason

LIMIT_WINDOW = timedelta(hours=24)
ACTIVE_STATUSES = (InterestStatus.PENDING, InterestStatus.ACCEPTED)


def _between(user_id: int, other_id: int) -> ColumnElement[bool]:
    return or_(
        and_(Interest.from_user_id == user_id, Interest.to_user_id == other_id),
        and_(Interest.from_user_id == other_id, Interest.to_user_id == user_id),
    )


@dataclass(frozen=True, slots=True)
class PairRelation:
    """What exists between the viewer and another user (for the buttons on a profile page)."""

    interest: Interest | None  # the latest interest in either direction
    conversation: Conversation | None


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

    Rules: sender not banned and has an ACTIVE profile; recipient's profile is ACTIVE, of the
    opposite gender, owner not banned, no report between the two; no pending/accepted interest or
    conversation for the pair in either direction; the recipient has not declined this sender
    before; at most INTEREST_DAILY_LIMIT interests per sender in a rolling 24 hours.
    """
    if from_user.is_banned:
        raise UserBanned()
    if to_user_id == from_user.id:
        raise InterestToSelf()
    try:
        clean_message = InterestIn(message=message).message
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    # Serialize sends per sender so the daily limit cannot be raced. NO KEY UPDATE does not block
    # the FK checks of concurrent inserts that reference this user (avoids deadlocks).
    await session.execute(select(User.id).where(User.id == from_user.id).with_for_update(key_share=True))

    own = await get_profile(session, from_user.id)
    if own is None or own.status != ProfileStatus.ACTIVE:
        raise ProfileNotActive()
    target = await get_profile(session, to_user_id)
    if (
        target is None
        or target.status != ProfileStatus.ACTIVE
        or target.gender != own.gender.opposite
        or target.user.is_banned
        or await report_exists_between(session, from_user.id, to_user_id)
    ):
        raise ProfileNotFound()

    active_interest = exists().where(_between(from_user.id, to_user_id), Interest.status.in_(ACTIVE_STATUSES))
    if await session.scalar(select(active_interest)) or await chat.get_conversation_between(
        session, from_user.id, to_user_id
    ):
        raise InterestAlreadyExists()
    declined = exists().where(
        Interest.from_user_id == from_user.id,
        Interest.to_user_id == to_user_id,
        Interest.status == InterestStatus.DECLINED,
    )
    if await session.scalar(select(declined)):
        raise InterestDeclinedBefore()
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
    except IntegrityError as exc:  # the recipient sent us an interest concurrently
        raise InterestAlreadyExists() from exc
    await events.emit(
        session,
        events.EventKind.INTEREST_CREATED,
        {"interest_id": interest.id, "from_user_id": from_user.id, "to_user_id": to_user_id},
        notify_user_ids=[to_user_id, from_user.id],
    )
    return interest


async def _lock_for_recipient(session: AsyncSession, interest_id: int, user: User) -> Interest:
    if user.is_banned:
        raise UserBanned()
    interest = await session.get(Interest, interest_id, with_for_update={"key_share": True}, populate_existing=True)
    if interest is None or interest.to_user_id != user.id:
        raise InterestNotFound()
    if interest.status != InterestStatus.PENDING:
        raise InterestNotPending()
    return interest


async def accept(session: AsyncSession, interest_id: int, user: User) -> Conversation:
    """Recipient accepts: the interest becomes accepted and a conversation is created."""
    interest = await _lock_for_recipient(session, interest_id, user)
    sender = await session.get(User, interest.from_user_id)
    if sender is None or sender.is_banned:
        raise ProfileNotFound()
    if await chat.get_conversation_between(session, interest.from_user_id, interest.to_user_id):
        raise InterestAlreadyExists()
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
    """Sender withdraws a pending interest (no notification)."""
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
    """Received interests (newest first) from non-banned senders, ``from_user.profile`` loaded."""
    result = await session.scalars(
        select(Interest)
        .join(User, User.id == Interest.from_user_id)
        .where(Interest.to_user_id == user.id, Interest.status.in_(statuses), User.is_banned.is_(False))
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
        .where(
            Interest.to_user_id == user.id,
            Interest.status == InterestStatus.PENDING,
            User.is_banned.is_(False),
        )
    )
    return count or 0
