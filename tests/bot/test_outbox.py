"""Outbox worker: Telegram delivery per event kind with a fake Bot (no network)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from app.bot.callbacks import AdminProfileCb, ChatCb, InterestCb
from app.bot.outbox import OutboxWorker
from app.clock import utcnow
from app.enums import Gender, MessageOrigin, ProfileStatus, ReportReason
from app.models import Outbox, User
from app.schemas import ProfileCreate
from app.services import chat, guardians, interests, moderation, profiles, users
from app.services.events import EventListener
from tests.bot.conftest import buttons

_METHOD = SendMessage(chat_id=1, text="x")


@dataclass
class Sent:
    chat_id: int
    text: str
    reply_markup: Any


class FakeBot:
    """Records send_message calls; ``fail(chat_id, exc, ...)`` makes the next sends to that chat raise."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.delivered = asyncio.Event()
        self._failures: dict[int, list[Exception]] = {}

    def fail(self, chat_id: int, *errors: Exception) -> None:
        self._failures.setdefault(chat_id, []).extend(errors)

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None, **_: Any) -> None:
        if self._failures.get(chat_id):
            raise self._failures[chat_id].pop(0)
        self.sent.append(Sent(chat_id, text, reply_markup))
        self.delivered.set()

    def to(self, user: User) -> list[Sent]:
        return [item for item in self.sent if item.chat_id == user.telegram_id]


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def worker(bot: FakeBot) -> OutboxWorker:
    return OutboxWorker(bot, send_delay=0)  # type: ignore[arg-type]


async def _settle(session) -> None:
    """Mark everything emitted so far as delivered, so a test looks at its own events only."""
    await session.execute(update(Outbox).values(sent_at=utcnow()))
    await session.commit()


async def _link_guardian(session, ward: User, guardian: User) -> None:
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], guardian)
    await session.commit()


async def _last_row(session) -> Outbox:
    stmt = select(Outbox).order_by(Outbox.id.desc()).limit(1).execution_options(populate_existing=True)
    return (await session.scalars(stmt)).one()


@pytest.fixture
async def chat_with_walis(session, make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    wali_man, wali_woman = await make_user(first_name="Отец"), await make_user(first_name="Брат")
    await _link_guardian(session, man, wali_man)
    await _link_guardian(session, woman, wali_woman)
    await _settle(session)
    return man, woman, conversation, wali_man, wali_woman


async def test_web_message_goes_to_recipient_and_both_walis_never_to_sender(session, bot, worker, chat_with_walis):
    man, woman, conversation, wali_man, wali_woman = chat_with_walis
    await chat.send_message(session, conversation.id, man, "Ас-саляму алейкум <b>!</b>", MessageOrigin.WEB)
    await session.commit()

    assert await worker.process_batch() == 1

    assert bot.to(man) == []
    [to_woman] = bot.to(woman)
    assert to_woman.text == f"💬 <b>Ахмад</b> (#{man.id}):\nАс-саляму алейкум &lt;b&gt;!&lt;/b&gt;"
    reply, site = buttons(to_woman.reply_markup)
    assert ChatCb.unpack(reply.callback_data).conversation_id == conversation.id
    assert site.url == f"http://testserver/chats/{conversation.id}"
    for wali in (wali_man, wali_woman):
        [copy] = bot.to(wali)
        assert copy.text.startswith("👁 Копия переписки:")
        assert "<b>Ахмад</b>: Ас-саляму алейкум &lt;b&gt;!&lt;/b&gt;" in copy.text
        assert copy.reply_markup is None
    row = await _last_row(session)
    assert row.sent_at is not None and row.attempts == 0


async def test_disabled_notifications_skip_recipient_but_not_wali_copies(session, bot, worker, chat_with_walis):
    man, woman, conversation, wali_man, wali_woman = chat_with_walis
    await users.set_notify(session, woman, False)
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.TELEGRAM)
    await session.commit()

    await worker.process_batch()

    assert bot.to(woman) == []
    assert len(bot.to(wali_man)) == len(bot.to(wali_woman)) == 1


async def test_revoked_wali_gets_no_copy(session, bot, worker, chat_with_walis):
    man, woman, conversation, wali_man, wali_woman = chat_with_walis
    await chat.send_message(session, conversation.id, woman, "Ва алейкум ассалам", MessageOrigin.WEB)
    await guardians.revoke(session, woman)  # after the message, before delivery
    await session.commit()

    await worker.process_batch()

    assert len(bot.to(man)) == 1
    assert bot.to(wali_woman) == []
    assert len(bot.to(wali_man)) == 1


async def test_blocked_bot_marks_row_sent_and_others_still_delivered(session, bot, worker, chat_with_walis):
    man, woman, conversation, _, wali_woman = chat_with_walis
    bot.fail(woman.telegram_id, TelegramForbiddenError(_METHOD, "Forbidden: bot was blocked by the user"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    assert bot.to(woman) == []
    assert len(bot.to(wali_woman)) == 1
    row = await _last_row(session)
    assert row.sent_at is not None and row.attempts == 0


async def test_chat_not_found_is_skipped_without_retry(session, bot, worker, chat_with_walis):
    man, woman, conversation, *_ = chat_with_walis
    bot.fail(woman.telegram_id, TelegramBadRequest(_METHOD, "Bad Request: chat not found"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    row = await _last_row(session)
    assert row.sent_at is not None


async def test_flood_control_waits_and_retries(session, bot, worker, chat_with_walis):
    man, woman, conversation, *_ = chat_with_walis
    bot.fail(woman.telegram_id, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=0))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    assert len(bot.to(woman)) == 1
    row = await _last_row(session)
    assert row.sent_at is not None and row.attempts == 0


async def test_network_error_schedules_retry_with_backoff(session, bot, worker, chat_with_walis):
    man, woman, conversation, *_ = chat_with_walis
    bot.fail(woman.telegram_id, TelegramNetworkError(_METHOD, "timeout"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    row = await _last_row(session)
    assert row.sent_at is None and row.attempts == 1
    assert row.last_error == "send: TelegramNetworkError"
    assert row.next_attempt_at > utcnow() + timedelta(seconds=5)
    assert await worker.process_batch() == 0  # not due yet


async def test_interest_created_accepted_and_declined(session, bot, worker, make_pair, make_profile):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id, "Хочу <познакомиться>")
    await session.commit()
    await worker.process_batch()
    [card] = bot.to(woman)
    assert "Хочу &lt;познакомиться&gt;" in card.text
    accept, _, site = buttons(card.reply_markup)
    assert InterestCb.unpack(accept.callback_data).interest_id == interest.id
    assert site.url == f"http://testserver/u/{man.id}"

    conversation = await interests.accept(session, interest.id, woman)
    await session.commit()
    await worker.process_batch()
    [accepted] = bot.to(man)
    assert "Ваш отклик принят 🤲" in accepted.text
    assert ChatCb.unpack(buttons(accepted.reply_markup)[0].callback_data).conversation_id == conversation.id

    other = (await make_profile(gender=Gender.FEMALE, name="Мариям")).user
    second = await interests.send_interest(session, man, other.id)
    await interests.decline(session, second.id, other)
    await session.commit()
    await worker.process_batch()
    assert bot.to(other) == []  # the interest was declined before delivery: no stale card
    assert "Не расстраивайтесь" in bot.to(man)[-1].text


async def test_conversation_closed_goes_to_the_other_participant(session, bot, worker, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _settle(session)
    await chat.close_conversation(session, conversation, man)
    await session.commit()

    await worker.process_batch()

    assert bot.to(man) == []
    [closed] = bot.to(woman)
    assert "завершил общение" in closed.text


async def test_profile_submitted_goes_to_admins_and_moderated_to_owner(
    session, bot, worker, make_user, override_settings
):
    admin = await make_user(is_admin=True)
    override_settings(ADMIN_TELEGRAM_IDS=[777])
    owner = await make_user()
    data = ProfileCreate(
        gender=Gender.FEMALE,
        name="Айша",
        birth_year=1995,
        city="Уфа",
        marital_status="never_married",
        children=0,
        education="higher",
        employment="employed",
        wishes="Ищу верующего мужа для семьи.",
    )
    profile = await profiles.create_profile(session, owner, data)
    await session.commit()
    await worker.process_batch()

    recipients = sorted(item.chat_id for item in bot.sent)
    assert recipients == sorted([admin.telegram_id, 777])
    approve = AdminProfileCb.unpack(buttons(bot.sent[0].reply_markup)[0].callback_data)
    assert approve.user_id == owner.id

    await profiles.reject(session, profile.user_id, "Нужно <больше> деталей")
    await session.commit()
    await worker.process_batch()
    [moderated] = bot.to(owner)
    assert "Причина: Нужно &lt;больше&gt; деталей" in moderated.text
    assert profile.status == ProfileStatus.REJECTED


async def test_report_created_goes_to_admins(session, bot, worker, make_pair, make_user):
    admin = await make_user(is_admin=True)
    man, woman = await make_pair()
    await moderation.create_report(session, woman, man.id, ReportReason.FRAUD, "Просил <деньги>")
    await session.commit()

    await worker.process_batch()

    [card] = bot.to(admin)
    assert "Просил &lt;деньги&gt;" in card.text
    assert len(buttons(card.reply_markup)) == 3


async def test_guardian_linked_goes_to_ward(session, bot, worker, make_pair, make_user):
    _, woman = await make_pair()
    wali = await make_user(first_name="Юсуф", username="yusuf_wali")
    await _link_guardian(session, woman, wali)

    await worker.process_batch()

    [linked] = bot.to(woman)
    assert "Вали подключён: Юсуф (@yusuf_wali)" in linked.text
    assert bot.to(wali) == []


async def test_deleted_recipient_is_skipped(session, bot, worker, make_pair):
    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id)
    await session.commit()
    woman_id = woman.id
    await users.delete_account(session, await users.get_by_id(session, woman_id))
    await session.commit()

    await worker.process_batch()

    assert bot.sent == []
    rows = (await session.scalars(select(Outbox).where(Outbox.sent_at.is_(None)))).all()
    assert rows == []


async def test_run_loop_wakes_on_notify_and_stops(session, bot, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _settle(session)
    worker = OutboxWorker(bot, poll_interval=60, send_delay=0)  # type: ignore[arg-type]
    listener = EventListener()
    listener.add_callback(worker.wake)
    await listener.start()
    await listener.wait_connected(5)
    task = asyncio.create_task(worker.run())
    try:
        await chat.send_message(session, conversation.id, woman, "Салам", MessageOrigin.WEB)
        await session.commit()
        await asyncio.wait_for(bot.delivered.wait(), 5)  # far below the 60 s polling interval: NOTIFY woke it
        assert len(bot.to(man)) == 1
    finally:
        worker.stop()
        await asyncio.wait_for(task, 5)
        await listener.stop()
