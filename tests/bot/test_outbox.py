"""Outbox worker: Telegram delivery per event kind with a fake Bot (no network)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import select, update

from app.bot import render
from app.bot.callbacks import AdminProfileCb, ChatCb
from app.bot.outbox import OutboxWorker
from app.clock import utcnow
from app.enums import Gender, MessageOrigin, ProfileStatus, ReportReason
from app.models import Message, Outbox, User
from app.schemas import ProfileCreate
from app.services import chat, guardians, interests, moderation, profiles, users
from app.services.events import EventListener
from tests.bot.conftest import SEND_METHOD, buttons


async def _settle(session) -> None:
    """Mark everything emitted so far as delivered, so a test looks at its own events only."""
    await session.execute(update(Outbox).values(sent_at=utcnow()))
    await session.commit()


async def _link_guardian(session, ward: User, guardian: User) -> None:
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], guardian)
    await session.commit()


async def _last_row(session) -> Outbox:
    """The latest row that is not a witness notice (those go to the witness chat, see test_witness)."""
    stmt = (
        select(Outbox)
        .where(~Outbox.kind.startswith("witness."))
        .order_by(Outbox.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
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

    assert await worker.process_batch() == 2  # message.created + witness.copy

    assert bot.to(man) == []
    [to_woman] = bot.to(woman)
    # The legacy format; the sender has a wali: the badge is in every notification of his messages.
    assert to_woman.text == (
        f"<b>Анкета №500 пишет:</b>\nАс-саляму алейкум &lt;b&gt;!&lt;/b&gt;\n\n{render.GUARDIAN_BADGE}"
    )
    message = await session.scalar(select(Message).where(Message.sender_id == man.id))
    [report] = buttons(to_woman.reply_markup)  # one open chat: no «Ответить», text goes there anyway
    assert (report.text, report.callback_data) == ("Пожаловаться", f"rep:{message.id}")
    for wali in (wali_man, wali_woman):
        [copy] = bot.to(wali)
        assert copy.text.startswith("👁 Копия переписки:")
        assert "<b>Анкета №500</b>: Ас-саляму алейкум &lt;b&gt;!&lt;/b&gt;" in copy.text
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
    bot.fail(woman.telegram_id, TelegramForbiddenError(SEND_METHOD, "Forbidden: bot was blocked by the user"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    assert bot.to(woman) == []
    assert len(bot.to(wali_woman)) == 1
    row = await _last_row(session)
    assert row.sent_at is not None and row.attempts == 0


async def test_chat_not_found_is_skipped_without_retry(session, bot, worker, chat_with_walis):
    man, woman, conversation, *_ = chat_with_walis
    bot.fail(woman.telegram_id, TelegramBadRequest(SEND_METHOD, "Bad Request: chat not found"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    row = await _last_row(session)
    assert row.sent_at is not None


async def test_flood_control_of_one_chat_defers_the_row_without_resending_or_counting(
    session, bot, worker, chat_with_walis
):
    man, woman, conversation, wali_man, wali_woman = chat_with_walis
    bot.fail(woman.telegram_id, TelegramRetryAfter(SEND_METHOD, "Too Many Requests", retry_after=0))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()
    assert bot.to(woman) == [] and not worker.telegram_unavailable
    row = await _last_row(session)
    assert row.sent_at is None and row.attempts == 0  # deferred, not failed
    await worker.process_batch()

    assert len(bot.to(woman)) == 1
    assert len(bot.to(wali_man)) == 1 and len(bot.to(wali_woman)) == 1  # the walis got their copies once
    row = await _last_row(session)
    assert row.sent_at is not None and row.attempts == 0


async def test_telegram_outage_pauses_delivery_without_giving_up(session, bot, worker, chat_with_walis):
    """A long outage (connection refused, 5xx) must not burn the row's attempts: wali copies and
    notifications are delivered once Telegram is back, in their original order."""
    man, woman, conversation, _, wali_woman = chat_with_walis
    outage = [TelegramNetworkError(SEND_METHOD, "Cannot connect"), TelegramServerError(SEND_METHOD, "Bad Gateway")] * 10
    bot.fail(woman.telegram_id, *outage)
    await chat.send_message(session, conversation.id, man, "Первое", MessageOrigin.WEB)
    await chat.send_message(session, conversation.id, man, "Второе", MessageOrigin.WEB)
    await session.commit()

    for _ in range(len(outage)):  # far more than events.MAX_ATTEMPTS
        await worker.process_batch()
        assert worker.telegram_unavailable
    rows = (
        await session.scalars(
            select(Outbox).where(Outbox.sent_at.is_(None), Outbox.kind == "message.created").order_by(Outbox.id)
        )
    ).all()
    for row in rows:
        await session.refresh(row)
    assert [(row.attempts, row.failed_at, row.next_attempt_at <= utcnow()) for row in rows] == [(0, None, True)] * 2
    assert bot.to(woman) == []  # the second message did not overtake the first

    await worker.process_batch()

    assert not worker.telegram_unavailable
    assert [item.text.split("\n")[1] for item in bot.to(woman)] == ["Первое", "Второе"]
    assert [item.text.rsplit(": ", 1)[-1] for item in bot.to(wali_woman)] == ["Первое", "Второе"]


async def test_row_specific_failure_schedules_retry_with_backoff(session, bot, worker, chat_with_walis):
    man, woman, conversation, *_ = chat_with_walis
    bot.fail(woman.telegram_id, RuntimeError("unexpected"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()

    row = await _last_row(session)
    assert row.sent_at is None and row.attempts == 1
    assert row.last_error == "send: RuntimeError"
    assert row.next_attempt_at > utcnow() + timedelta(seconds=5)
    assert await worker.process_batch() == 0  # not due yet
    assert not worker.telegram_unavailable


async def test_interest_created_accepted_and_declined(session, bot, worker, make_pair, make_profile):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id, "Хочу <познакомиться>")
    await session.commit()
    await worker.process_batch()
    [card] = bot.to(woman)
    assert card.text.startswith("<b>Вами заинтересовалась анкета №500</b>")
    assert "Хочу &lt;познакомиться&gt;" in card.text
    accept, decline = buttons(card.reply_markup)
    assert (accept.callback_data, decline.callback_data) == (f"int:acc:{interest.id}", f"int:dec:{interest.id}")

    conversation = await interests.accept(session, interest.id, woman)
    await session.commit()
    await worker.process_batch()
    [accepted] = bot.to(man)
    assert accepted.text.startswith("<b>Чат открыт между анкетами №501 и №500.</b>")
    [close] = buttons(accepted.reply_markup)
    assert close.text == "Завершить общение"
    assert ChatCb.unpack(close.callback_data).conversation_id == conversation.id

    other = (await make_profile(gender=Gender.FEMALE)).user
    second = await interests.send_interest(session, man, other.id)
    await interests.decline(session, second.id, other)
    await session.commit()
    await worker.process_batch()
    assert bot.to(other) == []  # the interest was declined before delivery: no stale card
    assert bot.to(man)[-1].text == "Анкета №502 отклонила ваш отклик. Не отчаивайтесь, ин ша Аллах."


async def test_conversation_closed_goes_to_the_other_participant(session, bot, worker, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _settle(session)
    await chat.close_conversation(session, conversation, man)
    await session.commit()

    await worker.process_batch()

    assert bot.to(man) == []
    [closed] = bot.to(woman)
    assert closed.text == "Анкета №500 завершила общение."


async def test_profile_submitted_goes_to_admins_and_moderated_to_owner(
    session, bot, worker, make_user, override_settings
):
    admin = await make_user(is_admin=True)
    override_settings(ADMIN_TELEGRAM_IDS=[777])
    owner = await make_user()
    data = ProfileCreate(
        gender=Gender.FEMALE,
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


async def test_wali_linked_mid_chat_is_disclosed_to_the_partner(
    session, bot, worker, make_pair, make_conversation, make_user
):
    """The partner who keeps chatting from the bot must learn that a wali now reads along."""
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.set_bot_active_conversation(session, man, conversation.id)
    await _settle(session)
    wali = await make_user(first_name="Юсуф")
    await _link_guardian(session, woman, wali)

    await worker.process_batch()

    [notice] = bot.to(man)
    assert (
        notice.text
        == "👁 К переписке с <b>Анкета №501</b> подключён вали: ваши сообщения в этом чате теперь видны вали."
    )
    assert ChatCb.unpack(buttons(notice.reply_markup)[0].callback_data).conversation_id == conversation.id
    row = await _last_row(session)
    assert row.payload["user_ids"] == sorted([man.id, woman.id, wali.id])

    # Her messages and the acceptance of her interests carry the badge from now on.
    await chat.send_message(session, conversation.id, woman, "Салам", MessageOrigin.WEB)
    await session.commit()
    await worker.process_batch()
    assert bot.to(man)[-1].text.endswith("\n\n👁 Вали подключён — переписка видна вали")


async def test_interest_accepted_by_a_ward_shows_the_badge(session, bot, worker, make_pair, make_user):
    man, woman = await make_pair()
    await _link_guardian(session, woman, await make_user())
    interest = await interests.send_interest(session, man, woman.id)
    await interests.accept(session, interest.id, woman)
    await session.commit()

    await worker.process_batch()

    [accepted] = [item for item in bot.to(man) if "Чат открыт" in item.text]
    assert "👁 Вали подключён — переписка видна вали" in accepted.text


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


async def test_queued_cards_and_messages_of_a_banned_user_are_not_delivered(
    session, bot, worker, chat_with_walis, make_user
):
    man, woman, conversation, _, wali_woman = chat_with_walis
    admin = await make_user(is_admin=True)
    await moderation.create_report(session, man, woman.id, ReportReason.INSULT)
    await chat.send_message(session, conversation.id, man, "Сообщение перед баном", MessageOrigin.WEB)
    await users.ban(session, man)
    await session.commit()

    await worker.process_batch()

    assert not any("Жалоба" in item.text for item in bot.to(admin))  # the banned reporter's report card
    assert any(item.text.startswith("Чат #") for item in bot.to(admin))  # the witness still sees everything
    assert not any("Сообщение перед баном" in item.text for item in bot.to(woman))
    assert any("Сообщение перед баном" in item.text for item in bot.to(wali_woman))  # oversight copies still go
