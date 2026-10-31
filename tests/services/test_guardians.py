from datetime import timedelta

import pytest
from sqlalchemy import select

from app.clock import utcnow
from app.enums import GuardianStatus
from app.models import Guardian, Outbox
from app.services import guardians
from app.services.errors import GuardianInviteInvalid, GuardianSelf, UserBanned


def token_of(link: str) -> str:
    prefix = "https://t.me/nikah_istina_bot?start=wali_"
    assert link.startswith(prefix)
    return link.removeprefix(prefix)


async def test_create_invite_link_and_revoke_previous_pending(session, make_profile):
    ward = (await make_profile()).user
    first = token_of(await guardians.create_invite(session, ward))
    second = token_of(await guardians.create_invite(session, ward))
    assert len(f"wali_{second}") <= 64
    assert await guardians.get_invite(session, first) is None
    invite = await guardians.get_invite(session, second)
    assert invite is not None and invite.ward.id == ward.id and invite.ward.profile is not None
    assert (await guardians.get_pending_invite(session, ward)).invite_token == second
    statuses = {g.invite_token: g.status for g in await session.scalars(select(Guardian))}
    assert statuses == {first: GuardianStatus.REVOKED, second: GuardianStatus.PENDING}


async def test_accept_invite_links_guardian(session, make_profile, make_user):
    ward = (await make_profile()).user
    wali = await make_user()
    token = token_of(await guardians.create_invite(session, ward))
    link = await guardians.accept_invite(session, token, wali)
    assert link.status == GuardianStatus.ACTIVE and link.guardian_user_id == wali.id and link.linked_at
    assert await guardians.has_active_guardian(session, ward.id)
    assert (await guardians.get_active_guardian(session, ward.id)).guardian.id == wali.id
    assert [g.ward.id for g in await guardians.list_wards(session, wali)] == [ward.id]
    assert await guardians.get_invite(session, token) is None
    with pytest.raises(GuardianInviteInvalid):
        await guardians.accept_invite(session, token, wali)  # single use

    event = await session.scalar(select(Outbox).where(Outbox.kind == "guardian.linked"))
    assert event.payload == {
        "guardian_id": link.id,
        "ward_user_id": ward.id,
        "guardian_user_id": wali.id,
        "user_ids": sorted([ward.id, wali.id]),
    }


async def test_new_guardian_replaces_previous_one(session, make_profile, make_user):
    ward = (await make_profile()).user
    old_wali, new_wali = await make_user(), await make_user()
    await guardians.accept_invite(session, token_of(await guardians.create_invite(session, ward)), old_wali)
    # A new invite does not disconnect the current guardian until it is accepted.
    token = token_of(await guardians.create_invite(session, ward))
    assert (await guardians.get_active_guardian(session, ward.id)).guardian_user_id == old_wali.id
    await guardians.accept_invite(session, token, new_wali)
    assert (await guardians.get_active_guardian(session, ward.id)).guardian_user_id == new_wali.id
    assert await guardians.list_wards(session, old_wali) == []
    active = list(await session.scalars(select(Guardian).where(Guardian.status == GuardianStatus.ACTIVE)))
    assert len(active) == 1


async def test_accept_rules(session, make_profile, make_user):
    ward = (await make_profile()).user
    token = token_of(await guardians.create_invite(session, ward))
    with pytest.raises(GuardianSelf):
        await guardians.accept_invite(session, token, ward)
    with pytest.raises(UserBanned):
        await guardians.accept_invite(session, token, await make_user(is_banned=True))
    with pytest.raises(GuardianInviteInvalid):
        await guardians.accept_invite(session, "unknown", await make_user())

    invite = await guardians.get_invite(session, token)
    invite.expires_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    assert await guardians.get_invite(session, token) is None
    with pytest.raises(GuardianInviteInvalid):
        await guardians.accept_invite(session, token, await make_user())


async def test_banned_ward_cannot_create_invites(session, make_user):
    with pytest.raises(UserBanned):
        await guardians.create_invite(session, await make_user(is_banned=True))


async def test_revoke(session, make_profile, make_user):
    ward = (await make_profile()).user
    wali = await make_user()
    await guardians.accept_invite(session, token_of(await guardians.create_invite(session, ward)), wali)
    pending = token_of(await guardians.create_invite(session, ward))
    assert await guardians.revoke(session, ward) is True
    assert not await guardians.has_active_guardian(session, ward.id)
    assert await guardians.get_invite(session, pending) is None
    assert await guardians.revoke(session, ward) is False


async def test_batch_lookup(session, make_profile, make_user):
    with_wali = (await make_profile()).user
    without = (await make_profile()).user
    wali = await make_user()
    await guardians.accept_invite(session, token_of(await guardians.create_invite(session, with_wali)), wali)
    assert await guardians.users_with_active_guardian(session, [with_wali.id, without.id]) == {with_wali.id}
    assert await guardians.active_guardians_for(session, [with_wali.id, without.id]) == {with_wali.id: wali.id}
    assert await guardians.users_with_active_guardian(session, []) == set()
