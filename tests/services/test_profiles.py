import asyncio

import pytest
from sqlalchemy import select

from app.clock import current_year, utcnow
from app.enums import Education, Gender, InterestStatus, MaritalStatus, ProfileStatus, ReportReason
from app.models import Outbox
from app.schemas import CatalogFilters, ProfileCreate, ProfileUpdate
from app.services import interests, moderation, profiles
from app.services.errors import (
    ConsentRequired,
    InvalidInput,
    ProfileAlreadyExists,
    ProfileNotFound,
    ProfilePending,
    ProfileRequired,
    ProfileStateError,
    UserBanned,
)


def profile_data(gender: Gender = Gender.FEMALE, **overrides: object) -> ProfileCreate:
    data: dict[str, object] = {
        "gender": gender,
        "birth_year": current_year() - 25,
        "city": "Казань",
        "marital_status": "never_married",
        "children": 0,
        "education": "higher",
        "employment": "employed",
        "wishes": "Ищу верующего и ответственного мужа.",
    }
    data.update(overrides)
    return ProfileCreate.model_validate(data)


async def outbox_kinds(session) -> list[str]:
    return list(await session.scalars(select(Outbox.kind).order_by(Outbox.id)))


async def test_create_requires_consent_and_is_pending_with_premoderation(session, make_user):
    user = await make_user(consent=False)
    with pytest.raises(ConsentRequired):
        await profiles.create_profile(session, user, profile_data())
    user.consent_at = utcnow()
    profile = await profiles.create_profile(session, user, profile_data())
    assert profile.status == ProfileStatus.PENDING
    assert profile.submitted_at is not None and profile.approved_at is None
    assert user.profile is profile
    assert await outbox_kinds(session) == ["profile.submitted"]
    with pytest.raises(ProfileAlreadyExists):
        await profiles.create_profile(session, user, profile_data())


async def test_create_without_premoderation_is_active(session, make_user, override_settings):
    override_settings(PREMODERATION=False)
    profile = await profiles.create_profile(session, await make_user(), profile_data())
    assert profile.status == ProfileStatus.ACTIVE and profile.approved_at is not None
    assert await outbox_kinds(session) == []


async def test_banned_user_cannot_create_or_edit(session, make_user, make_profile):
    banned = await make_user(is_banned=True)
    with pytest.raises(UserBanned):
        await profiles.create_profile(session, banned, profile_data())
    profile = await make_profile()
    profile.user.is_banned = True
    with pytest.raises(UserBanned):
        await profiles.update_profile(session, profile.user, ProfileUpdate(city="Уфа"))


async def test_stop_words_flag_profile_but_do_not_block(session, make_user):
    profile = await profiles.create_profile(
        session, await make_user(), profile_data(about="Занимаюсь инвестициями и криптовалютой")
    )
    assert profile.flagged is True
    assert "инвестициями" in profile.flag_reason and "криптовалютой" in profile.flag_reason
    clean = await profiles.update_profile(session, profile.user, ProfileUpdate(about="Люблю читать"))
    assert clean.flagged is False and clean.flag_reason is None


async def test_editing_active_profile_returns_it_to_moderation(session, make_profile):
    profile = await make_profile(gender=Gender.FEMALE)
    updated = await profiles.update_profile(session, profile.user, ProfileUpdate(city="Уфа"))
    assert updated.status == ProfileStatus.PENDING and updated.city == "Уфа"
    assert updated.approved_at is None
    assert await outbox_kinds(session) == ["profile.submitted"]
    # No-op edits change nothing.
    await profiles.update_profile(session, profile.user, ProfileUpdate(city="Уфа"))
    assert await outbox_kinds(session) == ["profile.submitted"]


async def test_editing_without_premoderation_keeps_profile_active(session, make_profile, override_settings):
    override_settings(PREMODERATION=False)
    profile = await make_profile()
    updated = await profiles.update_profile(session, profile.user, ProfileUpdate(about="Новый текст"))
    assert updated.status == ProfileStatus.ACTIVE


async def test_editing_rejected_profile_resubmits_it(session, make_profile):
    profile = await make_profile(status=ProfileStatus.REJECTED, moderation_note="Уберите контакты")
    updated = await profiles.update_profile(session, profile.user, ProfileUpdate(about="Исправила"))
    assert updated.status == ProfileStatus.PENDING and updated.moderation_note is None


async def test_editing_hidden_profile_requires_moderation_on_show(session, make_profile):
    profile = await make_profile(status=ProfileStatus.HIDDEN)
    await profiles.update_profile(session, profile.user, ProfileUpdate(about="Новый текст"))
    assert profile.status == ProfileStatus.HIDDEN and profile.approved_at is None
    shown = await profiles.show(session, profile.user)
    assert shown.status == ProfileStatus.PENDING
    assert await outbox_kinds(session) == ["profile.submitted"]


async def test_hide_and_show(session, make_profile):
    profile = await make_profile()
    await profiles.hide(session, profile.user)
    assert profile.status == ProfileStatus.HIDDEN
    with pytest.raises(ProfileStateError):
        await profiles.hide(session, profile.user)
    await profiles.show(session, profile.user)
    assert profile.status == ProfileStatus.ACTIVE  # approved content goes straight back
    with pytest.raises(ProfileStateError):
        await profiles.show(session, profile.user)
    rejected = await make_profile(status=ProfileStatus.REJECTED)
    with pytest.raises(ProfileStateError):
        await profiles.hide(session, rejected.user)


async def test_profile_actions_require_a_profile(session, make_user):
    user = await make_user()
    for action in (profiles.hide, profiles.show):
        with pytest.raises(ProfileRequired):
            await action(session, user)
    with pytest.raises(ProfileRequired):
        await profiles.update_profile(session, user, ProfileUpdate(city="Уфа"))


async def test_approve_and_reject(session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    await profiles.approve(session, profile.user_id)
    assert profile.status == ProfileStatus.ACTIVE and profile.approved_at is not None
    with pytest.raises(ProfileStateError):
        await profiles.approve(session, profile.user_id)

    with pytest.raises(InvalidInput):
        await profiles.reject(session, profile.user_id, "   ")
    await profiles.reject(session, profile.user_id, "Уберите номер телефона")
    assert profile.status == ProfileStatus.REJECTED
    assert profile.moderation_note == "Уберите номер телефона"
    with pytest.raises(ProfileStateError):
        await profiles.reject(session, profile.user_id, "ещё раз")
    with pytest.raises(ProfileNotFound):
        await profiles.approve(session, 9999)

    events = list(await session.scalars(select(Outbox).where(Outbox.kind == "profile.moderated").order_by(Outbox.id)))
    assert [event.payload["status"] for event in events] == ["active", "rejected"]
    assert events[0].payload["user_ids"] == [profile.user_id]


async def test_list_pending(session, make_profile):
    first = await make_profile(status=ProfileStatus.PENDING)
    await make_profile(status=ProfileStatus.ACTIVE)
    second = await make_profile(status=ProfileStatus.PENDING)
    assert [p.user_id for p in await profiles.list_pending(session)] == [first.user_id, second.user_id]


async def test_catalog_shows_only_active_opposite_gender_not_banned(session, make_profile, make_user):
    viewer = (await make_profile(gender=Gender.MALE)).user
    visible = await make_profile(gender=Gender.FEMALE)
    await make_profile(gender=Gender.MALE)  # same gender
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.HIDDEN)
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.REJECTED)
    await make_profile(await make_user(is_banned=True), gender=Gender.FEMALE)

    items, total = await profiles.list_catalog(session, viewer)
    assert total == 1 and [p.user_id for p in items] == [visible.user_id]
    assert items[0].user.id == visible.user_id  # user is loaded


async def test_catalog_excludes_report_relations_in_both_directions(session, make_profile):
    viewer = (await make_profile(gender=Gender.MALE)).user
    reported = await make_profile(gender=Gender.FEMALE)
    reporter = await make_profile(gender=Gender.FEMALE)
    neutral = await make_profile(gender=Gender.FEMALE)
    await moderation.create_report(session, viewer, reported.user_id, ReportReason.FAKE)
    await moderation.create_report(session, reporter.user, viewer.id, ReportReason.INSULT)

    items, total = await profiles.list_catalog(session, viewer)
    assert total == 1 and [p.user_id for p in items] == [neutral.user_id]


async def test_only_active_profiles_browse_with_premoderation(session, make_profile, make_user, override_settings):
    pending = await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    await make_profile(gender=Gender.FEMALE)
    with pytest.raises(ProfilePending):
        await profiles.list_catalog(session, pending.user)
    override_settings(PREMODERATION=False)
    assert (await profiles.list_catalog(session, pending.user))[1] == 1
    with pytest.raises(ProfileRequired):
        await profiles.list_catalog(session, await make_user())
    pending.user.is_banned = True
    with pytest.raises(UserBanned):
        await profiles.list_catalog(session, pending.user)


async def test_catalog_filters_and_pagination(session, make_profile):
    viewer = (await make_profile(gender=Gender.FEMALE)).user
    year = current_year()
    young = await make_profile(gender=Gender.MALE, birth_year=year - 22, city="Казань")
    middle = await make_profile(
        gender=Gender.MALE,
        birth_year=year - 30,
        city="Орёл",
        marital_status=MaritalStatus.DIVORCED,
        children=2,
        education=Education.RELIGIOUS,
    )
    older = await make_profile(gender=Gender.MALE, birth_year=year - 45, city="Уфа")

    async def ids(**filters: object) -> list[int]:
        items, _ = await profiles.list_catalog(session, viewer, CatalogFilters.model_validate(filters))
        return sorted(p.user_id for p in items)

    assert await ids() == sorted([young.user_id, middle.user_id, older.user_id])
    assert await ids(age_min=25, age_max=40) == [middle.user_id]
    assert await ids(age_max=22) == [young.user_id]
    assert await ids(city="  ОРЕЛ ") == [middle.user_id]  # case- and ё-insensitive
    assert await ids(marital_status="divorced") == [middle.user_id]
    assert await ids(has_children=True) == [middle.user_id]
    assert await ids(has_children=False) == sorted([young.user_id, older.user_id])
    assert await ids(education="religious") == [middle.user_id]

    page1, total = await profiles.list_catalog(session, viewer, page=1, per_page=2)
    page2, _ = await profiles.list_catalog(session, viewer, page=2, per_page=2)
    assert total == 3 and len(page1) == 2 and len(page2) == 1
    assert {p.user_id for p in page1} | {p.user_id for p in page2} == {young.user_id, middle.user_id, older.user_id}
    # Newest approved first.
    assert page1[0].user_id == older.user_id


async def test_get_viewable_profile(session, make_profile, make_pair):
    man, woman = await make_pair()
    other_man = await make_profile(gender=Gender.MALE)
    hidden_woman = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.HIDDEN)

    assert (await profiles.get_viewable_profile(session, man, woman.id)).user_id == woman.id
    assert (await profiles.get_viewable_profile(session, man, man.id)).user_id == man.id
    with pytest.raises(ProfileNotFound):
        await profiles.get_viewable_profile(session, man, other_man.user_id)  # same gender
    with pytest.raises(ProfileNotFound):
        await profiles.get_viewable_profile(session, man, hidden_woman.user_id)
    with pytest.raises(ProfileNotFound):
        await profiles.get_viewable_profile(session, man, 99999)

    # A pending interest keeps the profile visible to the counterpart even when it is hidden.
    interest = await interests.send_interest(session, woman, man.id)
    await profiles.hide(session, woman)
    assert (await profiles.get_viewable_profile(session, man, woman.id)).user_id == woman.id
    await interests.decline(session, interest.id, man)
    assert interest.status == InterestStatus.DECLINED
    with pytest.raises(ProfileNotFound):
        await profiles.get_viewable_profile(session, man, woman.id)


async def test_public_counts(session, make_profile, make_user):
    await make_profile(gender=Gender.MALE)
    await make_profile(gender=Gender.FEMALE)
    await make_profile(gender=Gender.FEMALE)
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    await make_profile(await make_user(is_banned=True), gender=Gender.MALE)
    assert await profiles.public_counts(session) == {Gender.MALE: 1, Gender.FEMALE: 2}


async def test_concurrent_creation_reports_existing_profile(session, make_user):
    """Two submits racing past the existence check: the loser gets ProfileAlreadyExists, not a
    database error (which would end up as a 500 page)."""
    from app.db import get_sessionmaker
    from app.services import users

    user = await make_user()
    async with get_sessionmaker()() as first, get_sessionmaker()() as second:
        await profiles.create_profile(first, await users.get_by_id(first, user.id), profile_data())
        racing = asyncio.create_task(
            profiles.create_profile(second, await users.get_by_id(second, user.id), profile_data())
        )
        await asyncio.sleep(0.3)  # the second INSERT now waits for the first transaction
        await first.commit()
        with pytest.raises(ProfileAlreadyExists):
            await racing
        await second.rollback()
    assert await outbox_kinds(session) == ["profile.submitted"]


async def test_counterparts_never_see_unapproved_content(session, make_pair):
    """An interest or a chat lets the counterpart open the profile, but not an edit waiting for
    moderation and not a profile a moderator took down."""
    from app.enums import ReportAction
    from app.schemas import ProfileUpdate
    from app.services.errors import ProfileUnderReview

    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id, "Салам")
    await profiles.update_profile(session, man, ProfileUpdate(about="Непроверенный текст о себе"))

    with pytest.raises(ProfileUnderReview):
        await profiles.get_viewable_profile(session, woman, man.id)
    assert await interests.list_incoming(session, woman) == []
    assert await interests.pending_incoming_count(session, woman) == 0
    assert (await profiles.get_viewable_profile(session, man, man.id)).about == "Непроверенный текст о себе"

    await profiles.approve(session, man.id)
    assert (await profiles.get_viewable_profile(session, woman, man.id)).about == "Непроверенный текст о себе"
    assert len(await interests.list_incoming(session, woman)) == 1

    reporter = (await profiles.get_profile(session, woman.id)).user
    report = await moderation.create_report(session, reporter, man.id, ReportReason.FAKE)
    admin = await _admin(session)
    await moderation.resolve_report(session, report.id, admin, ReportAction.HIDE_PROFILE)
    with pytest.raises(ProfileUnderReview):
        await profiles.get_viewable_profile(session, woman, man.id)
    assert await interests.list_incoming(session, woman) == []


async def _admin(session):
    from app.models import User

    admin = User(telegram_id=777000, first_name="Админ", is_admin=True)
    session.add(admin)
    await session.flush()
    return admin


async def test_resubmissions_are_limited_per_hour(session, make_profile, override_settings):
    from app.schemas import ProfileUpdate
    from app.services.errors import ProfileSubmissionLimitReached

    user = (await make_profile()).user
    for number in range(profiles.SUBMISSIONS_PER_HOUR):
        await profiles.update_profile(session, user, ProfileUpdate(about=f"Правка {number}"))
    with pytest.raises(ProfileSubmissionLimitReached):
        await profiles.update_profile(session, user, ProfileUpdate(about="Ещё одна правка"))
    assert (await profiles.get_profile(session, user.id)).about == f"Правка {profiles.SUBMISSIONS_PER_HOUR - 1}"
    assert await outbox_kinds(session) == ["profile.submitted"] * profiles.SUBMISSIONS_PER_HOUR

    override_settings(PREMODERATION=False)  # nothing reaches the admins: no limit
    await profiles.update_profile(session, user, ProfileUpdate(about="Без премодерации"))
