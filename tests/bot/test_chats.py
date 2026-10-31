"""Chats in the bot: list, active chat, text relay with origin=telegram, /stop, media rejection."""

from __future__ import annotations

from sqlalchemy import select

from app.bot import render, texts
from app.bot.callbacks import ChatAction, ChatCb
from app.enums import Gender, MessageOrigin
from app.models import Message, Outbox
from app.services import chat, users
from tests.bot.conftest import button_data, buttons


async def _activate(session, user, conversation) -> None:
    await chat.set_bot_active_conversation(session, user, conversation.id)
    await session.commit()


async def test_text_in_active_chat_is_sent_with_telegram_origin(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, woman, "Ас-саляму алейкум", MessageOrigin.WEB)
    await _activate(session, man, conversation)

    await harness.text(man, "Ва алейкум ассалам! Как вы?")

    sent = (await session.scalars(select(Message).where(Message.sender_id == man.id))).all()
    assert [(m.text, m.origin, m.conversation_id) for m in sent] == [
        ("Ва алейкум ассалам! Как вы?", MessageOrigin.TELEGRAM, conversation.id)
    ]
    assert harness.sent(man) == []  # no noisy confirmation
    event = await session.scalar(select(Outbox).where(Outbox.kind == "message.created").order_by(Outbox.id.desc()))
    assert event.payload["sender_id"] == man.id and event.payload["origin"] == "telegram"
    incoming = await session.scalar(select(Message).where(Message.sender_id == woman.id))
    await session.refresh(incoming)
    assert incoming.read_at is not None  # replying marks the partner's messages read


async def test_reply_to_a_notification_goes_to_that_chat(harness, session, make_profile, make_conversation):
    man = (await make_profile(gender=Gender.MALE)).user
    first = (await make_profile(gender=Gender.FEMALE, name="Фатима")).user
    second = (await make_profile(gender=Gender.FEMALE, name="Мариям")).user
    with_first = await make_conversation(man, first)
    with_second = await make_conversation(man, second)
    await _activate(session, man, with_first)
    note = await chat.send_message(session, with_second.id, second, "Салам", MessageOrigin.WEB)
    await session.commit()
    sender = await users.get_by_id(session, second.id)
    rendered = render.message_notification(sender, note)

    await harness.text(man, "Ва алейкум салам", reply_to=harness.bot_message(man, rendered.text, rendered.reply_markup))

    reply = await session.scalar(select(Message).where(Message.sender_id == man.id))
    assert reply.conversation_id == with_second.id  # not the active chat with Фатима
    await session.refresh(man)
    assert man.bot_active_conversation_id == with_second.id
    assert "Вы пишете: <b>Мариям</b>" in harness.last_text(man)


async def test_closed_conversation_clears_active_chat(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _activate(session, man, conversation)
    conversation.closed_at = conversation.created_at  # closed without clearing (e.g. a race)
    await session.commit()

    await harness.text(man, "Вы здесь?")

    assert "Общение в этом чате завершено." in harness.last_text(man)
    await session.refresh(man)
    assert man.bot_active_conversation_id is None
    assert (await session.scalars(select(Message))).all() == []


async def test_plain_text_without_active_chat_gets_hint(harness, make_pair):
    man, _ = await make_pair()
    await harness.text(man, "Привет")
    assert harness.last_text(man) == texts.NO_ACTIVE_CHAT


async def test_reply_button_sets_active_chat_and_stop_clears_it(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)

    await harness.press(man, ChatCb(action=ChatAction.REPLY, conversation_id=conversation.id).pack())
    await session.refresh(man)
    assert man.bot_active_conversation_id == conversation.id
    assert f"Вы пишете: <b>Фатима</b> (#{woman.id})" in harness.last_text(man)

    await harness.text(man, "/stop")
    await session.refresh(man)
    assert man.bot_active_conversation_id is None
    assert harness.last_text(man) == texts.CHAT_STOPPED


async def test_foreign_conversation_cannot_be_activated(harness, session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    stranger = (await make_profile()).user
    await harness.press(stranger, ChatCb(action=ChatAction.REPLY, conversation_id=conversation.id).pack())
    await session.refresh(stranger)
    assert stranger.bot_active_conversation_id is None
    assert harness.callback_answers()[-1].text == "Чат не найден."


async def test_chats_menu_lists_and_opens_history(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, woman, "Салам <b>брат</b>", MessageOrigin.WEB)
    await session.commit()

    await harness.text(man, texts.MENU_CHATS)
    data = button_data(harness.last_markup(man), "Фатима")
    assert "1 нов." in buttons(harness.last_markup(man))[0].text

    await harness.press(man, data)

    joined = "\n".join(harness.texts(man))
    assert "Салам &lt;b&gt;брат&lt;/b&gt;" in joined
    assert "Вы пишете: <b>Фатима</b>" in harness.last_text(man)
    await session.refresh(man)
    assert man.bot_active_conversation_id == conversation.id


async def test_close_conversation_from_bot(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await harness.press(man, ChatCb(action=ChatAction.CLOSE, conversation_id=conversation.id).pack())
    await harness.press(man, button_data(harness.last_markup(man), "Да, завершить"))
    await session.refresh(conversation)
    assert conversation.closed_at is not None and conversation.closed_by_id == man.id


async def test_media_is_rejected_even_in_active_chat(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await _activate(session, man, conversation)

    await harness.photo(man, caption="Это я")

    assert harness.last_text(man) == texts.MEDIA_REJECTED
    assert (await session.scalars(select(Message))).all() == []
