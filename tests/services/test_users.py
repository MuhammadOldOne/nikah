from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.clock import utcnow
from app.enums import ConversationCloseReason, Gender, InterestStatus, MessageOrigin, ReportReason
from app.models import Conversation, Guardian, Interest, LoginToken, Message, Outbox, Profile, Report, User
from app.services import auth, chat, guardians, interests, moderation, profiles, users
from app.services.errors import AdminBanDenied


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_get_or_create_by_telegram_creates_once_and_refreshes_names(session):
    user = await users.get_or_create_by_telegram(session, 555, "fatima", "Фатима")
    assert user.id is not None and user.profile is None
    assert user.notify_telegram is True and user.consent_at is None
    again = await users.get_or_create_by_telegram(session, 555, "fatima_new", None)
    assert again.id == user.id
    assert (again.username, again.first_name) == ("fatima_new", None)
    assert await count(session, User) == 1
    assert (await users.get_by_telegram_id(session, 555)).id == user.id
    assert (await users.get_by_id(session, user.id)).telegram_id == 555
    assert await users.get_by_id(session, 999) is None


async def test_consent_notify_and_last_seen(session, make_user):
    user = await make_user(consent=False)
    await users.set_consent(session, user)
    first = user.consent_at
    assert first is not None
    await users.set_consent(session, user)
    assert user.consent_at == first

    await users.set_notify(session, user, False)
    assert user.notify_telegram is False

    await users.touch_last_seen(session, user)
    seen = user.last_seen_at
    assert seen is not None
    await users.touch_last_seen(session, user)
    assert user.last_seen_at == seen  # throttled
    user.last_seen_at = utcnow() - timedelta(minutes=5)
    await users.touch_last_seen(session, user)
    assert user.last_seen_at > seen


async def test_is_admin_by_flag_or_settings(session, make_user, override_settings):
    regular = await make_user()
    flagged = await make_user(is_admin=True)
    configured = await make_user(telegram_id=42)
    override_settings(ADMIN_TELEGRAM_IDS=[42, 43])
    assert not users.is_admin(regular)
    assert users.is_admin(flagged)
    assert users.is_admin(configured)
    assert await users.admin_telegram_ids(session) == sorted({42, 43, flagged.telegram_id})


async def test_ban_hides_profile_cancels_interests_closes_chats(
    session, make_pair, make_profile, make_conversation, make_user
):
    man, woman = await make_pair()
    other_woman = (await make_profile(gender=Gender.FEMALE)).user
    conversation = await make_conversation(man, woman)
    pending = await interests.send_interest(session, man, other_woman.id)
    wali = await make_user()
    link = await guardians.create_invite(session, man)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], wali)
    await session.commit()

    await users.ban(session, man)
    await session.commit()

    assert man.is_banned and man.banned_at is not None
    assert pending.status == InterestStatus.CANCELLED
    assert conversation.closed_at is not None
    assert conversation.close_reason == ConversationCloseReason.USER_BANNED
    assert not await guardians.has_active_guardian(session, man.id)
    closed_events = list(await session.scalars(select(Outbox).where(Outbox.kind == "conversation.closed")))
    assert len(closed_events) == 1 and closed_events[0].payload["reason"] == "user_banned"

    # The banned man is no longer in the catalog of women.
    items, _total = await profiles.list_catalog(session, woman)
    assert man.id not in {profile.user_id for profile in items}

    await users.unban(session, man)
    assert not man.is_banned and man.banned_at is None
    assert conversation.closed_at is not None  # stays closed


async def test_admins_cannot_be_banned(session, make_user, override_settings):
    flagged = await make_user(is_admin=True)
    listed = await make_user()
    override_settings(ADMIN_TELEGRAM_IDS=[listed.telegram_id])
    for admin in (flagged, listed):
        with pytest.raises(AdminBanDenied):
            await users.ban(session, admin)
        assert not admin.is_banned and admin.banned_at is None


async def test_ban_is_idempotent(session, make_user):
    user = await make_user()
    await users.ban(session, user)
    banned_at = user.banned_at
    await users.ban(session, user)
    assert user.banned_at == banned_at


async def test_delete_account_wipes_data_and_closes_partner_chats(
    session, make_pair, make_conversation, make_user, make_profile
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, man, "Ассаляму алейкум", MessageOrigin.WEB)
    reply = await chat.send_message(session, conversation.id, woman, "Ва алейкум ассалям", MessageOrigin.TELEGRAM)
    await moderation.create_report(session, woman, man.id, ReportReason.OTHER)
    await moderation.create_report(session, man, woman.id, ReportReason.OTHER)
    wali = await make_user()
    link = await guardians.create_invite(session, man)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], wali)
    await auth.create_magic_link(session, man)
    man.bot_active_conversation_id = conversation.id
    woman.bot_active_conversation_id = conversation.id
    await session.commit()
    man_id, woman_id, conversation_id = man.id, woman.id, conversation.id

    await users.delete_account(session, man)
    await session.commit()

    assert await session.get(User, man_id) is None
    assert await session.get(Profile, man_id) is None
    assert await count(session, Interest) == 0
    assert await count(session, Report) == 0
    assert await count(session, Guardian) == 0
    assert await count(session, LoginToken) == 0
    remaining = list(await session.scalars(select(Message)))
    assert [message.id for message in remaining] == [reply.id]  # only the partner's message

    kept = await session.get(Conversation, conversation_id)
    assert kept is not None and kept.closed_at is not None
    assert kept.close_reason == ConversationCloseReason.ACCOUNT_DELETED
    assert kept.partner_id(woman_id) is None
    partner = await session.get(User, woman_id)
    assert partner.bot_active_conversation_id is None

    summaries = await chat.list_conversations(session, partner)
    assert len(summaries) == 1 and summaries[0].partner is None
    event = await session.scalar(select(Outbox).where(Outbox.kind == "conversation.closed"))
    assert event.payload == {
        "conversation_id": conversation_id,
        "closed_by_id": None,
        "reason": "account_deleted",
        "user_ids": sorted([man_id, woman_id]),
    }

    # When the partner deletes too, the orphaned conversation disappears.
    await users.delete_account(session, partner)
    await session.commit()
    assert await count(session, Conversation) == 0
    assert await count(session, Message) == 0


async def test_deleted_user_can_register_again_as_new_account(session, make_user):
    user = await make_user(telegram_id=777)
    old_id = user.id
    await users.delete_account(session, user)
    await session.commit()
    fresh = await users.get_or_create_by_telegram(session, 777, None, "Новый")
    assert fresh.id != old_id and fresh.consent_at is None
