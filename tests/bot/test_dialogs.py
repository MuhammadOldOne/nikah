"""Dialogs next to an open chat: they survive restarts (the FSM lives in the database), never take
chat text meant for the partner as an interest message, and the consent gate keeps every message.
Also replies to the legacy bot's chat messages and repeated interests (docs/LEGACY_BOT.md §2.7)."""

from __future__ import annotations

from datetime import timedelta

from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

from app.bot import render, texts
from app.bot.callbacks import GIVE_CONSENT, CatalogAction, CatalogCb, ChatAction, ChatCb
from app.bot.dispatcher import create_dispatcher
from app.bot.storage import DatabaseStorage, purge_expired
from app.enums import Gender, MessageOrigin, ReportReason
from app.models import BotDialogState, Interest, Message, Report, User
from app.services import chat, interests, users
from tests.bot.conftest import BOT_ID, BotHarness, button_data


def _restart(harness: BotHarness) -> None:
    """A new process: a fresh dispatcher (and storage) on the same database."""
    harness.dispatcher = create_dispatcher()


async def _chat_texts(session, sender: User) -> list[tuple[int, str]]:
    rows = await session.scalars(select(Message).where(Message.sender_id == sender.id).order_by(Message.id))
    return [(m.conversation_id, m.text) for m in rows]


# --- Interests next to an open chat (finding: chat text sent to another profile) --------------------


async def test_with_an_open_chat_an_interest_is_sent_at_once_and_text_stays_in_the_chat(
    harness, session, make_pair, make_profile, make_conversation
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    other = await make_profile(gender=Gender.FEMALE)

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=other.number).pack())
    answer = harness.callback_answers()[-1]
    assert answer.show_alert and texts.INTEREST_SENT_WHILE_CHATTING in answer.text
    assert texts.INTEREST_MESSAGE_PROMPT not in harness.texts(man)
    sent = await session.scalar(select(Interest).where(Interest.to_user_id == other.user_id))
    assert sent is not None and sent.message is None

    await harness.text(man, "Как прошёл ваш день? Мои родители хотят познакомиться")
    assert await _chat_texts(session, man) == [
        (conversation.id, "Как прошёл ваш день? Мои родители хотят познакомиться")
    ]


async def test_a_chat_opening_during_the_interest_question_keeps_the_text_from_both(
    harness, session, make_pair, make_profile
):
    man, woman = await make_pair()
    other = await make_profile(gender=Gender.FEMALE)
    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=other.number).pack())
    assert harness.last_text(man) == texts.INTEREST_MESSAGE_PROMPT
    # Meanwhile the sister accepts his earlier interest: a chat opens.
    earlier = await interests.send_interest(session, man, woman.id)
    await interests.accept(session, earlier.id, woman)
    await session.commit()

    await harness.text(man, "Ассаляму алейкум, как вы?")
    assert harness.last_text(man) == texts.INTEREST_MESSAGE_NOT_SENT.format(number=other.number)
    assert await _chat_texts(session, man) == []
    assert await session.scalar(select(Interest).where(Interest.to_user_id == other.user_id)) is None
    # «Без сообщения» names the profile: it still works after the dialog ended.
    await harness.press(man, button_data(harness.last_markup(man), "Без сообщения"))
    sent = await session.scalar(select(Interest).where(Interest.to_user_id == other.user_id))
    assert sent is not None and sent.message is None


async def test_a_repeated_interest_is_refused_before_a_message_is_asked(harness, session, make_pair):
    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id)
    await session.commit()
    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=woman.profile.number).pack())
    assert harness.callback_answers()[-1].text == "Между вами уже есть отклик или переписка."
    assert harness.texts(man) == []


# --- The dialog state survives a restart ------------------------------------------------------------


async def test_a_report_comment_typed_after_a_restart_is_not_sent_to_the_partner(
    harness, session, make_pair, make_conversation
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await harness.press(woman, ChatCb(action=ChatAction.REPORT, conversation_id=conversation.id).pack())
    await harness.press(woman, f"rr:{man.profile.number}:{ReportReason.FRAUD.value}")
    _restart(harness)

    await harness.text(woman, "Он просит перевести деньги на карту, похоже на мошенника")

    assert await _chat_texts(session, woman) == []
    report = await session.scalar(select(Report))
    assert (report.target_user_id, report.reason, report.comment) == (
        man.id,
        ReportReason.FRAUD,
        "Он просит перевести деньги на карту, похоже на мошенника",
    )
    assert harness.last_text(woman) == texts.REPORT_SENT


async def test_dialog_states_expire_are_purged_and_go_with_the_account(harness, session, make_user):
    user = await make_user()
    storage = DatabaseStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=user.telegram_id, user_id=user.telegram_id)
    await storage.set_state(key, "ReportForm:comment")
    await storage.update_data(key, {"report": {"number": 501, "reason": "fraud"}})
    assert await storage.get_state(key) == "ReportForm:comment"
    assert await storage.get_data(key) == {"report": {"number": 501, "reason": "fraud"}}

    await session.execute(update(BotDialogState).values(updated_at=BotDialogState.updated_at - timedelta(days=8)))
    await session.commit()
    assert (await storage.get_state(key), await storage.get_data(key)) == (None, {})
    assert await purge_expired(session) == 1
    await session.commit()

    await storage.set_state(key, "ReportForm:comment")
    await storage.set_state(key, None)  # an empty state without data leaves no row
    assert (await session.scalars(select(BotDialogState))).all() == []
    await storage.update_data(key, {"draft": "О себе"})
    await users.delete_account(session, user)
    await session.commit()
    assert (await session.scalars(select(BotDialogState))).all() == []


# --- The consent gate keeps every message, each for its chat -----------------------------------------


async def _migrated_in_chat(session, make_user, make_profile, make_conversation) -> tuple[User, User, int]:
    """A migrated brother (no consent) with an open chat moved from the legacy bot."""
    man = await make_user(consent=False)
    await make_profile(man, gender=Gender.MALE)
    sister = (await make_profile(gender=Gender.FEMALE)).user
    man.consent_at = sister.consent_at
    await session.commit()
    conversation = await make_conversation(man, sister)
    man.consent_at = None
    await session.commit()
    return man, sister, conversation.id


async def test_every_message_before_consent_is_delivered_in_order_even_after_a_restart(
    harness, session, make_user, make_profile, make_conversation
):
    man, _, conversation_id = await _migrated_in_chat(session, make_user, make_profile, make_conversation)
    for text in ("Первое сообщение", "Второе сообщение", "Третье сообщение"):
        await harness.text(man, text)
    prompts = harness.texts(man)
    assert prompts[0].startswith("<b>Согласие на обработку персональных данных</b>")
    assert prompts[1:] == [texts.CONSENT_MESSAGE_WAITS, texts.CONSENT_MESSAGE_WAITS]
    assert await _chat_texts(session, man) == []
    _restart(harness)

    await harness.press(man, GIVE_CONSENT)
    assert await _chat_texts(session, man) == [
        (conversation_id, "Первое сообщение"),
        (conversation_id, "Второе сообщение"),
        (conversation_id, "Третье сообщение"),
    ]


async def test_the_consent_gate_routes_replies_like_the_relay(
    harness, session, make_user, make_profile, make_conversation
):
    man, sister, conversation_id = await _migrated_in_chat(session, make_user, make_profile, make_conversation)
    suitor = (await make_profile(gender=Gender.FEMALE)).user
    card = await interests.send_interest(session, suitor, man.id)
    await session.commit()
    # A «Reply» to an interest card is refused (as with consent), not kept for the open chat.
    rendered = render.interest_notification(card, suitor.profile, has_guardian=False)
    await harness.text(
        man, "Ответ для анкеты с отклика", reply_to=harness.bot_message(man, rendered.text, rendered.reply_markup)
    )
    assert harness.last_text(man) == texts.REPLY_NOT_TO_CHAT
    # A «Reply» to a message of a chat that has closed since stays addressed to that chat.
    note = await chat.send_message(session, conversation_id, sister, "Салам", MessageOrigin.WEB)
    await session.commit()
    relayed = render.message_notification(await users.get_by_id(session, sister.id), note)
    old = harness.bot_message(man, relayed.text, relayed.reply_markup)
    conversation = await chat.get_conversation_for_participant(session, conversation_id, sister)
    await chat.close_conversation(session, conversation, sister)
    await session.commit()
    await harness.text(man, "Ответ в закрытый чат", reply_to=old)

    await harness.press(man, GIVE_CONSENT)
    assert await _chat_texts(session, man) == []
    assert any(text.startswith("Общение в этом чате завершено") for text in harness.texts(man))


# --- Replies to the legacy bot's chat messages ------------------------------------------------------


async def test_a_reply_to_the_legacy_chat_opened_message_goes_to_the_only_chat(
    harness, session, make_pair, make_conversation
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    legacy = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Завершить общение", callback_data="chat:close")]]
    )
    for text in ("Чат открыт между анкетами №500 и №501. Просто пишите сюда текстом.", "Открыт чат с анкетой №501."):
        await harness.text(man, f"Ассаляму алейкум! ({text[:10]})", reply_to=harness.bot_message(man, text, legacy))
    assert [cid for cid, _ in await _chat_texts(session, man)] == [conversation.id, conversation.id]
    assert texts.REPLY_NOT_TO_CHAT not in harness.texts(man)
