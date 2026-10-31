import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.enums import Gender, InterestStatus, ProfileStatus, ReportReason
from app.models import Interest, Outbox
from app.services import interests, moderation
from app.services.errors import (
    InterestAlreadyExists,
    InterestDeclinedBefore,
    InterestLimitReached,
    InterestNotFound,
    InterestNotPending,
    InterestToSelf,
    InvalidInput,
    ProfileNotActive,
    ProfileNotFound,
    UserBanned,
)


async def test_send_interest_happy_path(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id, "  Ассаляму алейкум!  ")
    assert interest.status == InterestStatus.PENDING
    assert interest.message == "Ассаляму алейкум!" and interest.flagged is False
    event = await session.scalar(select(Outbox))
    assert event.kind == "interest.created"
    assert event.payload == {
        "interest_id": interest.id,
        "from_user_id": man.id,
        "to_user_id": woman.id,
        "user_ids": sorted([man.id, woman.id]),
    }
    assert await interests.pending_incoming_count(session, woman) == 1
    assert [i.id for i in await interests.list_incoming(session, woman)] == [interest.id]
    assert [i.id for i in await interests.list_outgoing(session, man)] == [interest.id]
    incoming = (await interests.list_incoming(session, woman))[0]
    assert incoming.from_user.profile.name == "Ахмад"


async def test_sender_rules(session, make_pair, make_profile, make_user):
    man, woman = await make_pair()
    with pytest.raises(InterestToSelf):
        await interests.send_interest(session, man, man.id)
    pending = await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    with pytest.raises(ProfileNotActive):
        await interests.send_interest(session, pending.user, woman.id)
    with pytest.raises(ProfileNotActive):
        await interests.send_interest(session, await make_user(), woman.id)
    banned = await make_profile(await make_user(is_banned=True), gender=Gender.MALE)
    with pytest.raises(UserBanned):
        await interests.send_interest(session, banned.user, woman.id)
    with pytest.raises(InvalidInput):
        await interests.send_interest(session, man, woman.id, "а" * 501)
    with pytest.raises(InvalidInput):
        await interests.send_interest(session, man, woman.id, "пиши мне @ahmad_k")


async def test_recipient_rules(session, make_pair, make_profile, make_user):
    man, _woman = await make_pair()
    other_man = await make_profile(gender=Gender.MALE)
    hidden = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.HIDDEN)
    banned = await make_profile(await make_user(is_banned=True), gender=Gender.FEMALE)
    no_profile = await make_user()
    for target in (other_man.user_id, hidden.user_id, banned.user_id, no_profile.id, 999999):
        with pytest.raises(ProfileNotFound):
            await interests.send_interest(session, man, target)
    assert await session.scalar(select(Interest)) is None


async def test_report_between_users_blocks_interest(session, make_pair):
    man, woman = await make_pair()
    await moderation.create_report(session, woman, man.id, ReportReason.INSULT)
    with pytest.raises(ProfileNotFound):
        await interests.send_interest(session, man, woman.id)
    with pytest.raises(ProfileNotFound):
        await interests.send_interest(session, woman, man.id)


async def test_pair_uniqueness_in_both_directions(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestAlreadyExists):
        await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestAlreadyExists):
        await interests.send_interest(session, woman, man.id)
    await interests.accept(session, interest.id, woman)
    with pytest.raises(InterestAlreadyExists):
        await interests.send_interest(session, woman, man.id)


async def test_unique_index_enforces_unordered_pair(session, make_pair):
    man, woman = await make_pair()
    session.add(Interest(from_user_id=man.id, to_user_id=woman.id))
    await session.flush()
    session.add(Interest(from_user_id=woman.id, to_user_id=man.id))
    with pytest.raises(Exception, match="uq_interests_active_pair"):
        await session.flush()
    await session.rollback()


async def test_concurrent_reverse_interests_only_one_wins(session, make_pair):
    man, woman = await make_pair()
    await session.commit()

    async def send(sender_id: int, recipient_id: int) -> str:
        async with get_sessionmaker()() as own:
            sender = await own.get(type(man), sender_id)
            try:
                await interests.send_interest(own, sender, recipient_id)
                await own.commit()
                return "ok"
            except InterestAlreadyExists:
                await own.rollback()
                return "exists"

    results = await asyncio.gather(send(man.id, woman.id), send(woman.id, man.id))
    assert sorted(results) == ["exists", "ok"]
    assert len(list(await session.scalars(select(Interest)))) == 1


async def test_cancel_then_resend_allowed_but_decline_is_final_for_sender(session, make_pair):
    man, woman = await make_pair()
    first = await interests.send_interest(session, man, woman.id)
    await interests.cancel(session, first.id, man)
    assert first.status == InterestStatus.CANCELLED
    second = await interests.send_interest(session, man, woman.id)
    await interests.decline(session, second.id, woman)
    with pytest.raises(InterestDeclinedBefore):
        await interests.send_interest(session, man, woman.id)
    # The person who declined may still reach out later.
    reverse = await interests.send_interest(session, woman, man.id)
    assert reverse.status == InterestStatus.PENDING


async def test_daily_limit_is_rolling_and_counts_cancelled(session, make_profile, override_settings):
    override_settings(INTEREST_DAILY_LIMIT=3)
    man = (await make_profile(gender=Gender.MALE)).user
    women = [(await make_profile(gender=Gender.FEMALE)).user for _ in range(5)]
    for woman in women[:3]:
        interest = await interests.send_interest(session, man, woman.id)
        await interests.cancel(session, interest.id, man)
    with pytest.raises(InterestLimitReached):
        await interests.send_interest(session, man, women[3].id)
    # Interests older than 24 hours no longer count.
    await session.execute(update(Interest).values(created_at=Interest.created_at - timedelta(hours=24, minutes=1)))
    await interests.send_interest(session, man, women[3].id)


async def test_accept_creates_conversation(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestNotFound):
        await interests.accept(session, interest.id, man)  # only the recipient
    conversation = await interests.accept(session, interest.id, woman)
    assert interest.status == InterestStatus.ACCEPTED and interest.decided_at is not None
    assert conversation.created_from_interest_id == interest.id
    assert (conversation.user_a_id, conversation.user_b_id) == tuple(sorted([man.id, woman.id]))
    with pytest.raises(InterestNotPending):
        await interests.accept(session, interest.id, woman)
    event = await session.scalar(select(Outbox).where(Outbox.kind == "interest.accepted"))
    assert event.payload == {
        "interest_id": interest.id,
        "conversation_id": conversation.id,
        "from_user_id": man.id,
        "to_user_id": woman.id,
        "user_ids": sorted([man.id, woman.id]),
    }
    relation = await interests.get_relation(session, man, woman.id)
    assert relation.interest.id == interest.id and relation.conversation.id == conversation.id


async def test_decline_and_cancel_permissions(session, make_pair, make_profile):
    man, woman = await make_pair()
    stranger = (await make_profile(gender=Gender.FEMALE)).user
    interest = await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestNotFound):
        await interests.decline(session, interest.id, man)
    with pytest.raises(InterestNotFound):
        await interests.decline(session, interest.id, stranger)
    with pytest.raises(InterestNotFound):
        await interests.cancel(session, interest.id, woman)
    with pytest.raises(InterestNotFound):
        await interests.get_interest(session, interest.id, stranger)
    assert (await interests.get_interest(session, interest.id, woman)).to_user.profile is not None

    await interests.decline(session, interest.id, woman)
    with pytest.raises(InterestNotPending):
        await interests.cancel(session, interest.id, man)
    kinds = list(await session.scalars(select(Outbox.kind).order_by(Outbox.id)))
    assert kinds == ["interest.created", "interest.declined"]


async def test_banned_recipient_cannot_accept_and_banned_senders_are_hidden(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    man.is_banned = True
    await session.flush()
    assert await interests.list_incoming(session, woman) == []
    assert await interests.pending_incoming_count(session, woman) == 0
    with pytest.raises(ProfileNotFound):
        await interests.accept(session, interest.id, woman)
    woman.is_banned = True
    with pytest.raises(UserBanned):
        await interests.decline(session, interest.id, woman)


async def test_stop_words_flag_interest_message(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id, "Есть выгодные инвестиции")
    assert interest.flagged is True and "инвестиции" in interest.flag_reason


async def test_list_incoming_history_statuses(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await interests.decline(session, interest.id, woman)
    assert await interests.list_incoming(session, woman) == []
    history = await interests.list_incoming(session, woman, statuses=list(InterestStatus))
    assert [i.id for i in history] == [interest.id]
