"""Witness (свидетель): admin-only actions and the events that reach the witness chat."""

import pytest
from sqlalchemy import select

from app.enums import ConversationCloseReason, Gender, MessageKind, MessageOrigin, ReportReason
from app.models import Message, Outbox
from app.services import chat, guardians, interests, moderation, witness
from app.services.errors import (
    ConversationClosed,
    ConversationNotFound,
    InterestBlockedByWitness,
    InvalidInput,
    MessageNotFound,
    WitnessOnly,
)


async def rows(session, kind: str) -> list[Outbox]:
    return list((await session.scalars(select(Outbox).where(Outbox.kind == kind).order_by(Outbox.id))).all())


async def link_guardian(session, ward, guardian) -> None:
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], guardian)


@pytest.fixture
async def chat_and_admin(make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    return man, woman, conversation, await make_user(is_admin=True)


async def test_witness_actions_are_admin_only(session, chat_and_admin, make_user, override_settings):
    man, woman, conversation, _ = chat_and_admin
    message = await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    for actor in (man, woman, await make_user()):
        with pytest.raises(WitnessOnly, match=r"Только для свидетеля\."):
            await witness.witness_flag(session, actor, message.id)
        with pytest.raises(WitnessOnly):
            await witness.witness_say(session, actor, conversation.id, "Помните об Аллахе")
        with pytest.raises(WitnessOnly):
            await witness.witness_close(session, actor, conversation.id)
    listed = await make_user(telegram_id=4242)  # admin by ADMIN_TELEGRAM_IDS only
    override_settings(ADMIN_TELEGRAM_IDS=[4242])
    flagged = await witness.witness_flag(session, listed, message.id)
    assert flagged.flagged and flagged.flag_reason == "witness"


async def test_flag_is_idempotent_and_keeps_earlier_reasons(session, chat_and_admin):
    man, _, conversation, admin = chat_and_admin
    message = await chat.send_message(session, conversation.id, man, "Пиши @ahmad_k", MessageOrigin.TELEGRAM)
    await witness.witness_flag(session, admin, message.id)
    await witness.witness_flag(session, admin, message.id)
    assert message.flag_reason == "contact-leak filter, witness" and message.contacts_hidden
    with pytest.raises(MessageNotFound):
        await witness.witness_flag(session, admin, 999999)


async def test_witness_message_reaches_both_participants_and_their_walis(session, chat_and_admin, make_user):
    man, woman, conversation, admin = chat_and_admin
    wali = await make_user()
    await link_guardian(session, woman, wali)
    said = await witness.witness_say(session, admin, conversation.id, "  Помните: цель — никах.  ")
    assert (said.kind, said.sender_id, said.text, said.origin) == (
        MessageKind.WITNESS,
        None,
        "Помните: цель — никах.",
        MessageOrigin.TELEGRAM,
    )
    assert said.is_witness and conversation.last_message_at is not None
    [event] = await rows(session, "message.witness")
    assert event.payload == {
        "conversation_id": conversation.id,
        "message_id": said.id,
        "recipient_ids": sorted([man.id, woman.id]),
        "guardians": [{"ward_user_id": woman.id, "guardian_user_id": wali.id}],
        "user_ids": sorted([man.id, woman.id]),
    }
    # Both participants see it in the conversation (site and bot history); it is nobody's unread.
    assert [m.id for m in await chat.list_messages(session, conversation)] == [said.id]
    assert await chat.unread_total(session, man) == 0
    assert await rows(session, "witness.copy") == []  # the witness does not get a copy of itself
    with pytest.raises(InvalidInput):
        await witness.witness_say(session, admin, conversation.id, "   ")
    with pytest.raises(ConversationNotFound):
        await witness.witness_say(session, admin, 999999, "Текст")


async def test_witness_close_ends_the_chat_and_the_pair(session, chat_and_admin):
    man, woman, conversation, admin = chat_and_admin
    await chat.set_bot_active_conversation(session, man, conversation.id)
    closed = await witness.witness_close(session, admin, conversation.id)
    assert closed.close_reason == ConversationCloseReason.CLOSED_BY_WITNESS and closed.closed_by_id == admin.id
    await session.refresh(man)
    assert man.bot_active_conversation_id is None
    [event] = await rows(session, "conversation.closed")
    assert event.payload["reason"] == "closed_by_witness" and event.payload["user_ids"] == sorted([man.id, woman.id])
    [notice] = await rows(session, "witness.conversation_closed")
    assert notice.payload == {
        "conversation_id": conversation.id,
        "closed_by_id": admin.id,
        "reason": "closed_by_witness",
        "user_ids": [],
    }
    with pytest.raises(ConversationClosed, match=r"Чат уже закрыт\."):
        await witness.witness_close(session, admin, conversation.id)
    # A closed chat may still get a witness message (e.g. why it was closed).
    await witness.witness_say(session, admin, conversation.id, "Чат закрыт за нарушение правил.")
    # Neither side can open a new chat with the other through interests.
    for sender, recipient in ((man, woman), (woman, man)):
        with pytest.raises(InterestBlockedByWitness):
            await interests.send_interest(session, sender, recipient.id)


async def test_every_user_message_gets_exactly_one_witness_copy(session, chat_and_admin):
    man, woman, conversation, _ = chat_and_admin
    first = await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    second = await chat.send_message(session, conversation.id, woman, "Ва алейкум", MessageOrigin.TELEGRAM)
    copies = await rows(session, "witness.copy")
    assert [row.payload for row in copies] == [
        {
            "conversation_id": conversation.id,
            "message_id": first.id,
            "sender_id": man.id,
            "recipient_id": woman.id,
            "origin": "web",
            "user_ids": [],
        },
        {
            "conversation_id": conversation.id,
            "message_id": second.id,
            "sender_id": woman.id,
            "recipient_id": man.id,
            "origin": "telegram",
            "user_ids": [],
        },
    ]
    assert len(await rows(session, "message.created")) == 2


async def test_opening_and_closing_notify_the_witness(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    conversation = await interests.accept(session, interest.id, woman)
    [opened] = await rows(session, "witness.conversation_opened")
    assert opened.payload == {
        "conversation_id": conversation.id,
        "interest_id": interest.id,
        "user_a_id": min(man.id, woman.id),
        "user_b_id": max(man.id, woman.id),
        "user_ids": [],
    }
    await chat.close_conversation(session, conversation, man)
    [closed] = await rows(session, "witness.conversation_closed")
    assert closed.payload["reason"] == "closed_by_user" and closed.payload["closed_by_id"] == man.id


async def test_message_report_flags_and_notifies_witness_and_admins(session, chat_and_admin, make_profile):
    man, woman, conversation, admin = chat_and_admin
    message = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.TELEGRAM)
    report = await moderation.create_report(session, woman, man.id, ReportReason.INSULT, message_id=message.id)
    assert report.message_id == message.id
    assert message.flagged and message.flag_reason == "user report"
    [admins] = await rows(session, "report.created")
    assert admins.payload["message_id"] == message.id
    [notice] = await rows(session, "witness.report")
    assert notice.payload == {"report_id": report.id, "message_id": message.id, "user_ids": []}
    said = await witness.witness_say(session, admin, conversation.id, "Предупреждение")
    with pytest.raises(MessageNotFound):  # a witness message is not anyone's message to report
        await moderation.create_report(session, woman, man.id, ReportReason.INSULT, message_id=said.id)
    other = (await make_profile(gender=Gender.MALE)).user
    with pytest.raises(MessageNotFound):  # only a participant, only the partner's message
        await moderation.create_report(session, other, man.id, ReportReason.INSULT, message_id=message.id)
    with pytest.raises(MessageNotFound):
        await moderation.create_report(session, man, woman.id, ReportReason.INSULT, message_id=message.id)
    assert (await session.scalars(select(Message).where(Message.flagged))).all() == [message]
