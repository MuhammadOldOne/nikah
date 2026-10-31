"""User search, user card, ban / unban."""

from __future__ import annotations

from app.enums import Gender
from app.models import User
from app.services import profiles


async def _fresh_user(session, user_id: int) -> User:
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


async def test_search_by_number_telegram_id_and_username(admin_client, make_user, make_profile):
    alice = await make_user(telegram_id=555000111, username="Aisha_K")
    bob = await make_user(telegram_id=555000222, username="aishat")
    other = await make_user(username="ibrahim")
    sister = await make_profile(alice, gender=Gender.FEMALE, number=512)
    await make_profile(bob, number=513)

    for query in ("№512", "#512", "№ 512", "512"):
        found = await admin_client.get("/admin/users", q=query)
        assert f"/admin/users/{alice.id}" in found.text and f"/admin/users/{bob.id}" not in found.text, query
        assert "Анкета №512" in found.text
    assert "Никого не нашли" in (await admin_client.get("/admin/users", q=f"#{alice.id}")).text  # not an internal id

    by_telegram = await admin_client.get("/admin/users", q="555000222")
    assert f"/admin/users/{bob.id}" in by_telegram.text and f"/admin/users/{alice.id}" not in by_telegram.text

    by_username = await admin_client.get("/admin/users", q="@aisha")
    assert f"/admin/users/{alice.id}" in by_username.text and f"/admin/users/{other.id}" not in by_username.text
    # "_" is a literal character, not a LIKE wildcard
    exact = await admin_client.get("/admin/users", q="aisha_")
    assert f"/admin/users/{alice.id}" in exact.text and f"/admin/users/{bob.id}" not in exact.text

    without_profile = await admin_client.get("/admin/users", q="@ibrahim")
    assert "аккаунт без анкеты" in without_profile.text and f"/admin/users/{other.id}" in without_profile.text

    nothing = await admin_client.get("/admin/users", q="@nobody")
    assert "Никого не нашли" in nothing.text
    for invalid_query in ("Фатима; drop table", "№99999999999"):
        invalid = await admin_client.get("/admin/users", q=invalid_query)
        assert invalid.status_code == 200 and "Не понимаю запрос" in invalid.text, invalid_query
    # Too big for any id column: no database error (looked up as a username prefix).
    huge = await admin_client.get("/admin/users", q="9" * 20)
    assert huge.status_code == 200 and "Никого не нашли" in huge.text
    assert sister.number == 512


async def test_user_card_never_shows_phone(admin_client, session, make_profile):
    profile = await make_profile(gender=Gender.FEMALE)
    user = await _fresh_user(session, profile.user_id)
    user.phone = "+79991234567"
    await session.commit()
    response = await admin_client.get(f"/admin/users/{user.id}")
    assert response.status_code == 200
    assert f"Анкета №{profile.number}" in response.text and str(user.telegram_id) in response.text
    assert "79991234567" not in response.text and "+7" not in response.text
    search = await admin_client.get("/admin/users", q=str(user.telegram_id))
    assert "79991234567" not in search.text


async def test_unknown_user_is_404(admin_client):
    assert (await admin_client.get("/admin/users/999999")).status_code == 404
    assert (await admin_client.post("/admin/users/999999/ban")).status_code == 404


async def test_ban_and_unban(admin_client, session, make_pair, make_profile):
    _, woman = await make_pair()
    viewer = (await make_profile()).user
    response = await admin_client.post(f"/admin/users/{woman.id}/ban", next=f"/admin/users/{woman.id}")
    assert response.status_code == 303 and response.headers["location"] == f"/admin/users/{woman.id}"
    assert (await _fresh_user(session, woman.id)).is_banned
    assert (await profiles.list_catalog(session, viewer))[0] == []
    page = await admin_client.get(f"/admin/users/{woman.id}")
    assert "заблокирован" in page.text and "/unban" in page.text

    await admin_client.post(f"/admin/users/{woman.id}/unban")
    assert not (await _fresh_user(session, woman.id)).is_banned
    assert [p.user_id for p in (await profiles.list_catalog(session, viewer))[0]] == [woman.id]


async def test_admins_cannot_be_banned(admin_client, session, make_user, admin):
    other_admin = await make_user(is_admin=True)
    for target in (admin, other_admin):
        await admin_client.post(f"/admin/users/{target.id}/ban")
        assert not (await _fresh_user(session, target.id)).is_banned
    page = await admin_client.get(f"/admin/users/{other_admin.id}")
    assert "Администратора нельзя заблокировать" in page.text
    assert f'action="/admin/users/{other_admin.id}/ban"' not in page.text


async def test_ban_requires_csrf(admin_client, session, make_user):
    user = await make_user()
    response = await admin_client.http.post(f"/admin/users/{user.id}/ban")
    assert response.status_code == 403
    assert not (await _fresh_user(session, user.id)).is_banned
