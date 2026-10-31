"""Outbox worker: delivers domain events to Telegram.

The worker wakes on every NOTIFY (EventListener callback) and at least every ``poll_interval``
seconds, locks due rows with FOR UPDATE SKIP LOCKED (several bot processes are safe), builds the
messages from current database data (payloads carry IDs only; rows that no longer exist are
skipped) and sends them. Delivery is at least once.

Building (DB reads + pure rendering in app.bot.render) is separate from sending.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeGuard

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import render
from app.bot.logutil import exc_summary
from app.db import session_scope
from app.enums import ConversationCloseReason, InterestStatus, ProfileStatus, ReportStatus
from app.models import Conversation, Interest, Message, Outbox, User
from app.services import auth, events, guardians, moderation, profiles, users
from app.services.events import Event, EventKind

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5.0
BATCH_SIZE = 20
SEND_DELAY = 0.05  # about 20 messages per second, below Telegram's limit of 30
MAX_FLOOD_RETRIES = 3
HOUSEKEEPING_INTERVAL = 6 * 60 * 60.0


@dataclass(frozen=True, slots=True)
class Delivery:
    telegram_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None = None

    @classmethod
    def of(cls, telegram_id: int, rendered: render.Rendered) -> Delivery:
        return cls(telegram_id, rendered.text, rendered.reply_markup)


def _notifiable(user: User | None) -> TypeGuard[User]:
    """Regular notifications honour the user's setting and never go to banned users."""
    return user is not None and not user.is_banned and user.notify_telegram


async def _message_created(session: AsyncSession, row: Outbox) -> list[Delivery]:
    payload = row.payload
    message = await session.get(Message, payload["message_id"])
    sender = await users.get_by_id(session, payload["sender_id"])
    if message is None or sender is None:
        return []  # deleted together with the sender's account
    recipient = await users.get_by_id(session, payload["recipient_id"])
    deliveries = []
    if _notifiable(recipient) and recipient.id != sender.id:  # never echo to the sender
        deliveries.append(Delivery.of(recipient.telegram_id, render.message_notification(sender, message)))
    participants = {sender.id: sender} | ({recipient.id: recipient} if recipient is not None else {})
    copied_to: set[int] = set()
    for link in payload.get("guardians", []):
        ward, guardian_id = participants.get(link["ward_user_id"]), link["guardian_user_id"]
        if ward is None or guardian_id in copied_to or guardian_id in participants:
            continue
        # The link may have been revoked or replaced since the message was written.
        active = await guardians.get_active_guardian(session, ward.id)
        if active is None or active.guardian_user_id != guardian_id or active.guardian is None:
            continue
        if active.guardian.is_banned:
            continue
        partner = recipient if ward.id == sender.id else sender
        deliveries.append(
            Delivery.of(active.guardian.telegram_id, render.guardian_copy(ward, partner, sender, message))
        )
        copied_to.add(guardian_id)
    return deliveries


async def _interest_created(session: AsyncSession, row: Outbox) -> list[Delivery]:
    interest = await session.get(Interest, row.payload["interest_id"])
    if interest is None or interest.status != InterestStatus.PENDING:
        return []
    recipient = await users.get_by_id(session, interest.to_user_id)
    sender = await users.get_by_id(session, interest.from_user_id)
    if not _notifiable(recipient) or sender is None or sender.is_banned or sender.profile is None:
        return []
    has_guardian = await guardians.has_active_guardian(session, sender.id)
    rendered = render.interest_notification(interest, sender.profile, has_guardian=has_guardian)
    return [Delivery.of(recipient.telegram_id, rendered)]


async def _interest_accepted(session: AsyncSession, row: Outbox) -> list[Delivery]:
    sender = await users.get_by_id(session, row.payload["from_user_id"])
    acceptor = await users.get_by_id(session, row.payload["to_user_id"])
    if not _notifiable(sender) or acceptor is None:
        return []
    return [Delivery.of(sender.telegram_id, render.interest_accepted(acceptor, row.payload["conversation_id"]))]


async def _interest_declined(session: AsyncSession, row: Outbox) -> list[Delivery]:
    sender = await users.get_by_id(session, row.payload["from_user_id"])
    decliner = await users.get_by_id(session, row.payload["to_user_id"])
    if not _notifiable(sender) or decliner is None:
        return []
    return [Delivery.of(sender.telegram_id, render.interest_declined(decliner))]


async def _conversation_closed(session: AsyncSession, row: Outbox) -> list[Delivery]:
    conversation = await session.get(Conversation, row.payload["conversation_id"])
    if conversation is None:
        return []
    reason = ConversationCloseReason(row.payload["reason"])
    deliveries = []
    for user_id in (conversation.user_a_id, conversation.user_b_id):
        if user_id is None or user_id == row.payload.get("closed_by_id"):
            continue
        recipient = await users.get_by_id(session, user_id)
        if not _notifiable(recipient):
            continue
        partner_id = conversation.partner_id(user_id)
        partner = await users.get_by_id(session, partner_id) if partner_id is not None else None
        deliveries.append(Delivery.of(recipient.telegram_id, render.conversation_closed(reason, partner)))
    return deliveries


async def _to_admins(session: AsyncSession, rendered: render.Rendered) -> list[Delivery]:
    return [Delivery.of(telegram_id, rendered) for telegram_id in await users.admin_telegram_ids(session)]


async def _profile_submitted(session: AsyncSession, row: Outbox) -> list[Delivery]:
    profile = await profiles.get_profile(session, row.payload["user_id"])
    if profile is None or profile.status != ProfileStatus.PENDING or profile.user.is_banned:
        return []
    if profile.submitted_at is not None and profile.submitted_at > row.created_at:
        return []  # re-submitted since: the newer event carries the current content
    return await _to_admins(session, render.moderation_card(profile))


async def _profile_moderated(session: AsyncSession, row: Outbox) -> list[Delivery]:
    profile = await profiles.get_profile(session, row.payload["user_id"])
    if profile is None or profile.status != row.payload["status"] or not _notifiable(profile.user):
        return []
    return [Delivery.of(profile.user.telegram_id, render.profile_moderated(profile))]


async def _report_created(session: AsyncSession, row: Outbox) -> list[Delivery]:
    report = await moderation.get_report(session, row.payload["report_id"])
    if report is None or report.status != ReportStatus.OPEN:
        return []
    return await _to_admins(session, render.report_card(report))


async def _guardian_linked(session: AsyncSession, row: Outbox) -> list[Delivery]:
    link = await guardians.get_active_guardian(session, row.payload["ward_user_id"])
    if link is None or link.id != row.payload["guardian_id"] or link.guardian is None:
        return []
    ward = await users.get_by_id(session, row.payload["ward_user_id"])
    if not _notifiable(ward):
        return []
    return [Delivery.of(ward.telegram_id, render.guardian_linked(link.guardian))]


_BUILDERS: dict[str, Callable[[AsyncSession, Outbox], Awaitable[list[Delivery]]]] = {
    EventKind.MESSAGE_CREATED: _message_created,
    EventKind.INTEREST_CREATED: _interest_created,
    EventKind.INTEREST_ACCEPTED: _interest_accepted,
    EventKind.INTEREST_DECLINED: _interest_declined,
    EventKind.CONVERSATION_CLOSED: _conversation_closed,
    EventKind.PROFILE_SUBMITTED: _profile_submitted,
    EventKind.PROFILE_MODERATED: _profile_moderated,
    EventKind.REPORT_CREATED: _report_created,
    EventKind.GUARDIAN_LINKED: _guardian_linked,
}


async def build_deliveries(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """Telegram messages for one outbox row, from the current state of the database."""
    builder = _BUILDERS.get(row.kind)
    if builder is None:
        logger.warning("outbox %s: unknown event kind %r, skipped", row.id, row.kind)
        return []
    return await builder(session, row)


class OutboxWorker:
    def __init__(
        self,
        bot: Bot,
        *,
        poll_interval: float = POLL_INTERVAL,
        batch_size: int = BATCH_SIZE,
        send_delay: float = SEND_DELAY,
    ) -> None:
        self._bot = bot
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._send_delay = send_delay
        self._wakeup = asyncio.Event()
        self._stopping = False

    def wake(self, _event: Event | None = None) -> None:
        """EventListener callback: process the outbox now."""
        self._wakeup.set()

    def stop(self) -> None:
        """Ask ``run`` to return after the current batch."""
        self._stopping = True
        self._wakeup.set()

    async def run(self) -> None:
        logger.info("outbox worker started")
        while not self._stopping:
            self._wakeup.clear()
            try:
                while await self.process_batch() >= self._batch_size and not self._stopping:
                    pass
            except Exception as exc:  # keep the worker alive; rows stay pending
                logger.error("outbox batch failed: %s", exc_summary(exc))
            if not self._stopping:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), self._poll_interval)
        logger.info("outbox worker stopped")

    async def process_batch(self) -> int:
        """Deliver one batch of due rows in one transaction; returns how many rows were taken."""
        async with session_scope() as session:
            rows = await events.fetch_pending(session, limit=self._batch_size)
            for row in rows:
                await self._process(session, row)
        return len(rows)

    async def _process(self, session: AsyncSession, row: Outbox) -> None:
        try:
            async with session.begin_nested():  # a failing query must not abort the whole batch
                deliveries = await build_deliveries(session, row)
        except Exception as exc:
            logger.error("outbox %s (%s): building messages failed: %s", row.id, row.kind, exc_summary(exc))
            await events.mark_failed(session, row, f"build: {type(exc).__name__}")
            return
        try:
            for delivery in deliveries:
                await self._send(row, delivery)
        except Exception as exc:  # network or server errors: retry the row later with backoff
            logger.warning("outbox %s (%s): delivery failed: %s", row.id, row.kind, type(exc).__name__)
            await events.mark_failed(session, row, f"send: {type(exc).__name__}")
            return
        await events.mark_sent(session, row)

    async def _send(self, row: Outbox, delivery: Delivery) -> None:
        """Send one message. Flood control: wait and retry. A recipient who blocked the bot or a
        message Telegram rejects is skipped (retrying cannot help). Other errors propagate."""
        flood_retries = 0
        while True:
            try:
                await self._bot.send_message(
                    chat_id=delivery.telegram_id, text=delivery.text, reply_markup=delivery.reply_markup
                )
            except TelegramRetryAfter as exc:
                flood_retries += 1
                if flood_retries > MAX_FLOOD_RETRIES:
                    raise
                logger.warning("outbox %s: flood control, retrying in %s s", row.id, exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramForbiddenError:
                logger.info("outbox %s (%s): recipient blocked the bot, skipped", row.id, row.kind)
            except (TelegramBadRequest, TelegramNotFound) as exc:
                logger.warning("outbox %s (%s): Telegram rejected the message: %.200s", row.id, row.kind, exc.message)
            break
        await asyncio.sleep(self._send_delay)


async def housekeeping(interval: float = HOUSEKEEPING_INTERVAL) -> None:
    """Periodically remove processed outbox rows and expired login tokens."""
    while True:
        try:
            async with session_scope() as session:
                rows = await events.purge_processed(session)
                tokens = await auth.purge_expired_tokens(session)
            logger.info("housekeeping: removed %d outbox rows and %d login tokens", rows, tokens)
        except Exception as exc:
            logger.error("housekeeping failed: %s", exc_summary(exc))
        await asyncio.sleep(interval)
