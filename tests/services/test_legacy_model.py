"""Schema after the legacy integration: profile numbers, nullable fields, witness messages and the
constraints that must admit every state the legacy bot could produce."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clock import utcnow
from app.enums import Gender, InterestStatus, MessageKind, MessageOrigin
from app.models import Conversation, Interest, Message, Profile
from app.services import meta, profiles, users
from app.services.errors import InvalidInput, LegacyImportDone
from app.text import gender_word, profile_title


async def test_profile_numbers_start_at_500_and_explicit_numbers_are_kept(session, make_profile):
    first = await make_profile()
    second = await make_profile(gender=Gender.FEMALE)
    assert (first.number, second.number) == (500, 501)
    assert profile_title(first) == "Анкета №500" and gender_word(second.gender) == "Сестра"
    imported = await make_profile(number=777)
    assert imported.number == 777
    assert (await profiles.get_profile_by_number(session, 777)).user_id == imported.user_id
    assert await profiles.get_profile_by_number(session, 999) is None


async def test_profile_numbers_are_unique(session, make_profile, make_user):
    await make_profile(number=600)
    session.add(
        Profile(user=await make_user(), gender=Gender.MALE, birth_year=1990, city="Уфа", wishes="x", number=600)
    )
    with pytest.raises(IntegrityError, match="uq_profiles_number"):
        await session.flush()
    await session.rollback()


async def test_advance_number_sequence_moves_past_imported_numbers_and_never_back(session, make_profile):
    await make_profile(number=640)
    assert await profiles.advance_number_sequence(session) == 641
    assert await profiles.advance_number_sequence(session, floor=700) == 701  # deleted legacy numbers
    assert await profiles.advance_number_sequence(session, floor=650) == 701  # never moves back
    await session.commit()
    assert (await make_profile()).number == 701


async def test_migrated_profile_may_lack_the_new_required_fields(session, make_profile):
    legacy = await make_profile(marital_status=None, children=None, education=None, employment=None)
    assert profiles.missing_fields(legacy) == ["marital_status", "children", "education", "employment"]
    assert not profiles.is_complete(legacy)
    complete = await make_profile(gender=Gender.FEMALE)
    assert profiles.missing_fields(complete) == [] and profiles.is_complete(complete)


async def test_interests_admit_every_legacy_state(session, make_pair, make_profile):
    man, woman = await make_pair()
    other = (await make_profile(gender=Gender.FEMALE)).user
    session.add_all(
        [
            # Both directions pending at once.
            Interest(from_user_id=man.id, to_user_id=woman.id, status=InterestStatus.PENDING),
            Interest(from_user_id=woman.id, to_user_id=man.id, status=InterestStatus.PENDING),
            # Accepted in both directions over time (two chats), and a declined + a pending one.
            Interest(from_user_id=man.id, to_user_id=other.id, status=InterestStatus.ACCEPTED),
            Interest(from_user_id=other.id, to_user_id=man.id, status=InterestStatus.ACCEPTED),
            Interest(from_user_id=man.id, to_user_id=other.id, status=InterestStatus.DECLINED),
            Interest(from_user_id=man.id, to_user_id=other.id, status=InterestStatus.CANCELLED),
        ]
    )
    await session.flush()
    session.add(Interest(from_user_id=woman.id, to_user_id=man.id, status=InterestStatus.PENDING))
    with pytest.raises(IntegrityError, match="uq_interests_pending_direction"):
        await session.flush()
    await session.rollback()


async def test_a_pair_may_have_several_conversations_but_one_open(session, make_pair):
    man, woman = await make_pair()
    low, high = sorted((man.id, woman.id))
    session.add_all(
        [
            Conversation(user_a_id=low, user_b_id=high, closed_at=utcnow()),
            Conversation(user_a_id=low, user_b_id=high, closed_at=utcnow()),
            Conversation(user_a_id=low, user_b_id=high),
        ]
    )
    await session.flush()
    session.add(Conversation(user_a_id=low, user_b_id=high))
    with pytest.raises(IntegrityError, match="uq_conversations_open_pair"):
        await session.flush()
    await session.rollback()


async def test_witness_messages_have_no_sender_and_user_messages_have_one(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)

    def message(kind: MessageKind, sender_id: int | None) -> Message:
        return Message(
            conversation_id=conversation.id, kind=kind, sender_id=sender_id, text="т", origin=MessageOrigin.TELEGRAM
        )

    session.add_all([message(MessageKind.WITNESS, None), message(MessageKind.USER, man.id)])
    await session.flush()
    for bad in (message(MessageKind.WITNESS, man.id), message(MessageKind.USER, None)):
        async with session.begin_nested():
            session.add(bad)
            with pytest.raises(IntegrityError, match="ck_messages_kind_sender"):
                await session.flush()
    assert {m.kind for m in await session.scalars(select(Message))} == {MessageKind.USER, MessageKind.WITNESS}


async def test_legacy_import_marker_is_recorded_once(session):
    assert await meta.legacy_import_marker(session) is None
    await meta.mark_legacy_import(session, {"profiles": 46, "messages": 671})
    marker = await meta.legacy_import_marker(session)
    assert marker["counts"] == {"profiles": 46, "messages": 671} and marker["imported_at"]
    with pytest.raises(LegacyImportDone):
        await meta.mark_legacy_import(session, {"profiles": 1})
    await meta.set_value(session, "other", {"a": 1})
    await meta.set_value(session, "other", {"a": 2})
    assert await meta.get_value(session, "other") == {"a": 2}


async def test_admin_command_user_reference(session, make_profile, make_user):
    profile = await make_profile(number=512)
    no_profile = await make_user(telegram_id=424242)
    for ref in ("№512", "#512", " № 512 "):
        assert (await users.resolve_user_ref(session, ref)).id == profile.user_id
    assert (await users.resolve_user_ref(session, str(profile.user.telegram_id))).id == profile.user_id
    assert (await users.resolve_user_ref(session, "424242")).id == no_profile.id
    assert await users.resolve_user_ref(session, "№999") is None
    assert await users.resolve_user_ref(session, "1") is None
    for bad in ("@ahmad", "512a", "", "№"):
        with pytest.raises(InvalidInput):
            await users.resolve_user_ref(session, bad)
