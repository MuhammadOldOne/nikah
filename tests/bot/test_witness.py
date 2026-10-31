"""The witness (махрам/админ): a copy of every participant message from both channels exactly once,
chat opened/closed notices and reports, and the buttons «⚠️ Пометить», «✍️ Написать в чат»,
«🚫 Закрыть чат» — in the witness group or an admin's private chat, admins only."""

from __future__ import annotations

from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import ForceReply
from sqlalchemy import select

from app.bot import texts
from app.enums import ConversationCloseReason, MessageKind, MessageOrigin, ReportReason
from app.models import Message, Outbox, User
from app.services import chat, guardians, meta, moderation, witness
from tests.bot.conftest import SEND_METHOD, buttons

GROUP = -1001234567890
ADMIN_TG = 777_000_001


def _texts(items) -> list[str]:
    return [item.text for item in items]


async def _drain(worker) -> None:
    while await worker.process_batch():
        pass


async def test_copies_of_both_channels_reach_the_witness_group_exactly_once(
    harness, session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, woman, "Ас-саляму алейкум <брат>", MessageOrigin.WEB)
    await session.commit()
    await harness.text(man, "Ва алейкум ассалам")  # through the bot's own handler

    await _drain(worker)
    await _drain(worker)

    cid = conversation.id
    assert _texts(bot.to(GROUP)) == [
        f"Открыт чат #{cid}: №500 ↔ №501.",
        f"Чат #{cid} | №501 → №500 (сайт)\nАс-саляму алейкум &lt;брат&gt;",
        f"Чат #{cid} | №500 → №501 (Telegram)\nВа алейкум ассалам",
    ]
    message = await session.scalar(select(Message).where(Message.sender_id == man.id))
    flag, say, close = buttons(bot.to(GROUP)[-1].reply_markup)
    assert (flag.callback_data, say.callback_data, close.callback_data) == (
        f"w:flag:{message.id}",
        f"w:say:{cid}",
        f"w:close:{cid}",
    )
    assert not any(str(man.telegram_id) in item.text or str(man.id) == item.text for item in bot.to(GROUP))


async def test_without_a_group_each_admin_gets_the_copies(
    session, bot, worker, make_pair, make_conversation, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=None, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    admin = await make_user(is_admin=True)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await _drain(worker)

    for chat_id in (ADMIN_TG, admin.telegram_id):
        copies = [text for text in _texts(bot.to(chat_id)) if text.startswith("Чат #")]
        assert copies == [f"Чат #{conversation.id} | №500 → №501 (сайт)\nСалам"]


async def test_a_refusing_witness_group_falls_back_to_the_admins(
    session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _drain(worker)
    bot.sent.clear()
    bot.fail(GROUP, TelegramForbiddenError(SEND_METHOD, "Forbidden: bot was kicked from the supergroup chat"))
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()

    await _drain(worker)

    assert _texts(bot.to(ADMIN_TG)) == [f"Чат #{conversation.id} | №500 → №501 (сайт)\nСалам"]


async def test_group_flood_control_delays_but_never_drops_a_copy(
    session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _drain(worker)
    bot.sent.clear()
    flood = TelegramRetryAfter(SEND_METHOD, "Too Many Requests", retry_after=0)
    bot.fail(GROUP, *[flood] * 5)
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await chat.send_message(session, conversation.id, woman, "Ва алейкум салам", MessageOrigin.WEB)
    await session.commit()

    # The group's flood control holds only the group: both partners got their messages at once.
    await worker.process_batch()
    assert not worker.telegram_unavailable and bot.to(GROUP) == []
    assert _texts(bot.to(woman))[-1].endswith("Салам") and _texts(bot.to(man))[-1].endswith("Ва алейкум салам")
    await _drain(worker)

    assert _texts(bot.to(GROUP)) == [
        f"Чат #{conversation.id} | №500 → №501 (сайт)\nСалам",
        f"Чат #{conversation.id} | №501 → №500 (сайт)\nВа алейкум салам",
    ]
    assert len(bot.to(woman)) == 1 and len(bot.to(man)) == 1  # the partners got the messages once


async def test_a_long_group_flood_holds_its_rows_until_released(
    session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _drain(worker)
    bot.sent.clear()
    bot.fail(GROUP, TelegramRetryAfter(SEND_METHOD, "Too Many Requests", retry_after=30))
    for text in ("Первое", "Второе"):
        await chat.send_message(session, conversation.id, man, text, MessageOrigin.WEB)
    await session.commit()

    await worker.process_batch()
    copies = (
        await session.scalars(
            select(Outbox).where(Outbox.kind == "witness.copy").execution_options(populate_existing=True)
        )
    ).all()
    assert [row.sent_at for row in copies] == [None, None] and {row.attempts for row in copies} == {0}
    assert len({row.next_attempt_at for row in copies}) == 1  # released together, then taken in id order
    assert _texts(bot.to(woman)) and bot.to(GROUP) == []
    assert await worker.process_batch() == 0  # nothing is due before the release


async def test_a_witness_group_upgraded_to_a_supergroup_keeps_getting_copies(
    harness, session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=-4001, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _drain(worker)
    bot.sent.clear()
    upgraded = TelegramMigrateToChat(SEND_METHOD, "Bad Request: group chat was upgraded", migrate_to_chat_id=GROUP)
    bot.fail(-4001, upgraded)
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await chat.send_message(session, conversation.id, woman, "Ва алейкум салам", MessageOrigin.WEB)
    await session.commit()
    await _drain(worker)

    assert [text.rsplit("\n", 1)[-1] for text in _texts(bot.to(GROUP))] == ["Салам", "Ва алейкум салам"]
    assert bot.to(-4001) == [] and bot.to(ADMIN_TG) == []
    assert await meta.witness_chat_id(session) == GROUP
    # The witness's buttons work in the new supergroup.
    copy = await session.scalar(select(Message).order_by(Message.id))
    await harness.press(ADMIN_TG, f"w:flag:{copy.id}", group_id=GROUP)
    await session.refresh(copy)
    assert copy.flagged and "witness" in copy.flag_reason


async def test_witness_buttons_in_the_group(
    harness, session, bot, worker, make_pair, make_conversation, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    rude = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.WEB)
    await session.commit()
    await _drain(worker)
    bot.sent.clear()

    # An admin who never opened the bot privately: the account is created for the witness actions.
    await harness.press(ADMIN_TG, f"w:flag:{rude.id}", group_id=GROUP)
    assert harness.callback_answers()[-1].text == texts.WITNESS_FLAGGED
    await session.refresh(rude)
    assert rude.flagged and "witness" in rude.flag_reason

    await harness.press(ADMIN_TG, f"w:say:{conversation.id}", group_id=GROUP)
    prompt = harness.sent(GROUP)[-1]
    assert prompt.text.startswith(f"Введите текст, который бот отправит ОБЕИМ сторонам чата #{conversation.id}")
    assert isinstance(prompt.reply_markup, ForceReply)  # reaches the bot even in privacy mode
    await harness.text(ADMIN_TG, "Помните о <приличии>", group_id=GROUP)
    assert harness.last_text(GROUP) == texts.WITNESS_SAID
    said = await session.scalar(select(Message).where(Message.kind == MessageKind.WITNESS))
    assert (said.text, said.sender_id, said.conversation_id) == ("Помните о <приличии>", None, conversation.id)
    await harness.text(ADMIN_TG, "Обычное сообщение в группе", group_id=GROUP)  # the dialog is over
    assert harness.last_text(GROUP) == texts.WITNESS_SAID

    await harness.press(ADMIN_TG, f"w:close:{conversation.id}", group_id=GROUP)
    assert harness.callback_answers()[-1].text == texts.WITNESS_CLOSED
    await session.refresh(conversation)
    assert conversation.close_reason == ConversationCloseReason.CLOSED_BY_WITNESS
    await harness.press(ADMIN_TG, f"w:close:{conversation.id}", group_id=GROUP)
    assert harness.callback_answers()[-1].text == "Чат уже закрыт."

    await _drain(worker)
    for participant in (man, woman):
        assert _texts(bot.to(participant)) == [
            "<b>Сообщение от свидетеля:</b>\nПомните о &lt;приличии&gt;",
            "Свидетель завершил ваш чат. Если есть вопросы — обратитесь к админу.",
        ]
    assert _texts(bot.to(GROUP)) == [f"Чат #{conversation.id} закрыт: чат завершил свидетель (№500 ↔ №501)."]


async def test_witness_buttons_are_refused_to_non_admins(
    harness, session, make_pair, make_conversation, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    rude = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.WEB)
    await session.commit()

    member = 555_000_777  # a group member without an account: none is created
    await harness.press(member, f"w:close:{conversation.id}", group_id=GROUP)
    assert harness.callback_answers()[-1].text == "Только для свидетеля."
    assert await session.scalar(select(User).where(User.telegram_id == member)) is None

    for data in (f"w:flag:{rude.id}", f"w:say:{conversation.id}", f"w:close:{conversation.id}"):
        await harness.press(woman, data)  # a participant, in her private chat
        assert harness.callback_answers()[-1].text == "Только для свидетеля."
    await harness.text(woman, "Это не текст свидетеля")  # no witness dialog was opened: goes to her chat
    sent = await session.scalar(select(Message).where(Message.sender_id == woman.id))
    assert sent is not None and sent.kind == MessageKind.USER

    await session.refresh(rude)
    await session.refresh(conversation)
    assert not rude.flagged and conversation.is_open
    assert (await session.scalars(select(Message).where(Message.kind == MessageKind.WITNESS))).all() == []


async def test_other_groups_are_ignored(harness, session, make_pair, make_conversation, override_settings):
    override_settings(WITNESS_CHAT_ID=GROUP, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await harness.press(ADMIN_TG, f"w:close:{conversation.id}", group_id=-100999)
    assert harness.api.requests == []
    await session.refresh(conversation)
    assert conversation.is_open


async def test_witness_say_from_a_private_chat_and_cancel(
    harness, session, make_pair, make_conversation, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=None)
    admin = await make_user(is_admin=True)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)

    await harness.press(admin, f"w:say:{conversation.id}")
    await harness.text(admin, "/cancel")
    assert harness.last_text(admin) == texts.CANCELLED
    await harness.press(admin, f"w:say:{conversation.id}")
    await harness.text(admin, "Смотреть анкеты")  # a menu button leaves the dialog instead
    await harness.press(admin, f"w:say:{conversation.id}")
    await harness.text(admin, "Ассаляму алейкум, я свидетель этого чата.")

    said = (await session.scalars(select(Message).where(Message.kind == MessageKind.WITNESS))).all()
    assert [m.text for m in said] == ["Ассаляму алейкум, я свидетель этого чата."]
    assert harness.last_text(admin) == texts.WITNESS_SAID


async def test_reports_go_to_the_witness_group_only(
    session, bot, worker, make_pair, make_conversation, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP)
    admin = await make_user(is_admin=True)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    rude = await chat.send_message(session, conversation.id, man, "Переведи <деньги>", MessageOrigin.WEB)
    await session.commit()
    await _drain(worker)
    bot.sent.clear()

    await moderation.create_report(session, woman, man.id, ReportReason.OTHER, message_id=rude.id)
    await session.commit()
    await _drain(worker)

    assert _texts(bot.to(GROUP)) == [
        f"⚠️ Жалоба на сообщение #{rude.id} в чате #{conversation.id}\nОт: №501, на: №500\n«Переведи &lt;деньги&gt;»"
    ]
    [card] = _texts(bot.to(admin))  # the admins get the report card (report.created)
    assert "<b>Жалоба #" in card

    override_settings(WITNESS_CHAT_ID=None)
    bot.sent.clear()
    other = await chat.send_message(session, conversation.id, man, "Ещё", MessageOrigin.WEB)
    await session.commit()
    await _drain(worker)
    bot.sent.clear()
    await moderation.create_report(session, woman, man.id, ReportReason.OTHER, message_id=other.id)
    await session.commit()
    await _drain(worker)
    assert [item for item in bot.to(admin) if "Жалоба на сообщение" in item.text] == []  # admins: the card only


async def test_witness_message_reaches_the_walis_too(
    session, bot, worker, make_pair, make_conversation, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP)
    admin = await make_user(is_admin=True)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    wali = await make_user(first_name="Отец")
    invite = await guardians.create_invite(session, woman)
    await guardians.accept_invite(session, invite.rsplit("wali_", 1)[1], wali)
    await session.commit()
    await _drain(worker)
    bot.sent.clear()

    await witness.witness_say(session, admin, conversation.id, "Ассаляму алейкум")
    await session.commit()
    await _drain(worker)

    assert _texts(bot.to(man)) == _texts(bot.to(woman)) == ["<b>Сообщение от свидетеля:</b>\nАссаляму алейкум"]
    [copy] = _texts(bot.to(wali))
    assert copy.startswith("👁 Копия переписки: <b>Анкета №501</b> ↔ <b>Анкета №500</b>")
    assert copy.endswith("<b>Свидетель</b>: Ассаляму алейкум")


async def test_ban_and_unban_work_in_the_witness_group_and_admin_points_to_the_private_chat(
    harness, session, make_pair, make_user, override_settings
):
    override_settings(WITNESS_CHAT_ID=GROUP, ADMIN_TELEGRAM_IDS=[ADMIN_TG])
    man, _ = await make_pair()
    await harness.text(ADMIN_TG, "/ban №500", group_id=GROUP)
    await session.refresh(man)
    assert man.is_banned and harness.last_text(GROUP) == "Пользователь №500 (Анкета №500) заблокирован."
    await harness.text(ADMIN_TG, f"/unban@nikah_istina_bot {man.telegram_id}", group_id=GROUP)
    await session.refresh(man)
    assert not man.is_banned and harness.last_text(GROUP).endswith("(Анкета №500) разблокирован.")
    await harness.text(ADMIN_TG, "/admin", group_id=GROUP)
    assert harness.last_text(GROUP) == texts.ADMIN_QUEUE_IN_GROUP
    # Other members of the group cannot ban.
    member = await make_user()
    await harness.text(member, "/ban №500", group_id=GROUP)
    await session.refresh(man)
    assert not man.is_banned
