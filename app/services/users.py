"""Accounts: Telegram identity, consent, notifications, bans and account deletion."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import utcnow
from app.config import get_settings
from app.enums import ConversationCloseReason, GuardianStatus, InterestStatus
from app.models import BotDialogState, Conversation, Guardian, Interest, Profile, User
from app.services import chat
from app.services.errors import AdminBanDenied, ConsentRequired, InvalidInput
from app.services.relations import lock_users

LAST_SEEN_INTERVAL = timedelta(minutes=1)
_PROFILE_REF = re.compile(r"(?:№|#)\s*(\d{1,9})")
_TELEGRAM_ID = re.compile(r"\d{1,19}")
USER_REF_HINT = "Укажите Telegram ID (только цифры) или номер анкеты: №512 или #512."


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    """The user with ``profile`` loaded, or None."""
    return await session.scalar(select(User).where(User.id == user_id).options(selectinload(User.profile)))


async def get_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    """The user with ``profile`` loaded, or None."""
    return await session.scalar(select(User).where(User.telegram_id == telegram_id).options(selectinload(User.profile)))


async def get_or_create_by_telegram(
    session: AsyncSession, telegram_id: int, username: str | None, first_name: str | None
) -> User:
    """Find the account by Telegram id (creating it on first contact) and refresh its
    username/first name. Safe under concurrent calls. ``profile`` is loaded."""
    username = username[:64] if username else None
    first_name = first_name[:128] if first_name else None
    user = await get_by_telegram_id(session, telegram_id)
    if user is None:
        # ON CONFLICT makes concurrent first contacts (e.g. two quick updates) safe.
        stmt = insert(User).values(telegram_id=telegram_id, username=username, first_name=first_name)
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={"username": stmt.excluded.username, "first_name": stmt.excluded.first_name},
        )
        user = (await session.scalars(stmt.returning(User), execution_options={"populate_existing": True})).one()
        await session.refresh(user, ["profile"])
    elif (user.username, user.first_name) != (username, first_name):
        user.username = username
        user.first_name = first_name
        await session.flush()
    return user


async def set_consent(session: AsyncSession, user: User) -> None:
    """Record consent to personal data processing (first consent time is kept)."""
    if user.consent_at is None:
        user.consent_at = utcnow()
        await session.flush()


def needs_consent(user: User) -> bool:
    """No consent to personal data processing recorded yet: accounts migrated from the legacy bot
    (152-FZ: separate consent, asked at the first visit) and new accounts before the profile form.

    Until it is given the person may read rules/help/legal pages, give consent, receive
    notifications, read their own chats and interests, withdraw (cancel an interest, close a chat,
    hide the profile, report a message in their own chat) and delete the account; every other
    action raises ConsentRequired (docs/ARCHITECTURE.md §7)."""
    return user.consent_at is None


def require_consent(user: User) -> None:
    if needs_consent(user):
        raise ConsentRequired()


async def set_notify(session: AsyncSession, user: User, enabled: bool) -> None:
    """Turn Telegram notifications on or off."""
    user.notify_telegram = enabled
    await session.flush()


async def touch_last_seen(session: AsyncSession, user: User) -> None:
    """Update ``last_seen_at`` at most once a minute (avoids a write per request)."""
    now = utcnow()
    if user.last_seen_at is None or now - user.last_seen_at >= LAST_SEEN_INTERVAL:
        user.last_seen_at = now
        await session.flush()


async def end_web_sessions(session: AsyncSession, user: User) -> None:
    """Log the user out of the website on every device (site sessions are signed cookies, so
    they are invalidated by raising ``session_version``). The bot is not affected."""
    user.session_version += 1
    await session.flush()


async def resolve_user_ref(session: AsyncSession, ref: str) -> User | None:
    """The user an admin command names (/ban, /unban): a Telegram id (digits, as in the legacy bot)
    or a profile number «№512» / «#512». ``profile`` loaded; None if nobody matches.

    Raises InvalidInput (USER_REF_HINT) for anything else."""
    value = ref.strip()
    if match := _PROFILE_REF.fullmatch(value):
        return await session.scalar(
            select(User)
            .join(Profile, Profile.user_id == User.id)
            .where(Profile.number == int(match.group(1)))
            .options(selectinload(User.profile))
        )
    if _TELEGRAM_ID.fullmatch(value) and int(value) < 2**63:
        return await get_by_telegram_id(session, int(value))
    raise InvalidInput(USER_REF_HINT)


def is_admin(user: User) -> bool:
    return user.is_admin or user.telegram_id in get_settings().ADMIN_TELEGRAM_IDS


async def admin_telegram_ids(session: AsyncSession) -> list[int]:
    """Telegram ids to notify about moderation: ADMIN_TELEGRAM_IDS plus users flagged is_admin."""
    flagged = await session.scalars(select(User.telegram_id).where(User.is_admin.is_(True), User.is_banned.is_(False)))
    return sorted(set(get_settings().ADMIN_TELEGRAM_IDS) | set(flagged.all()))


async def _open_conversations(session: AsyncSession, user_id: int) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation).where(
            or_(Conversation.user_a_id == user_id, Conversation.user_b_id == user_id),
            Conversation.closed_at.is_(None),
        )
    )
    return list(result.all())


async def ban(session: AsyncSession, user: User) -> None:
    """Block the account: hidden from the catalog, pending interests cancelled, open
    conversations closed (partners are notified), guardian links revoked.

    Administrators cannot be banned (in any channel): their rights must be revoked first."""
    if is_admin(user):
        raise AdminBanDenied()
    if user.is_banned:
        return
    now = utcnow()
    user.is_banned = True
    user.banned_at = now
    await session.execute(
        update(Interest)
        .where(
            or_(Interest.from_user_id == user.id, Interest.to_user_id == user.id),
            Interest.status == InterestStatus.PENDING,
        )
        .values(status=InterestStatus.CANCELLED, decided_at=now)
    )
    await session.execute(
        update(Guardian)
        .where(
            or_(Guardian.guardian_user_id == user.id, Guardian.ward_user_id == user.id),
            Guardian.status.in_((GuardianStatus.ACTIVE, GuardianStatus.PENDING)),
        )
        .values(status=GuardianStatus.REVOKED, revoked_at=now)
    )
    for conversation in await _open_conversations(session, user.id):
        await chat.force_close(session, conversation, ConversationCloseReason.USER_BANNED)
    await session.flush()


async def unban(session: AsyncSession, user: User) -> None:
    """Lift the ban. Closed conversations and cancelled interests stay as they are, and web
    sessions opened before the ban stay ended (the person logs in again)."""
    user.is_banned = False
    user.banned_at = None
    user.session_version += 1
    await session.flush()


async def delete_account(session: AsyncSession, user: User) -> None:
    """Delete the user and all their data (152-FZ). Open conversations are closed for the
    partners first («собеседник удалил аккаунт»); the user's messages are deleted.

    Afterwards the session is cleared (``expunge_all``): do not use previously loaded objects.
    """
    user_id = user.id
    # Lock the users row first (lock order, §3): an accept that is opening a chat with this user
    # either commits before (and its conversation is closed below) or waits and then finds no user.
    await lock_users(session, user_id)
    conversations = await session.scalars(
        select(Conversation).where(or_(Conversation.user_a_id == user_id, Conversation.user_b_id == user_id))
    )
    orphaned: list[int] = []
    for conversation in conversations.all():
        if conversation.partner_id(user_id) is None:
            orphaned.append(conversation.id)
        elif conversation.is_open:
            await chat.force_close(session, conversation, ConversationCloseReason.ACCOUNT_DELETED)
    if orphaned:
        await session.execute(delete(Conversation).where(Conversation.id.in_(orphaned)))
    # Dialog drafts in the bot (profile texts, a report comment…) are keyed by the Telegram id.
    await session.execute(delete(BotDialogState).where(BotDialogState.telegram_user_id == user.telegram_id))
    await session.flush()
    await session.execute(delete(User).where(User.id == user_id))
    session.expunge_all()
