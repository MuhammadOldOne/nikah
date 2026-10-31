"""Report («Пожаловаться») form handling shared by the profile page and the chat."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ReportReason
from app.models import User
from app.services import moderation
from app.services.errors import DomainError
from app.web.deps import flash


async def submit_report(
    request: Request, session: AsyncSession, user: User, target_user_id: int, message_id: int | None = None
) -> bool:
    """Create a report from the submitted form. Flashes the problem and returns False on failure."""
    form = await request.form()
    comment = form.get("comment")
    try:
        reason = ReportReason(str(form.get("reason", "")))
    except ValueError:
        flash(request, "Выберите причину жалобы.", "error")
        return False
    try:
        await moderation.create_report(
            session, user, target_user_id, reason, comment if isinstance(comment, str) else None, message_id
        )
    except DomainError as exc:
        flash(request, exc.message, "error")
        return False
    return True
