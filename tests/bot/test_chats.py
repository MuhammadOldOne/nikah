"""«Мой чат» and the plain-text relay: the only open chat gets any text (legacy behaviour), choosing
a chat when several are open, «Reply», contact filter, rate limit, media, message reports."""

from __future__ import annotations

from sqlalchemy import select

from app.bot import render, texts
from app.bot.callbacks import CatalogAction, CatalogCb, ChatAction, ChatCb, MessageReportCb, ProfileAction, ProfileCb
from app.enums import Gender, MessageOrigin, ReportReason
from app.models import Message, Outbox, Profile, Report
from app.services import chat, interests, users
from tests.bot.conftest import button_data, buttons


async def _activate(session, user, conversation) -> None:
    await chat.set_bot_active_conversation(session, user, conversation.id)
    await session.commit()


async def test_text_goes_to_the_only_open_chat_without_choosing(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, woman, "Ас-саляму алейкум", MessageOrigin.WEB)
    await session.commit()
    assert man.bot_active_conversation_id is None

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


async def test_menu_labels_and_commands_are_not_relayed(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    await make_conversation(man, woman)
    await harness.text(man, "Помощь")
    await harness.text(man, "/nonsense")
    assert harness.last_text(man) == texts.UNKNOWN_COMMAND
    assert (await session.scalars(select(Message))).all() == []


async def test_my_chat_shows_partner_card_history_and_close(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, woman, "Салам <b>брат</b>", MessageOrigin.WEB)
    await session.commit()

    await harness.text(man, "Мой чат")

    joined = "\n".join(harness.texts(man))
    assert "<b>Анкета №501</b> — Сестра" in joined and "Салам &lt;b&gt;брат&lt;/b&gt;" in joined
    assert harness.last_text(man) == "Открыт чат с анкетой №501. Пишите сюда — бот передаст."
    close = button_data(harness.last_markup(man), "Завершить чат")
    await harness.press(man, close)
    await harness.press(man, button_data(harness.last_markup(man), "Да, завершить"))
    await session.refresh(conversation)
    assert conversation.closed_at is not None and conversation.closed_by_id == man.id

    await harness.text(man, "💬 Чаты")  # the first new bot's label still works
    assert harness.last_text(man) == texts.NO_OPEN_CHATS
    await harness.text(man, "Вы здесь?")
    assert harness.last_text(man) == texts.NO_OPEN_CHAT


async def test_legacy_close_button_closes_the_only_chat(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await harness.press(woman, "chat:close")
    await harness.press(woman, button_data(harness.last_markup(woman), "Да, завершить"))
    await session.refresh(conversation)
    assert conversation.closed_at is not None
    await harness.press(woman, "chat:close")
    assert harness.callback_answers()[-1].text == "Открытого чата нет."


async def test_contacts_are_hidden_and_the_sender_is_told(harness, session, make_pair, make_conversation):
    man, woman = await make_pair()
    await make_conversation(man, woman)
    await harness.text(man, "Пишите на @brother_ahmad или +7 900 123-45-67")
    stored = await session.scalar(select(Message).where(Message.sender_id == man.id))
    assert "@brother_ahmad" not in stored.text and "[скрыто свидетелем]" in stored.text and stored.flagged
    assert harness.last_text(man) == "Часть текста была скрыта фильтром (телефоны/ссылки/@). Свидетель уведомлён."


async def test_rate_limit_and_media_texts(harness, session, make_pair, make_conversation, override_settings):
    override_settings(CHAT_RATE_LIMIT_PER_MIN=1)
    man, woman = await make_pair()
    await make_conversation(man, woman)
    await harness.text(man, "Первое")
    await harness.text(man, "Второе")
    assert harness.last_text(man) == "⚠️ Вы отправляете сообщения слишком часто. Подождите минуту и попробуйте снова."

    await harness.photo(man, caption="Это я")
    assert harness.last_text(man) == (
        "В этом боте разрешена только текстовая переписка. "
        "Медиа, голосовые и стикеры не передаются — это требование шариатского формата."
    )
    assert [m.text for m in (await session.scalars(select(Message))).all()] == ["Первое"]


async def test_message_report_button_reports_that_message(harness, session, make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    rude = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.WEB)
    await session.commit()

    await harness.press(woman, f"rep:{rude.id}")  # the legacy callback data

    report = await session.scalar(select(Report))
    assert (report.reporter_id, report.target_user_id, report.message_id) == (woman.id, man.id, rude.id)
    assert harness.callback_answers()[-1].text == texts.MESSAGE_REPORTED
    await session.refresh(rude)
    assert rude.flagged and "user report" in rude.flag_reason
    await harness.press(woman, MessageReportCb(message_id=rude.id).pack())
    assert "Вы уже пожаловались на это сообщение" in harness.callback_answers()[-1].text

    stranger = await make_user()
    await harness.press(stranger, f"rep:{rude.id}")
    assert harness.callback_answers()[-1].text == "Сообщение не найдено."
    await harness.press(woman, "rep:999999")
    assert harness.callback_answers()[-1].text == "Сообщение не найдено."


async def test_reply_to_a_relayed_message_goes_to_its_chat(
    harness, session, make_profile, make_conversation, override_settings
):
    override_settings(MAX_OPEN_CONVERSATIONS=2)
    man = (await make_profile(gender=Gender.MALE)).user
    first = (await make_profile(gender=Gender.FEMALE)).user
    second = (await make_profile(gender=Gender.FEMALE)).user
    with_first = await make_conversation(man, first)
    with_second = await make_conversation(man, second)
    await _activate(session, man, with_first)
    note = await chat.send_message(session, with_second.id, second, "Салам", MessageOrigin.WEB)
    await session.commit()
    rendered = render.message_notification(await users.get_by_id(session, second.id), note)  # only «Пожаловаться»

    await harness.text(man, "Ва алейкум салам", reply_to=harness.bot_message(man, rendered.text, rendered.reply_markup))

    reply = await session.scalar(select(Message).where(Message.sender_id == man.id))
    assert reply.conversation_id == with_second.id  # not the active chat with the first sister
    await session.refresh(man)
    assert man.bot_active_conversation_id == with_second.id
    assert "Вы пишете: <b>Анкета №502</b>" in harness.last_text(man)


async def test_several_open_chats_need_a_choice(harness, session, make_profile, make_conversation, override_settings):
    override_settings(MAX_OPEN_CONVERSATIONS=2)
    man = (await make_profile(gender=Gender.MALE)).user
    first = (await make_profile(gender=Gender.FEMALE)).user
    second = (await make_profile(gender=Gender.FEMALE)).user
    await make_conversation(man, first)
    with_second = await make_conversation(man, second)

    await harness.text(man, "Салам")
    assert harness.last_text(man) == texts.CHOOSE_CHAT
    assert (await session.scalars(select(Message))).all() == []

    await harness.text(man, "Мой чат")
    await harness.press(man, button_data(harness.last_markup(man), "Анкета №502"))
    assert "Вы пишете: <b>Анкета №502</b>" in harness.last_text(man)
    await harness.text(man, "Салам, сестра")
    sent = await session.scalar(select(Message).where(Message.sender_id == man.id))
    assert sent.conversation_id == with_second.id

    await harness.text(man, "/stop")
    await session.refresh(man)
    assert man.bot_active_conversation_id is None and harness.last_text(man) == texts.CHAT_STOPPED


async def test_stop_with_the_only_chat_explains(harness, make_pair, make_conversation):
    man, woman = await make_pair()
    await make_conversation(man, woman)
    await harness.text(man, "/stop")
    assert harness.last_text(man) == texts.STOP_SINGLE_CHAT


async def test_closed_active_chat_is_cleared(harness, session, make_profile, make_conversation, override_settings):
    override_settings(MAX_OPEN_CONVERSATIONS=2)
    man = (await make_profile(gender=Gender.MALE)).user
    first = (await make_profile(gender=Gender.FEMALE)).user
    second = (await make_profile(gender=Gender.FEMALE)).user
    with_first = await make_conversation(man, first)
    await make_conversation(man, second)
    await _activate(session, man, with_first)
    await harness.press(man, ChatCb(action=ChatAction.REPLY, conversation_id=with_first.id).pack())
    header = harness.sent(man)[-1]
    with_first.closed_at = with_first.created_at  # closed without clearing (e.g. a race)
    await session.commit()

    await harness.text(man, "Вы здесь?", reply_to=harness.bot_message(man, header.text, header.reply_markup))

    assert "Общение в этом чате завершено." in harness.last_text(man)
    await session.refresh(man)
    assert man.bot_active_conversation_id is None
    assert (await session.scalars(select(Message))).all() == []


async def test_foreign_conversation_cannot_be_activated(harness, session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    stranger = (await make_profile()).user
    await harness.press(stranger, ChatCb(action=ChatAction.REPLY, conversation_id=conversation.id).pack())
    await session.refresh(stranger)
    assert stranger.bot_active_conversation_id is None
    assert harness.callback_answers()[-1].text == "Чат не найден."


async def _chat_with_second_suitor(session, make_profile, make_conversation):
    """Sister (№500) with an open chat with Ахмад (№501) and Юсуф's profile (№502) in her catalog."""
    sister = (await make_profile(gender=Gender.FEMALE)).user
    ahmad = (await make_profile(gender=Gender.MALE)).user
    yusuf = (await make_profile(gender=Gender.MALE)).user
    conversation = await make_conversation(ahmad, sister)
    return sister, ahmad, yusuf, conversation


async def test_reply_button_ends_an_open_dialog(harness, session, make_profile, make_conversation):
    sister, _, _, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    await harness.press(sister, CatalogCb(action=CatalogAction.REPORT, number=502).pack())
    await harness.press(sister, f"rr:502:{ReportReason.FAKE.value}")
    assert harness.edited_texts()[-1].endswith(texts.REPORT_COMMENT_PROMPT)

    await harness.press(sister, ChatCb(action=ChatAction.REPLY, conversation_id=conversation.id).pack())
    assert harness.last_text(sister).startswith(texts.CANCELLED)
    assert "Вы пишете: <b>Анкета №501</b>" in harness.last_text(sister)
    await harness.text(sister, "Ва алейкум ассалям! Мой отец хочет поговорить с вашей семьёй")

    sent = (await session.scalars(select(Message).where(Message.sender_id == sister.id))).all()
    assert [(m.conversation_id, m.text) for m in sent] == [
        (conversation.id, "Ва алейкум ассалям! Мой отец хочет поговорить с вашей семьёй")
    ]
    assert (await session.scalars(select(Report))).all() == []


async def test_reply_to_notification_inside_a_dialog_goes_to_the_chat(
    harness, session, make_profile, make_conversation
):
    sister, ahmad, _, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    note = await chat.send_message(session, conversation.id, ahmad, "Как ваши дела?", MessageOrigin.WEB)
    await session.commit()
    rendered = render.message_notification(await users.get_by_id(session, ahmad.id), note)
    await harness.press(sister, CatalogCb(action=CatalogAction.REPORT, number=502).pack())
    await harness.press(sister, f"rr:502:{ReportReason.FAKE.value}")

    reply_to = harness.bot_message(sister, rendered.text, rendered.reply_markup)
    await harness.text(sister, "Всё хорошо, а у вас?", reply_to=reply_to)

    assert (await session.scalars(select(Report))).all() == []
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert (reply.conversation_id, reply.text) == (conversation.id, "Всё хорошо, а у вас?")
    assert harness.last_text(sister) == f"{texts.CANCELLED}\nОтвет отправлен."


async def test_opening_my_chat_ends_the_profile_wizard(harness, session, make_profile, make_conversation):
    sister, _, _, conversation = await _chat_with_second_suitor(session, make_profile, make_conversation)
    await harness.press(sister, ProfileCb(action=ProfileAction.EDIT).pack())
    await harness.text(sister, "Мой чат")
    await harness.text(sister, "Ахмад, здравствуйте")

    profile = await session.scalar(select(Profile).where(Profile.user_id == sister.id))
    await session.refresh(profile)
    assert profile.city == "Казань"  # the text did not go into the wizard
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert reply is not None and reply.conversation_id == conversation.id


async def test_reply_to_a_wali_copy_is_not_relayed(harness, session, make_profile, make_conversation):
    ward = (await make_profile(gender=Gender.FEMALE)).user
    suitor = (await make_profile(gender=Gender.MALE)).user
    wali = (await make_profile(gender=Gender.MALE)).user
    own_partner = (await make_profile(gender=Gender.FEMALE)).user
    ward_chat = await make_conversation(suitor, ward)
    await make_conversation(wali, own_partner)
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
    interest = await interests.send_interest(session, yusuf, sister.id, "Ас-саляму алейкум")
    await session.commit()
    sender = await users.get_by_id(session, yusuf.id)
    card = render.interest_notification(interest, sender.profile, has_guardian=False)

    reply_to = harness.bot_message(sister, "Вами заинтересовалась анкета №502", card.reply_markup)
    await harness.text(sister, "Ва алейкум ассалам, расскажите о своей семье", reply_to=reply_to)

    assert (await session.scalars(select(Message).where(Message.sender_id == sister.id))).all() == []
    assert harness.last_text(sister) == texts.REPLY_NOT_TO_CHAT
    assert [b.text for b in buttons(card.reply_markup)] == ["Принять", "Отклонить"]

    # A reply to a bot message without buttons (the chat history, a witness message) goes to the chat.
    await harness.text(sister, "Ахмад, вы здесь?", reply_to=harness.bot_message(sister, "Ахмад · 12:00\nСалам"))
    reply = await session.scalar(select(Message).where(Message.sender_id == sister.id))
    assert reply is not None and reply.conversation_id == conversation.id
