"""Rules taken over from the legacy bot (docs/LEGACY_BOT.md §2): gating by the own profile status,
consent of migrated users, interest history per direction, open-chat limit, partial edits."""

import asyncio
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.clock import current_year, utcnow
from app.db import get_sessionmaker
from app.enums import (
    ConversationCloseReason,
    Education,
    Gender,
    InterestStatus,
    MaritalStatus,
    MessageOrigin,
    ProfileStatus,
    ReportReason,
)
from app.models import Conversation, Interest, Outbox, User
from app.schemas import AgeIn, ProfileUpdate, birth_year_from_age, first_error
from app.services import chat, guardians, interests, moderation, profiles, users
from app.services.errors import (
    AlreadyReported,
    ConsentRequired,
    InterestAlreadyExists,
    InterestDeclinedBefore,
    InterestLimitReached,
    InterestSentBefore,
    OpenConversationLimit,
    ProfileHidden,
    ProfileNotFound,
    ProfilePending,
    ProfileRejected,
    ProfileRequired,
    ReportLimitReached,
)

# --- Gating (§2.5) --------------------------------------------------------------------------------

GATES = [
    (ProfileStatus.PENDING, ProfilePending, "Ваша анкета ещё не одобрена"),
    (ProfileStatus.REJECTED, ProfileRejected, "отклонена модератором"),
    (ProfileStatus.HIDDEN, ProfileHidden, "Ваша анкета скрыта"),
]


@pytest.mark.parametrize(("status", "error", "text"), GATES)
async def test_browsing_and_interests_need_an_active_own_profile(session, make_profile, status, error, text):
    viewer = (await make_profile(gender=Gender.MALE, status=status)).user
    sister = await make_profile(gender=Gender.FEMALE)
    with pytest.raises(error, match=text):
        await profiles.list_catalog(session, viewer)
    with pytest.raises(error):
        await profiles.catalog_position(session, viewer, None, sister.user_id)
    with pytest.raises(error):
        await profiles.get_viewable_profile(session, viewer, sister.user_id)
    with pytest.raises(error):
        await interests.send_interest(session, viewer, sister.user_id)
    assert (await profiles.get_viewable_profile(session, viewer, viewer.id)).user_id == viewer.id  # own


async def test_gate_errors_without_profile_and_without_premoderation(
    session, make_profile, make_user, override_settings
):
    sister = await make_profile(gender=Gender.FEMALE)
    nobody = await make_user()
    for call in (
        profiles.list_catalog(session, nobody),
        profiles.get_viewable_profile(session, nobody, sister.user_id),
        interests.send_interest(session, nobody, sister.user_id),
    ):
        with pytest.raises(ProfileRequired):
            await call
    hidden = (await make_profile(gender=Gender.MALE, status=ProfileStatus.HIDDEN)).user
    override_settings(PREMODERATION=False)
    assert (await profiles.list_catalog(session, hidden))[1] == 1
    assert (await profiles.get_viewable_profile(session, hidden, sister.user_id)).user_id == sister.user_id
    with pytest.raises(ProfileHidden):  # sending always needs a published profile
        await interests.send_interest(session, hidden, sister.user_id)


async def test_counterparts_stay_viewable_while_the_own_profile_is_on_moderation(
    session, make_pair, make_conversation, make_profile
):
    man, woman = await make_pair()
    await make_conversation(man, woman)
    stranger = await make_profile(gender=Gender.FEMALE)
    own = await profiles.get_profile(session, man.id)
    await profiles.update_profile(session, man, ProfileUpdate(about="Новый текст о себе"))
    assert own.status == ProfileStatus.PENDING
    assert (await profiles.get_viewable_profile(session, man, woman.id)).user_id == woman.id
    with pytest.raises(ProfilePending):
        await profiles.get_viewable_profile(session, man, stranger.user_id)


# --- Consent gate (§4) -----------------------------------------------------------------------------


async def _without_consent(session, user: User) -> User:
    user.consent_at = None
    await session.flush()
    assert users.needs_consent(user)
    return user


async def test_migrated_user_without_consent_can_only_read_withdraw_and_consent(
    session, make_pair, make_conversation, make_profile
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    message = await chat.send_message(session, conversation.id, woman, "Салам", MessageOrigin.TELEGRAM)
    other = (await make_profile(gender=Gender.FEMALE)).user
    pending = await interests.send_interest(session, other, man.id)
    own_interest = await interests.send_interest(session, man, (await make_profile(gender=Gender.FEMALE)).user_id)
    stranger = (await make_profile(gender=Gender.FEMALE)).user
    await _without_consent(session, man)

    blocked = [
        interests.send_interest(session, man, other.id),
        interests.accept(session, pending.id, man),
        interests.decline(session, pending.id, man),
        chat.send_message(session, conversation.id, man, "Ответ", MessageOrigin.TELEGRAM),
        profiles.update_profile(session, man, ProfileUpdate(city="Уфа")),
        profiles.list_catalog(session, man),
        profiles.get_viewable_profile(session, man, stranger.id),
        moderation.create_report(session, man, stranger.id, ReportReason.FAKE),
        guardians.create_invite(session, man),
    ]
    for call in blocked:
        with pytest.raises(ConsentRequired, match="согласие на обработку персональных данных"):
            await call
    # Reading own data, withdrawing and reporting a message in the own chat work without consent.
    assert [m.id for m in await chat.list_messages(session, conversation)] == [message.id]
    assert (await profiles.get_viewable_profile(session, man, woman.id)).user_id == woman.id  # counterpart
    assert (await interests.list_incoming(session, man))[0].id == pending.id
    await interests.cancel(session, own_interest.id, man)
    report = await moderation.create_report(session, man, woman.id, ReportReason.INSULT, message_id=message.id)
    assert report.message_id == message.id
    await chat.close_conversation(session, conversation, man)
    await profiles.hide(session, man)
    with pytest.raises(ConsentRequired):
        await profiles.show(session, man)
    await users.set_consent(session, man)
    assert not users.needs_consent(man)
    await profiles.show(session, man)
    await users.delete_account(session, await users.get_by_id(session, woman.id))  # deletion needs no consent


# --- Interests per direction (§2.6) ----------------------------------------------------------------


async def test_decline_and_accept_are_final_for_the_sender_cancel_is_not(session, make_pair, override_settings):
    man, woman = await make_pair()
    first = await interests.send_interest(session, man, woman.id)
    await interests.cancel(session, first.id, man)
    second = await interests.send_interest(session, man, woman.id)  # own cancel: may send again
    await interests.decline(session, second.id, woman)
    with pytest.raises(InterestDeclinedBefore, match="уже отклонил"):
        await interests.send_interest(session, man, woman.id)

    # The reverse direction has its own history: she may write, he may accept, a chat opens.
    reverse = await interests.send_interest(session, woman, man.id)
    conversation = await interests.accept(session, reverse.id, man)
    await chat.close_conversation(session, conversation, woman)
    with pytest.raises(InterestSentBefore, match="Повторный отклик невозможен"):
        await interests.send_interest(session, woman, man.id)  # her interest was accepted once
    with pytest.raises(InterestDeclinedBefore):
        await interests.send_interest(session, man, woman.id)


async def test_a_pair_can_have_a_second_chat_through_the_other_direction(session, make_pair):
    man, woman = await make_pair()
    first = await interests.accept(session, (await interests.send_interest(session, man, woman.id)).id, woman)
    with pytest.raises(InterestAlreadyExists):  # while the chat is open
        await interests.send_interest(session, woman, man.id)
    await chat.close_conversation(session, first, man)
    second = await interests.accept(session, (await interests.send_interest(session, woman, man.id)).id, man)
    assert second.id != first.id and second.is_open
    assert (await interests.get_relation(session, man, woman.id)).conversation.id == second.id
    assert (await chat.get_conversation_between(session, man.id, woman.id)).id == second.id
    await chat.close_conversation(session, second, man)
    assert (await chat.get_conversation_between(session, man.id, woman.id)).id == second.id  # latest closed
    assert await chat.get_open_conversation_between(session, man.id, woman.id) is None


async def test_pending_in_either_direction_blocks_a_new_interest(session, make_pair):
    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestAlreadyExists):
        await interests.send_interest(session, woman, man.id)
    with pytest.raises(InterestAlreadyExists):
        await interests.send_interest(session, man, woman.id)


async def test_legacy_pair_with_both_directions_pending_can_be_accepted_once(session, make_pair):
    man, woman = await make_pair()
    session.add_all(
        [Interest(from_user_id=man.id, to_user_id=woman.id), Interest(from_user_id=woman.id, to_user_id=man.id)]
    )
    await session.flush()
    his, hers = (await session.scalars(select(Interest).order_by(Interest.id))).all()
    await interests.accept(session, his.id, woman)
    with pytest.raises(InterestAlreadyExists, match="открытый чат с этим человеком"):
        await interests.accept(session, hers.id, man)
    assert hers.status == InterestStatus.PENDING


# --- One open chat (§2.7) --------------------------------------------------------------------------


async def test_accept_respects_max_open_conversations_for_both(session, make_profile, override_settings):
    man = (await make_profile(gender=Gender.MALE)).user
    first, second, third = [(await make_profile(gender=Gender.FEMALE)).user for _ in range(3)]
    to_first = await interests.send_interest(session, man, first.id)
    to_second = await interests.send_interest(session, man, second.id)
    from_third = await interests.send_interest(session, (await make_profile(gender=Gender.MALE)).user, third.id)
    await interests.accept(session, to_first.id, first)
    with pytest.raises(OpenConversationLimit) as exc_info:  # the sender has an open chat
        await interests.accept(session, to_second.id, second)
    assert exc_info.value.message == "У одного из участников уже есть открытый чат. Завершите его сначала."
    assert [c.partner_id(man.id) for c in await chat.open_conversations(session, man)] == [first.id]
    override_settings(MAX_OPEN_CONVERSATIONS=2)
    await interests.accept(session, to_second.id, second)
    await interests.accept(session, from_third.id, third)
    assert {c.partner_id(man.id) for c in await chat.open_conversations(session, man)} == {first.id, second.id}
    counts = await chat.count_open_conversations(session, [man.id, third.id, 999999])
    assert counts == {man.id: 2, third.id: 1, 999999: 0}


async def test_concurrent_accepts_cannot_exceed_the_limit(session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    women = [(await make_profile(gender=Gender.FEMALE)).user for _ in range(2)]
    sent = [await interests.send_interest(session, man, woman.id) for woman in women]
    await session.commit()

    async def accept(interest_id: int, user_id: int) -> str:
        async with get_sessionmaker()() as own:
            user = await own.get(User, user_id)
            try:
                await interests.accept(own, interest_id, user)
                await asyncio.sleep(0.05)  # hold the locks a moment: the other accept must wait
                await own.commit()
                return "ok"
            except OpenConversationLimit:
                await own.rollback()
                return "limit"

    results = await asyncio.gather(*(accept(i.id, w.id) for i, w in zip(sent, women, strict=True)))
    assert sorted(results) == ["limit", "ok"]
    assert len((await session.scalars(select(Conversation))).all()) == 1


async def test_open_conversations_helper_for_plain_text_routing(session, make_pair, make_conversation):
    man, woman = await make_pair()
    assert await chat.open_conversations(session, man) == []
    conversation = await make_conversation(man, woman)
    [only] = await chat.open_conversations(session, man)
    assert only.id == conversation.id and only.user_a.profile is not None and only.user_b.profile is not None
    await chat.force_close(session, conversation, ConversationCloseReason.CLOSED_BY_USER, man.id)
    assert await chat.open_conversations(session, woman) == []


# --- Message reports (§2.12) ---------------------------------------------------------------------


async def test_a_participant_can_always_report_a_message_in_the_own_chat(
    session, make_pair, make_conversation, make_profile
):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    message = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.TELEGRAM)
    await moderation.create_report(session, woman, man.id, ReportReason.INSULT)  # the person: once while open
    with pytest.raises(AlreadyReported):
        await moderation.create_report(session, woman, man.id, ReportReason.FRAUD)
    for index in range(moderation.REPORT_DAILY_LIMIT - 1):  # use up the daily limit on other people
        stranger = (await make_profile(gender=Gender.MALE)).user_id
        await moderation.create_report(session, woman, stranger, ReportReason.FAKE, f"#{index}")
    with pytest.raises(ReportLimitReached):
        stranger = (await make_profile(gender=Gender.MALE)).user_id
        await moderation.create_report(session, woman, stranger, ReportReason.FAKE)
    await chat.close_conversation(session, conversation, woman)  # even after the chat was closed

    report = await moderation.create_report(session, woman, man.id, ReportReason.INSULT, message_id=message.id)
    assert report.message_id == message.id and message.flagged
    with pytest.raises(AlreadyReported, match="на это сообщение"):
        await moderation.create_report(session, woman, man.id, ReportReason.INSULT, message_id=message.id)


# --- Profiles: partial updates and re-moderation (§2.4) ---------------------------------------------


async def test_choice_edits_keep_an_active_profile_active(session, make_profile):
    profile = await make_profile(gender=Gender.FEMALE, marital_status=None, education=None)
    approved_at = profile.approved_at
    await profiles.update_profile(
        session,
        profile.user,
        ProfileUpdate(marital_status=MaritalStatus.DIVORCED, education=Education.HIGHER, height=165),
    )
    assert (profile.status, profile.approved_at, profile.marital_status) == (
        ProfileStatus.ACTIVE,
        approved_at,
        MaritalStatus.DIVORCED,
    )
    assert (await session.scalars(select(Outbox.kind))).all() == []  # nothing for the moderators
    await profiles.update_profile(session, profile.user, ProfileUpdate(nationality="татарка"))
    assert profile.status == ProfileStatus.PENDING and profile.approved_at is None
    assert (await session.scalars(select(Outbox.kind))).all() == ["profile.submitted"]


async def test_hidden_and_rejected_profiles_on_edit(session, make_profile):
    hidden = await make_profile(status=ProfileStatus.HIDDEN)
    await profiles.update_profile(session, hidden.user, ProfileUpdate(children=2))
    assert hidden.status == ProfileStatus.HIDDEN and hidden.approved_at is not None
    await profiles.update_profile(session, hidden.user, ProfileUpdate(city="Уфа"))
    assert hidden.status == ProfileStatus.HIDDEN and hidden.approved_at is None
    rejected = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.REJECTED)
    await profiles.update_profile(session, rejected.user, ProfileUpdate(children=1))
    assert rejected.status == ProfileStatus.PENDING  # any edit re-submits a rejected profile


async def test_a_migrated_profile_with_legacy_texts_can_be_completed(session, make_profile):
    profile = await make_profile(
        gender=Gender.FEMALE,
        wishes="Хорошего",  # 8 characters: shorter than today's minimum
        city="Москва/МО",  # characters today's rule rejects
        marital_status=None,
        children=None,
    )
    form = {
        "birth_year": str(profile.birth_year),
        "city": "Москва/МО",
        "wishes": "Хорошего\r\n",
        "marital_status": "never_married",
        "children": "0",
        "education": "higher",
        "employment": "employed",
        "height": "",
        "about": "",
    }
    update = ProfileUpdate.from_form(form, profile)
    assert update.changes() == {"marital_status": MaritalStatus.NEVER_MARRIED, "children": 0}
    await profiles.update_profile(session, profile.user, update)
    assert profiles.is_complete(profile) and profile.status == ProfileStatus.ACTIVE
    assert (profile.wishes, profile.city) == ("Хорошего", "Москва/МО")  # kept as they were
    with pytest.raises(ValidationError) as exc_info:  # a changed text is checked by today's rules
        ProfileUpdate.from_form({**form, "wishes": "Коротко"}, profile)
    assert "wishes" in first_error(exc_info.value) or "Пожелания" in first_error(exc_info.value)
    with pytest.raises(ValidationError):  # required fields cannot be cleared
        ProfileUpdate.from_form({"marital_status": ""}, profile)


async def test_viewable_profile_by_number(session, make_pair):
    man, woman = await make_pair()
    profile = await profiles.get_viewable_profile(session, man, woman.id)
    assert profile.number == 501
    assert (await profiles.get_viewable_profile_by_number(session, man, 501)).user_id == woman.id
    assert (await profiles.get_viewable_profile_by_number(session, man, 500)).user_id == man.id  # own
    for missing in (999, 0):
        with pytest.raises(ProfileNotFound):
            await profiles.get_viewable_profile_by_number(session, man, missing)
    with pytest.raises(ProfileNotFound):
        await profiles.get_viewable_profile(session, man, 999999)
    await profiles.hide(session, man)
    with pytest.raises(ProfileHidden):  # the gate, not "not found", for unknown numbers too
        await profiles.get_viewable_profile_by_number(session, man, 999)


# --- Age (§2.3) ----------------------------------------------------------------------------------


def test_age_to_birth_year_and_range():
    assert birth_year_from_age(25, date(2026, 10, 31)) == 2001
    assert birth_year_from_age(18) == current_year() - 18
    assert AgeIn(age="80").birth_year(date(2026, 1, 1)) == 1946
    for bad in ("17", "81", "сто"):
        with pytest.raises(ValidationError) as exc_info:
            AgeIn(age=bad)
        assert first_error(exc_info.value).startswith("Возраст:")
    assert utcnow().year >= 2026


async def test_check_can_send_refuses_like_send_interest_without_writing(
    session, make_pair, make_profile, override_settings
):
    man, woman = await make_pair()
    await interests.check_can_send(session, man, woman.id)
    await interests.send_interest(session, man, woman.id)
    with pytest.raises(InterestAlreadyExists):
        await interests.check_can_send(session, man, woman.id)
    override_settings(INTEREST_DAILY_LIMIT=1)
    other = (await make_profile(gender=Gender.FEMALE)).user
    with pytest.raises(InterestLimitReached):
        await interests.check_can_send(session, man, other.id)
    assert len((await session.scalars(select(Interest))).all()) == 1


async def test_deleting_an_account_while_an_accept_opens_its_chat_leaves_no_open_chat(session, make_pair):
    """delete_account locks the users row first: the accept commits before it (and its chat is closed
    as «account deleted») or waits and then fails."""
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    async with get_sessionmaker()() as acceptor_side, get_sessionmaker()() as deleting_side:
        accepting = await interests.accept(acceptor_side, interest.id, await acceptor_side.get(User, woman.id))
        assert accepting.is_open
        deleting = asyncio.create_task(users.delete_account(deleting_side, await deleting_side.get(User, man.id)))
        await asyncio.sleep(0.2)  # the deletion waits for the users row the accept holds
        await acceptor_side.commit()
        await deleting
        await deleting_side.commit()

    [conversation] = (await session.scalars(select(Conversation))).all()
    await session.refresh(conversation)
    assert conversation.user_a_id is None or conversation.user_b_id is None
    assert conversation.closed_at is not None and conversation.close_reason == ConversationCloseReason.ACCOUNT_DELETED
    assert (await chat.count_open_conversations(session, [woman.id]))[woman.id] == 0


async def test_the_close_note_tells_each_side_what_closing_really_means(session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    conversation = await interests.accept(session, interest.id, woman)
    assert await chat.close_note(session, conversation, man) == chat.CLOSE_NOTE_SENDER
    assert await chat.close_note(session, conversation, woman) == chat.CLOSE_NOTE_ACCEPTOR
    await chat.close_conversation(session, conversation, woman)
    with pytest.raises(InterestSentBefore):  # what the sender is told holds…
        await interests.send_interest(session, man, woman.id)
    await interests.send_interest(session, woman, man.id)  # …and the acceptor may write first
    legacy = Conversation(user_a_id=min(man.id, woman.id), user_b_id=max(man.id, woman.id))  # imported, no interest
    session.add(legacy)
    await session.flush()
    assert await chat.close_note(session, legacy, man) == chat.CLOSE_NOTE_UNKNOWN
