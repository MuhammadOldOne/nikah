"""Moderation queue: approve / reject pending profiles."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.enums import Gender, ProfileStatus
from app.models import Outbox, Profile
from app.schemas import ProfileCreate, ProfileUpdate
from app.services import profiles
from app.web.routes.admin import REJECT_PRESETS, STALE_PROFILE, profile_version


async def _moderated_events(session) -> list[dict]:
    rows = await session.scalars(select(Outbox).where(Outbox.kind == "profile.moderated").order_by(Outbox.id))
    return [row.payload for row in rows]


async def _status(session, user_id: int) -> Profile:
    profile = await session.get(Profile, user_id, populate_existing=True)
    assert profile is not None
    return profile


async def test_queue_shows_pending_profiles_only(admin_client, make_profile):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING, name="Марьям")
    await make_profile(status=ProfileStatus.ACTIVE, name="Ибрагим")
    response = await admin_client.get("/admin/moderation")
    assert response.status_code == 200
    assert "Марьям" in response.text and "Ибрагим" not in response.text
    assert f'action="/admin/moderation/{pending.user_id}/approve"' in response.text
    assert f'value="{profile_version(pending)}"' in response.text


async def test_queue_highlights_stop_words(admin_client, session, make_user, override_settings):
    user = await make_user()
    data = ProfileCreate(
        gender="female",
        name="Айша",
        birth_year=1995,
        city="Уфа",
        marital_status="never_married",
        children=0,
        education="higher",
        employment="employed",
        wishes="Ищу мужа, который поможет с инвестициями.",
    )
    await profiles.create_profile(session, user, data)
    await session.commit()
    response = await admin_client.get("/admin/moderation")
    assert "<mark>инвестициями</mark>" in response.text
    assert "стоп-слова" in response.text


async def test_approve(admin_client, session, make_profile, notifications):
    profile = await make_profile(status=ProfileStatus.PENDING)
    response = await admin_client.post(
        f"/admin/moderation/{profile.user_id}/approve", version=profile_version(profile), next="/admin/moderation"
    )
    assert response.status_code == 303 and response.headers["location"] == "/admin/moderation"
    updated = await _status(session, profile.user_id)
    assert updated.status == ProfileStatus.ACTIVE and updated.approved_at is not None
    assert await _moderated_events(session) == [
        {"user_id": profile.user_id, "status": "active", "user_ids": [profile.user_id]}
    ]
    event = await asyncio.wait_for(notifications.get(), timeout=5)
    assert event["kind"] == "profile.moderated" and event["user_id"] == profile.user_id
    page = await admin_client.get("/admin/moderation")
    assert f"Анкета #{profile.user_id} опубликована." in page.text


async def test_reject_with_preset(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    await admin_client.post(
        f"/admin/moderation/{profile.user_id}/reject", version=profile_version(profile), preset="2", note=""
    )
    updated = await _status(session, profile.user_id)
    assert updated.status == ProfileStatus.REJECTED
    assert updated.moderation_note == REJECT_PRESETS[2][1]
    assert await _moderated_events(session) == [
        {"user_id": profile.user_id, "status": "rejected", "user_ids": [profile.user_id]}
    ]


async def test_reject_with_custom_note(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    await admin_client.post(
        f"/admin/moderation/{profile.user_id}/reject",
        version=profile_version(profile),
        preset="0",
        note="  Укажите настоящий город.  ",
    )
    assert (await _status(session, profile.user_id)).moderation_note == "Укажите настоящий город."


async def test_reject_needs_a_reason(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    await admin_client.post(
        f"/admin/moderation/{profile.user_id}/reject", version=profile_version(profile), preset="99", note=" "
    )
    assert (await _status(session, profile.user_id)).status == ProfileStatus.PENDING
    assert "Укажите причину." in (await admin_client.get("/admin/moderation")).text
    assert await _moderated_events(session) == []


async def test_stale_card_is_refused(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    old_version = profile_version(profile)
    await profiles.update_profile(session, profile.user, ProfileUpdate(city="Москва"))
    await session.commit()
    await admin_client.post(f"/admin/moderation/{profile.user_id}/approve", version=old_version)
    assert (await _status(session, profile.user_id)).status == ProfileStatus.PENDING
    assert STALE_PROFILE in (await admin_client.get("/admin/moderation")).text


async def test_approve_of_already_moderated_profile(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.ACTIVE)
    await admin_client.post(f"/admin/moderation/{profile.user_id}/approve", version=profile_version(profile))
    assert "Анкета уже не на модерации." in (await admin_client.get("/admin/moderation")).text
    assert await _moderated_events(session) == []


async def test_unknown_profile(admin_client):
    await admin_client.post("/admin/moderation/999/approve", version="")
    assert "Анкета не найдена" in (await admin_client.get("/admin/moderation")).text


async def test_moderation_requires_csrf(admin_client, session, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    response = await admin_client.http.post(
        f"/admin/moderation/{profile.user_id}/approve", data={"version": profile_version(profile)}
    )
    assert response.status_code == 403
    assert (await _status(session, profile.user_id)).status == ProfileStatus.PENDING


async def test_redirect_stays_inside_admin(admin_client, make_profile):
    profile = await make_profile(status=ProfileStatus.PENDING)
    response = await admin_client.post(
        f"/admin/moderation/{profile.user_id}/approve", version=profile_version(profile), next="https://evil.example/"
    )
    assert response.headers["location"] == "/admin/moderation"
