"""Query helpers about the relation between two users, shared by several services."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from app.models import Conversation, Report


def report_between(user_id: int, other_id: int | ColumnElement[int] | QueryableAttribute[int]) -> ColumnElement[bool]:
    """True if either user has ever reported the other (any report status)."""
    return exists().where(
        or_(
            and_(Report.reporter_id == user_id, Report.target_user_id == other_id),
            and_(Report.reporter_id == other_id, Report.target_user_id == user_id),
        )
    )


async def report_exists_between(session: AsyncSession, user_id: int, other_id: int) -> bool:
    return bool(await session.scalar(select(report_between(user_id, other_id))))


async def open_conversation_partner_ids(session: AsyncSession, user_id: int) -> list[int]:
    """The other participants of the user's open conversations (deleted partners excluded)."""
    rows = await session.execute(
        select(Conversation.user_a_id, Conversation.user_b_id).where(
            or_(Conversation.user_a_id == user_id, Conversation.user_b_id == user_id), Conversation.closed_at.is_(None)
        )
    )
    return sorted({other for pair in rows.all() for other in pair if other is not None and other != user_id})
