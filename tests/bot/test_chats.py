"""Chats in the bot: list, active chat, text relay with origin=telegram, /stop, media rejection."""

from __future__ import annotations

from sqlalchemy import select

from app.bot import render, texts
from app.bot.callbacks import CatalogAction, CatalogCb, ChatAction, ChatCb, ProfileAction, ProfileCb, ReportCb
from app.enums import Gender, MessageOrigin, ReportReason
from app.models import Interest, Message, Outbox, Profile, Report
from app.services import chat, interests, users
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


async def _chat_with_second_suitor(session, make_profile, make_conversation):
    """Sister with an open chat with Ахмад and Юсуф's profile in her catalog."""
    sister = (await make_profile(gender=Gender.FEMALE, name="Фатима")).user
    ahmad = (await make_profile(gender=Gender.MALE, name="Ахмад")).user
    yusuf = (await make_profile(gender=Gender.MALE, name="Юсуф")).user
    conversation = await make_conversation(ahmad, sister)
    return sister, ahmad, yusuf, conversation


async def test_reply_button_ends_an_open_dialog(harness, session, make_profile, make_conversation):
    sister, _, yusuf, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    await harness.press(sister, CatalogCb(action=CatalogAction.INTEREST, user_id=yusuf.id).pack())
    assert harness.last_text(sister) == texts.INTEREST_MESSAGE_PROMPT

    await harness.press(sister, ChatCb(action=ChatAction.REPLY, conversation_id=conversation.id).pack())
    assert harness.last_text(sister).startswith(texts.CANCELLED)
    assert "Вы пишете: <b>Ахмад</b>" in harness.last_text(sister)
    await harness.text(sister, "Ва алейкум ассалям! Мой отец хочет поговорить с вашей семьёй")

    sent = (await session.scalars(select(Message).where(Message.sender_id == sister.id))).all()
    assert [(m.conversation_id, m.text) for m in sent] == [
        (conversation.id, "Ва алейкум ассалям! Мой отец хочет поговорить с вашей семьёй")
    ]
    assert (await session.scalars(select(Interest).where(Interest.from_user_id == sister.id))).all() == []


async def test_reply_to_notification_inside_a_dialog_goes_to_the_chat(
    harness, session, make_profile, make_conversation
):
    sister, ahmad, yusuf, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    note = await chat.send_message(session, conversation.id, ahmad, "Как ваши дела?", MessageOrigin.WEB)
    await session.commit()
    rendered = render.message_notification(await users.get_by_id(session, ahmad.id), note)
    await harness.press(sister, CatalogCb(action=CatalogAction.REPORT, user_id=yusuf.id).pack())
    await harness.press(sister, ReportCb(user_id=yusuf.id, reason=ReportReason.FAKE).pack())

    reply_to = harness.bot_message(sister, rendered.text, rendered.reply_markup)
    await harness.text(sister, "Всё хорошо, а у вас?", reply_to=reply_to)

    assert (await session.scalars(select(Report))).all() == []
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert (reply.conversation_id, reply.text) == (conversation.id, "Всё хорошо, а у вас?")
    assert harness.last_text(sister).startswith(f"{texts.CANCELLED}\nОтвет отправлен.")


async def test_opening_a_chat_ends_the_profile_wizard(harness, session, make_profile, make_conversation):
    sister, _, _, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    await harness.press(sister, ProfileCb(action=ProfileAction.EDIT).pack())
    await harness.press(sister, ChatCb(action=ChatAction.OPEN, conversation_id=conversation.id).pack())
    await harness.text(sister, "Ахмад, здравствуйте")

    profile = await session.scalar(select(Profile).where(Profile.user_id == sister.id))
    await session.refresh(profile)
    assert profile.name == "Фатима"
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert reply is not None and reply.conversation_id == conversation.id


async def test_reply_to_a_wali_copy_is_not_relayed(harness, session, make_profile, make_conversation):
    ward = (await make_profile(gender=Gender.FEMALE, name="Мариям")).user
    suitor = (await make_profile(gender=Gender.MALE, name="Ибрагим")).user
    wali = (await make_profile(gender=Gender.MALE, name="Хасан")).user
    own_partner = (await make_profile(gender=Gender.FEMALE, name="Аиша")).user
    ward_chat = await make_conversation(suitor, ward)
    own_chat = await make_conversation(wali, own_partner)
    await _activate(session, wali, own_chat)
    copied = await chat.send_message(session, ward_chat.id, suitor, "Салам", MessageOrigin.WEB)
    await session.commit()
    copy = render.guardian_copy(
        await users.get_by_id(session, ward.id), await users.get_by_id(session, suitor.id), suitor, copied
    )

    reply_to = harness.bot_message(wali, copy.text.replace("<b>", "").replace("</b>", ""), copy.reply_markup)
    await harness.text(wali, "Брат, давайте познакомимся семьями", reply_to=reply_to)

    assert (await session.scalars(select(Message).where(Message.sender_id == wali.id))).all() == []
    assert harness.last_text(wali) == texts.REPLY_TO_GUARDIAN_COPY


async def test_reply_to_an_interest_card_is_not_relayed(harness, session, make_profile, make_conversation):
    sister, _, yusuf, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    await _activate(session, sister, conversation)
    interest = await interests.send_interest(session, yusuf, sister.id, "Ас-саляму алейкум")
    await session.commit()
    sender = await users.get_by_id(session, yusuf.id)
    card = render.interest_notification(interest, sender.profile, has_guardian=False)

    reply_to = harness.bot_message(sister, "💌 Отклик", card.reply_markup)
    await harness.text(sister, "Ва алейкум ассалам, расскажите о своей семье", reply_to=reply_to)

    assert (await session.scalars(select(Message).where(Message.sender_id == sister.id))).all() == []
    assert harness.last_text(sister) == texts.REPLY_NOT_TO_CHAT

    # A reply to a bot message without buttons (e.g. the chat history) still goes to the active chat.
    await harness.text(sister, "Ахмад, вы здесь?", reply_to=harness.bot_message(sister, "Ахмад · 12:00\nСалам"))
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert reply is not None and reply.conversation_id == conversation.id
