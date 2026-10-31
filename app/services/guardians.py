"""Wali (guardian) links: invite by deep link, accept in the bot, revoke from settings."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import utcnow
from app.config import get_settings
from app.db import affected_rows
from app.enums import GuardianStatus
from app.models import Guardian, User
from app.services import events
from app.services.errors import GuardianInviteInvalid, GuardianSelf, UserBanned
from app.services.relations import open_conversation_partner_ids

INVITE_TTL = timedelta(days=7)
INVITE_PREFIX = "wali_"


def invite_link(token: str) -> str:
    return f"https://t.me/{get_settings().BOT_USERNAME}?start={INVITE_PREFIX}{token}"


async def create_invite(session: AsyncSession, ward: User) -> str:
    """Create a new invite (previous pending invites are revoked) and return its deep link.

    The current active guardian, if any, stays until the new one accepts.
    """
    if ward.is_banned:
        raise UserBanned()
    now = utcnow()
    await session.execute(
        update(Guardian)
        .where(Guardian.ward_user_id == ward.id, Guardian.status == GuardianStatus.PENDING)
        .values(status=GuardianStatus.REVOKED, revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    session.add(Guardian(ward_user_id=ward.id, invite_token=token, expires_at=now + INVITE_TTL))
    await session.flush()
    return invite_link(token)


async def get_pending_invite(session: AsyncSession, ward: User) -> Guardian | None:
    """The ward's unexpired pending invite (to show its link again in settings)."""
    return await session.scalar(
        select(Guardian)
        .where(
            Guardian.ward_user_id == ward.id,
            Guardian.status == GuardianStatus.PENDING,
            Guardian.expires_at > utcnow(),
        )
        .order_by(Guardian.id.desc())
        .limit(1)
    )


async def get_invite(session: AsyncSession, token: str) -> Guardian | None:
    """A valid pending invite with ``ward`` and ``ward.profile`` loaded (bot confirmation screen)."""
    return await session.scalar(
        select(Guardian)
        .where(
            Guardian.invite_token == token,
            Guardian.status == GuardianStatus.PENDING,
            Guardian.expires_at > utcnow(),
        )
        .options(selectinload(Guardian.ward).selectinload(User.profile))
    )


async def accept_invite(session: AsyncSession, token: str, guardian_user: User) -> Guardian:
    """Link ``guardian_user`` as the ward's active guardian, replacing a previous one."""
    invite = await session.scalar(
        select(Guardian)
        .where(Guardian.invite_token == token)
        .options(selectinload(Guardian.ward))
        .with_for_update(of=Guardian, key_share=True)
        .execution_options(populate_existing=True)
    )
    now = utcnow()
    if invite is None or invite.status != GuardianStatus.PENDING or invite.expires_at <= now or invite.ward.is_banned:
        raise GuardianInviteInvalid()
    if invite.ward_user_id == guardian_user.id:
        raise GuardianSelf()
    if guardian_user.is_banned:
        raise UserBanned()
    # Revoke the previous guardian first: at most one active guardian per ward (unique index).
    await session.execute(
        update(Guardian)
        .where(Guardian.ward_user_id == invite.ward_user_id, Guardian.status == GuardianStatus.ACTIVE)
        .values(status=GuardianStatus.REVOKED, revoked_at=now)
    )
    invite.status = GuardianStatus.ACTIVE
    invite.guardian_user_id = guardian_user.id
    invite.linked_at = now
    await session.flush()
    # The ward's chat partners are told at once (open chat pages and the bot): from now on their
    # messages are copied to the wali.
    partners = await open_conversation_partner_ids(session, invite.ward_user_id)
    await events.emit(
        session,
        events.EventKind.GUARDIAN_LINKED,
        {"guardian_id": invite.id, "ward_user_id": invite.ward_user_id, "guardian_user_id": guardian_user.id},
        notify_user_ids=[invite.ward_user_id, guardian_user.id, *partners],
    )
    return invite


async def revoke(session: AsyncSession, ward: User) -> bool:
    """Disconnect the active guardian and cancel pending invites. Returns True if anything changed."""
    result = await session.execute(
        update(Guardian)
        .where(
            Guardian.ward_user_id == ward.id,
            Guardian.status.in_((GuardianStatus.ACTIVE, GuardianStatus.PENDING)),
        )
        .values(status=GuardianStatus.REVOKED, revoked_at=utcnow())
    )
    return affected_rows(result) > 0


async def get_active_guardian(session: AsyncSession, ward_user_id: int) -> Guardian | None:
    """The ward's active link with ``guardian`` (the guardian's User) loaded."""
    return await session.scalar(
        select(Guardian)
        .where(Guardian.ward_user_id == ward_user_id, Guardian.status == GuardianStatus.ACTIVE)
        .options(selectinload(Guardian.guardian))
    )


async def active_guardians_for(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """{ward_user_id: guardian_user_id} for those of ``user_ids`` who have an active guardian."""
    ids = set(user_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(Guardian.ward_user_id, Guardian.guardian_user_id).where(
            Guardian.ward_user_id.in_(ids), Guardian.status == GuardianStatus.ACTIVE
        )
    )
    return {ward_id: guardian_id for ward_id, guardian_id in rows.all() if guardian_id is not None}


async def users_with_active_guardian(session: AsyncSession, user_ids: Iterable[int]) -> set[int]:
    """Batch variant of has_active_guardian for lists (badge «Вали подключён»)."""
    return set(await active_guardians_for(session, user_ids))


async def has_active_guardian(session: AsyncSession, user_id: int) -> bool:
    return user_id in await users_with_active_guardian(session, [user_id])


async def list_wards(session: AsyncSession, guardian_user: User) -> list[Guardian]:
    """Active links where ``guardian_user`` is the guardian, with ``ward`` and ``ward.profile`` loaded."""
    result = await session.scalars(
        select(Guardian)
        .where(Guardian.guardian_user_id == guardian_user.id, Guardian.status == GuardianStatus.ACTIVE)
        .options(selectinload(Guardian.ward).selectinload(User.profile))
        .order_by(Guardian.linked_at)
    )
    return list(result.all())
