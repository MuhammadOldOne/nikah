"""Reports (жалобы), report resolution and stop-word flags for moderators."""

from __future__ import annotations

from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.interfaces import LoaderOption

from app.clock import utcnow
from app.enums import MessageKind, ProfileStatus, ReportAction, ReportReason, ReportStatus
from app.models import Conversation, Interest, Message, Report, User
from app.schemas import ReportIn, first_error
from app.services import chat, events, profiles, users
from app.services.errors import (
    AlreadyReported,
    ConsentRequired,
    DomainError,
    InvalidInput,
    MessageNotFound,
    PermissionDenied,
    ReportAlreadyResolved,
    ReportLimitReached,
    ReportNotFound,
    ReportToSelf,
    UserBanned,
    UserNotFound,
)
from app.services.stopwords import find_stop_words

__all__ = [
    "create_report",
    "find_stop_words",
    "get_report",
    "list_flagged_messages",
    "list_reports",
    "resolve_report",
]

# Every report is sent to every admin in Telegram.
REPORT_DAILY_LIMIT = 5
REPORT_LIMIT_WINDOW = timedelta(hours=24)
HIDE_PROFILE_NOTE = "Анкета скрыта модератором по жалобе. Исправьте анкету, и она снова уйдёт на проверку."
REPORT_FLAG_REASON = "user report"
# Profile flag set by scripts/import_legacy.py: the legacy age gave a birth year outside 18–80, so
# it was clamped into the range and the profile was sent back to moderation.
LEGACY_AGE_FLAG_REASON = "legacy age"


async def create_report(
    session: AsyncSession,
    reporter: User,
    target_user_id: int,
    reason: ReportReason,
    comment: str | None = None,
    message_id: int | None = None,
) -> Report:
    """Report a user, or a message of theirs («Пожаловаться» under a chat message).

    - A message report needs only that the message is the target's (a participant message) in a
      conversation of the reporter (open or closed), and is accepted once per message. It is
      never limited otherwise and works without consent: a participant can always report a
      message in their own chat. The message is flagged ('user report').
    - Any other report needs consent and someone the reporter has met on the platform (a profile
      they can see in the catalog, or a person they exchanged an interest or a chat with); one
      open report per reporter/target pair, at most REPORT_DAILY_LIMIT such reports in 24 hours.

    Reported users disappear from each other's catalog. Emits ``report.created`` (admins) and
    ``witness.report`` (the witness chat).
    """
    if reporter.is_banned:
        raise UserBanned()
    if target_user_id == reporter.id:
        raise ReportToSelf()
    try:
        data = ReportIn(reason=reason, comment=comment)
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    # Serialize reports per reporter so the daily limit cannot be raced (see interests.send_interest).
    await session.execute(select(User.id).where(User.id == reporter.id).with_for_update(key_share=True))
    if await session.get(User, target_user_id) is None:
        raise UserNotFound()
    message: Message | None = None
    if message_id is not None:
        message = await session.get(Message, message_id, with_for_update={"key_share": True}, populate_existing=True)
        conversation = await session.get(Conversation, message.conversation_id) if message else None
        if (
            message is None
            or conversation is None
            or message.kind != MessageKind.USER
            or message.sender_id != target_user_id
            or not conversation.is_participant(reporter.id)
        ):
            raise MessageNotFound()
        duplicate = exists().where(Report.reporter_id == reporter.id, Report.message_id == message_id)
        if await session.scalar(select(duplicate)):
            raise AlreadyReported("Вы уже пожаловались на это сообщение. Свидетель и модератор его рассмотрят.")
    else:
        if reporter.consent_at is None:
            raise ConsentRequired()
        duplicate = exists().where(
            Report.reporter_id == reporter.id,
            Report.target_user_id == target_user_id,
            Report.status == ReportStatus.OPEN,
        )
        if await session.scalar(select(duplicate)):
            raise AlreadyReported()
        if not await _has_met(session, reporter, target_user_id):
            raise UserNotFound()
        reported_recently = await session.scalar(
            select(func.count())
            .select_from(Report)
            .where(
                Report.reporter_id == reporter.id,
                Report.message_id.is_(None),
                Report.created_at > func.now() - REPORT_LIMIT_WINDOW,
            )
        )
        if (reported_recently or 0) >= REPORT_DAILY_LIMIT:
            raise ReportLimitReached()
    report = Report(
        reporter_id=reporter.id,
        target_user_id=target_user_id,
        message_id=message_id,
        reason=data.reason,
        comment=data.comment,
    )
    session.add(report)
    if message is not None:
        chat.flag_message(message, REPORT_FLAG_REASON)
    await session.flush()
    await events.emit(
        session,
        events.EventKind.REPORT_CREATED,
        {
            "report_id": report.id,
            "reporter_id": reporter.id,
            "target_user_id": target_user_id,
            "message_id": message_id,
        },
    )
    await events.emit(session, events.EventKind.WITNESS_REPORT, {"report_id": report.id, "message_id": message_id})
    return report


async def _has_met(session: AsyncSession, reporter: User, target_user_id: int) -> bool:
    """An interest (any status) or a conversation between the two, or the target's profile is one
    the reporter may open (catalog)."""
    contact = or_(
        exists().where(
            or_(
                and_(Interest.from_user_id == reporter.id, Interest.to_user_id == target_user_id),
                and_(Interest.from_user_id == target_user_id, Interest.to_user_id == reporter.id),
            )
        ),
        exists().where(
            Conversation.user_a_id == min(reporter.id, target_user_id),
            Conversation.user_b_id == max(reporter.id, target_user_id),
        ),
    )
    if await session.scalar(select(contact)):
        return True
    try:
        await profiles.get_viewable_profile(session, reporter, target_user_id)
    except DomainError:
        return False
    return True


def _report_options() -> tuple[LoaderOption, ...]:
    return (
        selectinload(Report.reporter).selectinload(User.profile),
        selectinload(Report.target_user).selectinload(User.profile),
        selectinload(Report.message),
    )


async def get_report(session: AsyncSession, report_id: int) -> Report | None:
    """Report with ``reporter``, ``target_user`` (with profiles) and ``message`` loaded."""
    return await session.scalar(select(Report).where(Report.id == report_id).options(*_report_options()))


async def list_reports(
    session: AsyncSession, status: ReportStatus | None = ReportStatus.OPEN, *, limit: int = 50, offset: int = 0
) -> list[Report]:
    """Reports (oldest first for open ones, newest first otherwise), relations loaded."""
    stmt = select(Report).options(*_report_options())
    if status is not None:
        stmt = stmt.where(Report.status == status)
    order = Report.id if status == ReportStatus.OPEN else Report.id.desc()
    return list((await session.scalars(stmt.order_by(order).limit(limit).offset(offset))).all())


async def resolve_report(session: AsyncSession, report_id: int, admin: User, action: ReportAction) -> Report:
    """Admin decision on an open report:

    - dismiss: nothing else happens;
    - hide_profile: the target's profile is rejected with a standard note (owner may fix it);
    - ban_user: the target is banned and all open reports against them are resolved
      (``AdminBanDenied`` if the target is an administrator; nothing is written).
    """
    if not users.is_admin(admin):
        raise PermissionDenied()
    report = await session.get(Report, report_id, with_for_update={"key_share": True}, populate_existing=True)
    if report is None:
        raise ReportNotFound()
    if report.status != ReportStatus.OPEN:
        raise ReportAlreadyResolved()
    now = utcnow()
    if action == ReportAction.HIDE_PROFILE:
        profile = await profiles.get_profile(session, report.target_user_id)
        if profile is not None and profile.status != ProfileStatus.REJECTED:
            await profiles.reject(session, report.target_user_id, HIDE_PROFILE_NOTE)
    elif action == ReportAction.BAN_USER:
        target = await session.get(User, report.target_user_id)
        if target is not None:
            await users.ban(session, target)
        await session.execute(
            update(Report)
            .where(Report.target_user_id == report.target_user_id, Report.status == ReportStatus.OPEN)
            .values(
                status=ReportStatus.RESOLVED, resolution=ReportAction.BAN_USER, resolved_by_id=admin.id, resolved_at=now
            )
        )
    report.status = ReportStatus.RESOLVED
    report.resolution = action
    report.resolved_by_id = admin.id
    report.resolved_at = now
    await session.flush()
    return report


async def list_flagged_messages(session: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[Message]:
    """Flagged chat messages (stop words, contact filter, witness, reports), newest first,
    ``sender`` (with profile) loaded."""
    result = await session.scalars(
        select(Message)
        .where(Message.flagged.is_(True))
        .options(selectinload(Message.sender).selectinload(User.profile))
        .order_by(Message.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())
