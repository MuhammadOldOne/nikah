import pytest
from sqlalchemy import select

from app.enums import ConversationCloseReason, Gender, MessageOrigin
from app.models import Outbox
from app.services import chat, guardians, users
from app.services.errors import (
    ConversationClosed,
    ConversationNotFound,
    InvalidInput,
    PartnerDeleted,
    UserBanned,
)


async def link_guardian(session, ward, guardian) -> None:
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], guardian)


async def test_send_message_and_event_payload(session, make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    wali = await make_user()
    await link_guardian(session, woman, wali)

    message = await chat.send_message(session, conversation.id, man, "  Ассаляму алейкум  ", MessageOrigin.WEB)
    assert message.text == "Ассаляму алейкум" and message.origin == MessageOrigin.WEB
    assert message.flagged is False and message.read_at is None
    assert conversation.last_message_at is not None

    event = await session.scalar(select(Outbox).where(Outbox.kind == "message.created"))
    assert event.payload == {
        "conversation_id": conversation.id,
        "message_id": message.id,
        "sender_id": man.id,
        "recipient_id": woman.id,
        "origin": "web",
        "guardians": [{"ward_user_id": woman.id, "guardian_user_id": wali.id}],
        "user_ids": sorted([man.id, woman.id]),
    }


async def test_guardians_of_both_participants_are_listed(session, make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    man_wali, woman_wali = await make_user(), await make_user()
    await link_guardian(session, man, man_wali)
    await link_guardian(session, woman, woman_wali)
    await chat.send_message(session, conversation.id, woman, "Ва алейкум ассалям", MessageOrigin.TELEGRAM)
    event = await session.scalar(select(Outbox).where(Outbox.kind == "message.created"))
    assert sorted(event.payload["guardians"], key=lambda g: g["ward_user_id"]) == sorted(
        [
            {"ward_user_id": man.id, "guardian_user_id": man_wali.id},
            {"ward_user_id": woman.id, "guardian_user_id": woman_wali.id},
        ],
        key=lambda g: g["ward_user_id"],
    )
    assert event.payload["origin"] == "telegram"


async def test_only_participants_can_write_and_read(session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    stranger = (await make_profile(gender=Gender.MALE)).user
    with pytest.raises(ConversationNotFound):
        await chat.send_message(session, conversation.id, stranger, "привет", MessageOrigin.WEB)
    with pytest.raises(ConversationNotFound):
        await chat.get_conversation_for_participant(session, conversation.id, stranger)
    with pytest.raises(ConversationNotFound):
        await chat.send_message(session, 999999, man, "привет", MessageOrigin.WEB)
    loaded = await chat.get_conversation_for_participant(session, conversation.id, woman)
    assert {loaded.user_a.profile.name, loaded.user_b.profile.name} == {"Ахмад", "Фатима"}


async def test_message_validation_and_flagging(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    with pytest.raises(InvalidInput):
        await chat.send_message(session, conversation.id, man, "   ", MessageOrigin.WEB)
    with pytest.raises(InvalidInput):
        await chat.send_message(session, conversation.id, man, "а" * 2001, MessageOrigin.WEB)
    flagged = await chat.send_message(
        session, conversation.id, man, "Переведи деньги на карту 4276 1234 5678 9012", MessageOrigin.WEB
    )
    assert flagged.flagged is True
    assert "4276 1234 5678 9012" in flagged.flag_reason
    # Flagged messages are delivered anyway.
    assert await session.scalar(select(Outbox).where(Outbox.kind == "message.created")) is not None


async def test_closed_conversation_and_banned_sender(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    man.bot_active_conversation_id = conversation.id
    await session.flush()
    await chat.close_conversation(session, conversation, woman)
    assert conversation.closed_by_id == woman.id
    assert conversation.close_reason == ConversationCloseReason.CLOSED_BY_USER
    assert man.bot_active_conversation_id is None
    with pytest.raises(ConversationClosed):
        await chat.send_message(session, conversation.id, man, "привет", MessageOrigin.TELEGRAM)
    with pytest.raises(ConversationClosed):
        await chat.close_conversation(session, conversation, man)
    event = await session.scalar(select(Outbox).where(Outbox.kind == "conversation.closed"))
    assert event.payload == {
        "conversation_id": conversation.id,
        "closed_by_id": woman.id,
        "reason": "closed_by_user",
        "user_ids": sorted([man.id, woman.id]),
    }

    man.is_banned = True
    with pytest.raises(UserBanned):
        await chat.send_message(session, conversation.id, man, "привет", MessageOrigin.WEB)


async def test_partner_deleted(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    conversation_id, woman_id = conversation.id, woman.id
    await users.delete_account(session, man)
    await session.commit()
    woman = await users.get_by_id(session, woman_id)
    with pytest.raises(PartnerDeleted):
        await chat.send_message(session, conversation_id, woman, "вы тут?", MessageOrigin.WEB)
    with pytest.raises(PartnerDeleted):
        await chat.set_bot_active_conversation(session, woman, conversation_id)


async def test_list_messages_pagination(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    sent = []
    for index in range(7):
        sender = man if index % 2 == 0 else woman
        sent.append(await chat.send_message(session, conversation.id, sender, f"сообщение {index}", MessageOrigin.WEB))
    ids = [message.id for message in sent]

    assert [m.id for m in await chat.list_messages(session, conversation)] == ids
    assert [m.id for m in await chat.list_messages(session, conversation, limit=3)] == ids[-3:]
    assert [m.id for m in await chat.list_messages(session, conversation, after_id=ids[2])] == ids[3:]
    assert [m.id for m in await chat.list_messages(session, conversation, after_id=ids[2], limit=2)] == ids[3:5]
    assert [m.id for m in await chat.list_messages(session, conversation, before_id=ids[4], limit=2)] == ids[2:4]


async def test_unread_counts_and_mark_read(session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    other_woman = (await make_profile(gender=Gender.FEMALE)).user
    first = await make_conversation(man, woman)
    second = await make_conversation(man, other_woman)
    await chat.send_message(session, first.id, woman, "1", MessageOrigin.WEB)
    await chat.send_message(session, first.id, woman, "2", MessageOrigin.TELEGRAM)
    await chat.send_message(session, second.id, other_woman, "3", MessageOrigin.WEB)
    await chat.send_message(session, first.id, man, "4", MessageOrigin.WEB)

    assert await chat.unread_total(session, man) == 3
    assert await chat.unread_total(session, woman) == 1
    summaries = await chat.list_conversations(session, man)
    assert [s.conversation.id for s in summaries] == [first.id, second.id]  # most recent activity first
    assert [s.unread_count for s in summaries] == [2, 1]
    assert summaries[0].last_message.text == "4"
    assert summaries[0].partner.id == woman.id and summaries[0].partner_profile.name == "Фатима"
    assert summaries[0].partner_has_guardian is False

    assert await chat.mark_read(session, first, man) == 2
    assert await chat.mark_read(session, first, man) == 0
    assert await chat.unread_total(session, man) == 1
    assert await chat.unread_total(session, woman) == 1  # her own messages are not affected


async def test_partner_guardian_badge_in_list(session, make_pair, make_conversation, make_user):
    man, woman = await make_pair()
    await make_conversation(man, woman)
    await link_guardian(session, woman, await make_user())
    summary = (await chat.list_conversations(session, man))[0]
    assert summary.partner_has_guardian is True
    assert (await chat.list_conversations(session, woman))[0].partner_has_guardian is False


async def test_set_bot_active_conversation(session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.set_bot_active_conversation(session, man, conversation.id)
    assert man.bot_active_conversation_id == conversation.id
    await chat.set_bot_active_conversation(session, man, None)
    assert man.bot_active_conversation_id is None
    stranger = (await make_profile(gender=Gender.FEMALE)).user
    with pytest.raises(ConversationNotFound):
        await chat.set_bot_active_conversation(session, stranger, conversation.id)
    await chat.close_conversation(session, conversation, woman)
    with pytest.raises(ConversationClosed):
        await chat.set_bot_active_conversation(session, man, conversation.id)
