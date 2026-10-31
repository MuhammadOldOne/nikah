"""Outbox worker: delivers domain events to Telegram.

The worker wakes on every NOTIFY (EventListener callback) and at least every ``poll_interval``
seconds, locks due rows with FOR UPDATE SKIP LOCKED (several bot processes are safe), builds the
messages from current database data (payloads carry IDs only; rows that no longer exist are
skipped) and sends them. Delivery is at least once.

When Telegram cannot be reached (network or server errors, persistent flood control), the batch
stops at that row and the worker pauses with a growing delay: an outage is not the row's fault,
so no attempt is counted and nothing is given up, however long it lasts, and later messages never
overtake earlier ones. Attempts (and giving up after events.MAX_ATTEMPTS) are for failures of
one row only, such as a message that cannot be built.

Building (DB reads + pure rendering in app.bot.render) is separate from sending.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeGuard

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import render
from app.config import get_settings
from app.db import session_scope
from app.enums import ConversationCloseReason, InterestStatus, ProfileStatus, ReportStatus
from app.logutil import exc_summary
from app.models import Conversation, Interest, Message, Outbox, User
from app.services import auth, chat, events, guardians, moderation, profiles, users
from app.services.events import Event, EventKind
from app.services.relations import open_conversation_partner_ids

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5.0
BATCH_SIZE = 20
SEND_DELAY = 0.05  # about 20 messages per second, below Telegram's limit of 30
MAX_FLOOD_RETRIES = 3
HOUSEKEEPING_INTERVAL = 6 * 60 * 60.0
OUTAGE_PAUSE_MIN = 5.0
OUTAGE_PAUSE_MAX = 300.0
# Errors that say nothing about the message itself: Telegram is unreachable or refuses everything.
TELEGRAM_UNAVAILABLE = (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, TelegramUnauthorizedError)


@dataclass(frozen=True, slots=True)
class Delivery:
    telegram_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None = None
    # Where to send instead when Telegram refuses this chat for good (the bot was removed from the
    # witness group, a wrong WITNESS_CHAT_ID): the witness must not silently lose copies.
    fallback: tuple[int, ...] = ()

    @classmethod
    def of(cls, telegram_id: int, rendered: render.Rendered, fallback: tuple[int, ...] = ()) -> Delivery:
        return cls(telegram_id, rendered.text, rendered.reply_markup, fallback)


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
    # A banned sender's queued messages are not pushed to the partner (wali copies still go out).
    if _notifiable(recipient) and recipient.id != sender.id and not sender.is_banned:  # never echo to the sender
        watched = await guardians.has_active_guardian(session, sender.id)
        # «Ответить» only when plain text would not reach this chat by itself.
        several = (await chat.count_open_conversations(session, [recipient.id]))[recipient.id] > 1
        rendered = render.message_notification(sender, message, with_reply=several, sender_has_guardian=watched)
        deliveries.append(Delivery.of(recipient.telegram_id, rendered))
    participants = {sender.id: sender} | ({recipient.id: recipient} if recipient is not None else {})
    deliveries += await _guardian_copies(session, payload, participants, sender, message)
    return deliveries


async def _guardian_copies(
    session: AsyncSession, payload: dict[str, Any], participants: dict[int, User], sender: User | None, message: Message
) -> list[Delivery]:
    """One copy per active wali of a participant (``sender`` None: a witness message)."""
    deliveries = []
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
        partner_id = next((uid for uid in participants if uid != ward.id), None)
        partner = participants.get(partner_id) if partner_id is not None else None
        deliveries.append(
            Delivery.of(active.guardian.telegram_id, render.guardian_copy(ward, partner, sender, message))
        )
        copied_to.add(guardian_id)
    return deliveries


async def _message_witness(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """The witness wrote into the chat: both participants and their walis."""
    message = await session.get(Message, row.payload["message_id"])
    if message is None:
        return []
    participants = {
        user.id: user
        for user_id in row.payload.get("recipient_ids", [])
        if (user := await users.get_by_id(session, user_id)) is not None
    }
    deliveries = [
        Delivery.of(user.telegram_id, render.witness_message(message))
        for user in participants.values()
        if _notifiable(user)
    ]
    return deliveries + await _guardian_copies(session, row.payload, participants, None, message)


async def _interest_created(session: AsyncSession, row: Outbox) -> list[Delivery]:
    interest = await session.get(Interest, row.payload["interest_id"])
    if interest is None or interest.status != InterestStatus.PENDING:
        return []
    recipient = await users.get_by_id(session, interest.to_user_id)
    sender = await users.get_by_id(session, interest.from_user_id)
    if not _notifiable(recipient) or sender is None or sender.is_banned or sender.profile is None:
        return []
    if not profiles.has_approved_content(sender.profile):  # edited or rejected since: not shown to others
        return []
    has_guardian = await guardians.has_active_guardian(session, sender.id)
    rendered = render.interest_notification(interest, sender.profile, has_guardian=has_guardian)
    return [Delivery.of(recipient.telegram_id, rendered)]


async def _interest_accepted(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """The legacy «Чат открыт между анкетами №A и №B» to the sender (the acceptor saw it at once)."""
    sender = await users.get_by_id(session, row.payload["from_user_id"])
    acceptor = await users.get_by_id(session, row.payload["to_user_id"])
    if not _notifiable(sender) or acceptor is None or sender.profile is None or acceptor.profile is None:
        return []
    rendered = render.chat_opened(
        acceptor.profile,
        sender.profile,
        row.payload["conversation_id"],
        several_allowed=get_settings().MAX_OPEN_CONVERSATIONS > 1,
        badge=await guardians.has_active_guardian(session, acceptor.id),
    )
    return [Delivery.of(sender.telegram_id, rendered)]


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
    by_witness = reason == ConversationCloseReason.CLOSED_BY_WITNESS
    deliveries = []
    for user_id in (conversation.user_a_id, conversation.user_b_id):
        if user_id is None or (user_id == row.payload.get("closed_by_id") and not by_witness):
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
    if report is None or report.status != ReportStatus.OPEN or report.reporter.is_banned:
        return []
    return await _to_admins(session, render.report_card(report))


async def _guardian_linked(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """The ward is told who the wali is; the partners of the ward's open chats are told that
    their messages are now copied to a wali (spec: the partner sees «Вали подключён»)."""
    link = await guardians.get_active_guardian(session, row.payload["ward_user_id"])
    if link is None or link.id != row.payload["guardian_id"] or link.guardian is None:
        return []
    ward = await users.get_by_id(session, row.payload["ward_user_id"])
    if ward is None or ward.is_banned:
        return []
    deliveries = [Delivery.of(ward.telegram_id, render.guardian_linked(link.guardian))] if _notifiable(ward) else []
    for partner_id in await open_conversation_partner_ids(session, ward.id):
        conversation = await chat.get_conversation_between(session, ward.id, partner_id)
        partner = await users.get_by_id(session, partner_id)
        if conversation is not None and _notifiable(partner) and partner.id != link.guardian_user_id:
            notice = render.guardian_linked_to_partner(ward, conversation.id)
            deliveries.append(Delivery.of(partner.telegram_id, notice))
    return deliveries


async def _to_witness(session: AsyncSession, rendered: render.Rendered) -> list[Delivery]:
    """The witness: WITNESS_CHAT_ID (falling back to the admins if Telegram refuses that chat), or
    each admin privately when it is unset."""
    admin_ids = await users.admin_telegram_ids(session)
    witness_chat = get_settings().WITNESS_CHAT_ID
    if witness_chat is None:
        return [Delivery.of(telegram_id, rendered) for telegram_id in admin_ids]
    return [Delivery.of(witness_chat, rendered, fallback=tuple(admin_ids))]


async def _participants(session: AsyncSession, conversation: Conversation) -> tuple[User | None, User | None]:
    a = await users.get_by_id(session, conversation.user_a_id) if conversation.user_a_id is not None else None
    b = await users.get_by_id(session, conversation.user_b_id) if conversation.user_b_id is not None else None
    return a, b


async def _witness_copy(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """Exactly one copy per participant message, from the site or Telegram."""
    message = await session.get(Message, row.payload["message_id"])
    if message is None:
        return []  # deleted with the sender's account
    sender = await users.get_by_id(session, row.payload["sender_id"])
    recipient = await users.get_by_id(session, row.payload["recipient_id"])
    rendered = render.witness_copy(row.payload["conversation_id"], message, sender, recipient)
    return await _to_witness(session, rendered)


async def _witness_conversation_opened(session: AsyncSession, row: Outbox) -> list[Delivery]:
    conversation = await session.get(Conversation, row.payload["conversation_id"])
    if conversation is None:
        return []
    a, b = await _participants(session, conversation)
    return await _to_witness(session, render.witness_conversation_opened(conversation, a, b))


async def _witness_conversation_closed(session: AsyncSession, row: Outbox) -> list[Delivery]:
    conversation = await session.get(Conversation, row.payload["conversation_id"])
    if conversation is None:
        return []
    a, b = await _participants(session, conversation)
    reason = ConversationCloseReason(row.payload["reason"])
    return await _to_witness(session, render.witness_conversation_closed(conversation, reason, a, b))


async def _witness_report(session: AsyncSession, row: Outbox) -> list[Delivery]:
    """Only to the witness group: the admins already get the report card (report.created)."""
    witness_chat = get_settings().WITNESS_CHAT_ID
    report = await moderation.get_report(session, row.payload["report_id"])
    if witness_chat is None or report is None:
        return []
    return [Delivery.of(witness_chat, render.witness_report(report))]


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
    EventKind.MESSAGE_WITNESS: _message_witness,
    EventKind.WITNESS_COPY: _witness_copy,
    EventKind.WITNESS_CONVERSATION_OPENED: _witness_conversation_opened,
    EventKind.WITNESS_CONVERSATION_CLOSED: _witness_conversation_closed,
    EventKind.WITNESS_REPORT: _witness_report,
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
        self._stopped = asyncio.Event()
        self._outage_pause = 0.0  # > 0 while Telegram is unreachable

    @property
    def telegram_unavailable(self) -> bool:
        """True after a batch stopped because Telegram could not be reached."""
        return self._outage_pause > 0

    def wake(self, _event: Event | None = None) -> None:
        """EventListener callback: process the outbox now."""
        self._wakeup.set()

    def stop(self) -> None:
        """Ask ``run`` to return after the current batch."""
        self._stopped.set()
        self._wakeup.set()

    async def run(self) -> None:
        logger.info("outbox worker started")
        while not self._stopped.is_set():
            self._wakeup.clear()
            try:
                while (
                    await self.process_batch() >= self._batch_size
                    and not self.telegram_unavailable
                    and not self._stopped.is_set()
                ):
                    pass
            except Exception as exc:  # keep the worker alive; rows stay pending
                logger.error("outbox batch failed: %s", exc_summary(exc))
            if self.telegram_unavailable:  # new events must not hammer Telegram: only stop() cuts it short
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), self._outage_pause)
            elif not self._stopped.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), self._poll_interval)
        logger.info("outbox worker stopped")

    async def process_batch(self) -> int:
        """Deliver one batch of due rows in one transaction; returns how many rows were taken.

        Stops at the first row Telegram cannot be reached for (see the module docstring)."""
        async with session_scope() as session:
            rows = await events.fetch_pending(session, limit=self._batch_size)
            for row in rows:
                if not await self._process(session, row):
                    self._outage_pause = min(max(self._outage_pause * 2, OUTAGE_PAUSE_MIN), OUTAGE_PAUSE_MAX)
                    logger.warning("outbox: Telegram is unavailable, pausing delivery for %.0f s", self._outage_pause)
                    break
            else:
                if self._outage_pause:
                    logger.info("outbox: Telegram is reachable again, delivery resumed")
                self._outage_pause = 0.0
        return len(rows)

    async def _process(self, session: AsyncSession, row: Outbox) -> bool:
        """Deliver one row. Returns False (row untouched) if Telegram could not be reached."""
        try:
            async with session.begin_nested():  # a failing query must not abort the whole batch
                deliveries = await build_deliveries(session, row)
        except Exception as exc:
            logger.error("outbox %s (%s): building messages failed: %s", row.id, row.kind, exc_summary(exc))
            await events.mark_failed(session, row, f"build: {type(exc).__name__}")
            return True
        try:
            for delivery in deliveries:
                if not await self._send(row, delivery.telegram_id, delivery) and delivery.fallback:
                    logger.warning(
                        "outbox %s (%s): the witness chat refused the message, sent to admins", row.id, row.kind
                    )
                    for telegram_id in delivery.fallback:
                        await self._send(row, telegram_id, delivery)
        except TELEGRAM_UNAVAILABLE as exc:
            logger.warning("outbox %s (%s): Telegram unavailable: %s", row.id, row.kind, type(exc).__name__)
            return False
        except Exception as exc:  # unexpected and specific to this row: retry later with backoff
            logger.error("outbox %s (%s): delivery failed: %s", row.id, row.kind, exc_summary(exc))
            await events.mark_failed(session, row, f"send: {type(exc).__name__}")
            return True
        await events.mark_sent(session, row)
        return True

    async def _send(self, row: Outbox, telegram_id: int, delivery: Delivery) -> bool:
        """Send one message; returns whether it was delivered. Flood control (also a group's rate
        limit, e.g. the witness chat): wait and retry a few times, then the batch pauses with the
        row untouched, so nothing is dropped. A recipient who blocked the bot or a message Telegram
        rejects is skipped (retrying cannot help). Other errors propagate."""
        flood_retries = 0
        delivered = False
        while True:
            try:
                await self._bot.send_message(
                    chat_id=telegram_id, text=delivery.text, reply_markup=delivery.reply_markup
                )
                delivered = True
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
        return delivered


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
