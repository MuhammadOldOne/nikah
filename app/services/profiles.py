"""Profiles (анкеты): creation, editing, moderation, catalog.

Status rules (PREMODERATION on):
- create -> pending (``profile.submitted``); with PREMODERATION off -> active at once;
- edit of an active or rejected profile -> pending again; edit of a pending profile re-submits it;
  edit of a hidden profile keeps it hidden but clears approval, so ``show`` sends it to moderation;
- approve: pending -> active; reject: pending/active/hidden -> rejected (``profile.moderated``);
- hide: active/pending -> hidden; show: hidden -> active (or pending if not approved).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import current_year, utcnow
from app.config import get_settings
from app.enums import Gender, InterestStatus, ProfileStatus
from app.models import Conversation, Interest, Profile, User
from app.schemas import CatalogFilters, ModerationNoteIn, ProfileCreate, ProfileUpdate, first_error
from app.services import events
from app.services.errors import (
    ConsentRequired,
    InvalidInput,
    ProfileAlreadyExists,
    ProfileNotFound,
    ProfileRequired,
    ProfileStateError,
    ProfileSubmissionLimitReached,
    ProfileUnderReview,
    UserBanned,
)
from app.services.relations import report_between
from app.services.stopwords import flag_reason
from app.text import normalize_city_key

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 50
# Every submission sends a moderation card to every admin in Telegram.
SUBMISSIONS_PER_HOUR = 5
SUBMISSION_WINDOW = timedelta(hours=1)
_FLAGGED_FIELDS = ("name", "city", "occupation", "nationality", "wishes", "about")


def _city_key(column: Any) -> ColumnElement[str]:
    """SQL twin of app.text.normalize_city_key; matches the ix_profiles_city_key index."""
    return func.replace(func.lower(column), "ё", "е")


def _apply_flags(profile: Profile) -> None:
    reason = flag_reason(*(getattr(profile, name) for name in _FLAGGED_FIELDS))
    profile.flagged = reason is not None
    profile.flag_reason = reason


async def _submit(session: AsyncSession, profile: Profile) -> None:
    """Send the profile to moderation, or publish it right away when PREMODERATION is off."""
    now = utcnow()
    profile.submitted_at = now
    profile.moderation_note = None
    if get_settings().PREMODERATION:
        profile.status = ProfileStatus.PENDING
        profile.approved_at = None
        await session.flush()
        await events.emit(session, events.EventKind.PROFILE_SUBMITTED, {"user_id": profile.user_id})
    else:
        profile.status = ProfileStatus.ACTIVE
        profile.approved_at = now
        await session.flush()


async def _check_submission_rate(session: AsyncSession, user_id: int) -> None:
    """At most SUBMISSIONS_PER_HOUR re-submissions (premoderation only: they reach the admins)."""
    if not get_settings().PREMODERATION:
        return
    recent = await events.count_recent(session, events.EventKind.PROFILE_SUBMITTED, SUBMISSION_WINDOW, user_id=user_id)
    if recent >= SUBMISSIONS_PER_HOUR:
        raise ProfileSubmissionLimitReached()


async def get_profile(session: AsyncSession, user_id: int) -> Profile | None:
    """Profile by its owner's user id (= profile id), with ``user`` loaded; no visibility checks."""
    return await session.scalar(select(Profile).where(Profile.user_id == user_id).options(selectinload(Profile.user)))


async def get_profile_by_user(session: AsyncSession, user: User) -> Profile | None:
    return await get_profile(session, user.id)


async def create_profile(session: AsyncSession, user: User, data: ProfileCreate) -> Profile:
    """Create the user's profile. Requires recorded consent (users.set_consent)."""
    if user.is_banned:
        raise UserBanned()
    if user.consent_at is None:
        raise ConsentRequired()
    if await session.scalar(select(exists().where(Profile.user_id == user.id))):
        raise ProfileAlreadyExists()
    profile = Profile(user_id=user.id, **data.model_dump())
    _apply_flags(profile)
    try:
        async with session.begin_nested():
            session.add(profile)
    except IntegrityError as exc:  # created concurrently (e.g. a double submit)
        raise ProfileAlreadyExists() from exc
    user.profile = profile
    await _submit(session, profile)
    return profile


async def update_profile(session: AsyncSession, user: User, data: ProfileUpdate) -> Profile:
    """Apply the fields set in ``data`` (see module docstring for status changes)."""
    if user.is_banned:
        raise UserBanned()
    profile = await get_profile(session, user.id)
    if profile is None:
        raise ProfileRequired()
    changes = {name: value for name, value in data.changes().items() if getattr(profile, name) != value}
    if not changes:
        return profile
    if profile.status != ProfileStatus.HIDDEN:
        await _check_submission_rate(session, user.id)
    for name, value in changes.items():
        setattr(profile, name, value)
    _apply_flags(profile)
    if profile.status == ProfileStatus.HIDDEN:
        if get_settings().PREMODERATION:
            profile.approved_at = None
        await session.flush()
    else:
        await _submit(session, profile)
    return profile


async def hide(session: AsyncSession, user: User) -> Profile:
    """Hide one's own profile from the catalog."""
    profile = await get_profile(session, user.id)
    if profile is None:
        raise ProfileRequired()
    if profile.status not in (ProfileStatus.ACTIVE, ProfileStatus.PENDING):
        raise ProfileStateError()
    profile.status = ProfileStatus.HIDDEN
    await session.flush()
    return profile


async def show(session: AsyncSession, user: User) -> Profile:
    """Show a hidden profile again (back to moderation if its content is not approved)."""
    if user.is_banned:
        raise UserBanned()
    profile = await get_profile(session, user.id)
    if profile is None:
        raise ProfileRequired()
    if profile.status != ProfileStatus.HIDDEN:
        raise ProfileStateError()
    if profile.approved_at is not None or not get_settings().PREMODERATION:
        profile.status = ProfileStatus.ACTIVE
        profile.approved_at = profile.approved_at or utcnow()
        await session.flush()
    else:
        await _check_submission_rate(session, user.id)
        await _submit(session, profile)
    return profile


async def approve(session: AsyncSession, user_id: int) -> Profile:
    """Moderator: publish a pending profile. Callers check admin rights."""
    profile = await get_profile(session, user_id)
    if profile is None:
        raise ProfileNotFound()
    if profile.status != ProfileStatus.PENDING:
        raise ProfileStateError("Анкета уже не на модерации.")
    profile.status = ProfileStatus.ACTIVE
    profile.approved_at = utcnow()
    profile.moderation_note = None
    await session.flush()
    await events.emit(
        session,
        events.EventKind.PROFILE_MODERATED,
        {"user_id": user_id, "status": ProfileStatus.ACTIVE.value},
        notify_user_ids=[user_id],
    )
    return profile


async def reject(session: AsyncSession, user_id: int, reason: str) -> Profile:
    """Moderator: reject (or take down) a profile with a reason shown to its owner."""
    try:
        note = ModerationNoteIn(note=reason).note
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    profile = await get_profile(session, user_id)
    if profile is None:
        raise ProfileNotFound()
    if profile.status == ProfileStatus.REJECTED:
        raise ProfileStateError("Анкета уже отклонена.")
    profile.status = ProfileStatus.REJECTED
    profile.approved_at = None
    profile.moderation_note = note
    await session.flush()
    await events.emit(
        session,
        events.EventKind.PROFILE_MODERATED,
        {"user_id": user_id, "status": ProfileStatus.REJECTED.value},
        notify_user_ids=[user_id],
    )
    return profile


async def list_pending(session: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[Profile]:
    """Moderation queue, oldest submission first, with ``user`` loaded."""
    result = await session.scalars(
        select(Profile)
        .where(Profile.status == ProfileStatus.PENDING)
        .options(selectinload(Profile.user))
        .order_by(Profile.submitted_at, Profile.user_id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())


def approved_content() -> ColumnElement[bool]:
    """The profile's current content was approved by a moderator (or published without
    premoderation): only such content is shown to other people."""
    return and_(Profile.approved_at.is_not(None), Profile.status != ProfileStatus.REJECTED)


def has_approved_content(profile: Profile) -> bool:
    """Python twin of ``approved_content``."""
    return profile.approved_at is not None and profile.status != ProfileStatus.REJECTED


def _visible_to(viewer_id: int, gender: Gender) -> list[ColumnElement[bool]]:
    """Catalog visibility: active, opposite gender, owner not banned, not the viewer, and no
    report between the two users in either direction."""
    return [
        Profile.status == ProfileStatus.ACTIVE,
        Profile.gender == gender.opposite,
        Profile.user_id != viewer_id,
        User.is_banned.is_(False),
        ~report_between(viewer_id, Profile.user_id),
    ]


async def _viewer_profile(session: AsyncSession, viewer: User) -> Profile:
    if viewer.is_banned:
        raise UserBanned()
    profile = await get_profile(session, viewer.id)
    if profile is None:
        raise ProfileRequired()
    return profile


def _catalog_conditions(viewer: User, own: Profile, filters: CatalogFilters) -> list[ColumnElement[bool]]:
    conditions = _visible_to(viewer.id, own.gender)
    year = current_year()
    if filters.age_min is not None:
        conditions.append(Profile.birth_year <= year - filters.age_min)
    if filters.age_max is not None:
        conditions.append(Profile.birth_year >= year - filters.age_max)
    if filters.city:
        conditions.append(_city_key(Profile.city) == normalize_city_key(filters.city))
    if filters.marital_status is not None:
        conditions.append(Profile.marital_status == filters.marital_status)
    if filters.has_children is not None:
        conditions.append(Profile.children > 0 if filters.has_children else Profile.children == 0)
    if filters.education is not None:
        conditions.append(Profile.education == filters.education)
    return conditions


async def list_catalog(
    session: AsyncSession,
    viewer: User,
    filters: CatalogFilters | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> tuple[list[Profile], int]:
    """A page of profiles visible to ``viewer`` (newest approved first) and the total count.

    The viewer needs a profile in any status (pending users may browse). Items have ``user`` loaded.
    """
    own = await _viewer_profile(session, viewer)
    page = max(page, 1)
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    conditions = _catalog_conditions(viewer, own, filters or CatalogFilters())
    base = select(Profile).join(User, User.id == Profile.user_id).where(*conditions)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = await session.scalars(
        base.options(selectinload(Profile.user))
        .order_by(Profile.approved_at.desc().nulls_last(), Profile.user_id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(items.all()), total


async def catalog_position(
    session: AsyncSession, viewer: User, filters: CatalogFilters | None, user_id: int
) -> int | None:
    """1-based position of ``user_id`` in the viewer's catalog (``list_catalog`` order), or None if
    the profile is no longer in it (reported, hidden, filtered out…). Lets one-card browsing (the
    bot) continue after the card it showed, even when the list changed meanwhile."""
    own = await _viewer_profile(session, viewer)
    conditions = _catalog_conditions(viewer, own, filters or CatalogFilters())
    visible = select(Profile).join(User, User.id == Profile.user_id).where(*conditions)
    target = await session.scalar(visible.where(Profile.user_id == user_id))
    if target is None:
        return None
    if target.approved_at is None:  # sorted last (nulls last), then by id
        before = or_(Profile.approved_at.is_not(None), Profile.user_id > target.user_id)
    else:
        before = or_(
            Profile.approved_at > target.approved_at,
            and_(Profile.approved_at == target.approved_at, Profile.user_id > target.user_id),
        )
    ahead = await session.scalar(select(func.count()).select_from(visible.where(before).subquery()))
    return (ahead or 0) + 1


async def get_viewable_profile(session: AsyncSession, viewer: User, user_id: int) -> Profile:
    """A profile ``viewer`` may open (``/u/{id}``, bot ``p_<id>``), with ``user`` loaded.

    Allowed: one's own profile; a catalog-visible profile; or the profile of someone the viewer
    has a pending/accepted interest or a conversation with (unless that user is banned) — also when
    hidden by its owner, but never content a moderator has not approved: an edited (pending) or
    rejected counterpart raises ProfileUnderReview. Raises ProfileNotFound otherwise,
    ProfileRequired if the viewer has no profile.
    """
    if user_id == viewer.id:
        own = await get_profile(session, viewer.id)
        if own is None:
            raise ProfileRequired()
        return own
    own = await _viewer_profile(session, viewer)
    connected = or_(
        exists().where(
            or_(
                and_(Interest.from_user_id == viewer.id, Interest.to_user_id == user_id),
                and_(Interest.from_user_id == user_id, Interest.to_user_id == viewer.id),
            ),
            Interest.status.in_((InterestStatus.PENDING, InterestStatus.ACCEPTED)),
        ),
        exists().where(
            Conversation.user_a_id == min(viewer.id, user_id),
            Conversation.user_b_id == max(viewer.id, user_id),
        ),
    )
    profile = await session.scalar(
        select(Profile)
        .join(User, User.id == Profile.user_id)
        .where(
            Profile.user_id == user_id,
            User.is_banned.is_(False),
            or_(and_(*_visible_to(viewer.id, own.gender)), connected),
        )
        .options(selectinload(Profile.user))
    )
    if profile is None:
        raise ProfileNotFound()
    if not has_approved_content(profile):
        raise ProfileUnderReview()
    return profile


async def public_counts(session: AsyncSession) -> dict[Gender, int]:
    """Published profiles by gender (landing page counters)."""
    rows = await session.execute(
        select(Profile.gender, func.count())
        .join(User, User.id == Profile.user_id)
        .where(Profile.status == ProfileStatus.ACTIVE, User.is_banned.is_(False))
        .group_by(Profile.gender)
    )
    counts = dict.fromkeys(Gender, 0)
    counts.update({Gender(gender): count for gender, count in rows.all()})
    return counts
