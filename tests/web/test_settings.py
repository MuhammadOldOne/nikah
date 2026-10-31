"""Settings: wali invite / revoke and Telegram notifications."""

from app.services import guardians, users
from tests.web.conftest import csrf_of


async def test_settings_page_requires_login(client):
    assert (await client.get("/settings")).headers["location"] == "/login?next=%2Fsettings"


async def test_create_and_revoke_wali_invite(client, dev_login, make_profile, session):
    profile = await make_profile()
    user, user_id = profile.user, profile.user_id
    await dev_login(client, user)
    page = await client.get("/settings")
    assert "Создать ссылку для вали" in page.text
    csrf = await csrf_of(client)

    response = await client.post("/settings/wali/invite", data={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["location"] == "/settings#wali"
    invite = await guardians.get_pending_invite(session, user)
    assert invite is not None
    page = (await client.get("/settings")).text
    assert f"https://t.me/nikah_istina_bot?start=wali_{invite.invite_token}" in page
    assert "Ссылка для вали готова" in page

    await client.post("/settings/wali/revoke", data={"csrf_token": csrf})
    session.expire_all()
    assert await guardians.get_pending_invite(session, await users.get_by_id(session, user_id)) is None
    assert "Приглашение для вали отменено" in (await client.get("/settings")).text


async def test_active_wali_is_shown_and_can_be_disconnected(client, dev_login, make_profile, make_user, session):
    ward = (await make_profile()).user
    wali = await make_user(first_name="Мухаммад")
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], wali)
    await session.commit()

    await dev_login(client, ward)
    page = (await client.get("/settings")).text
    assert "Вали подключён" in page and "Мухаммад" in page and "Отключить вали" in page
    csrf = await csrf_of(client)
    await client.post("/settings/wali/revoke", data={"csrf_token": csrf})
    assert not await guardians.has_active_guardian(session, ward.id)
    assert "Вали отключён" in (await client.get("/settings")).text


async def test_toggle_telegram_notifications(client, dev_login, make_user, session):
    user = await make_user()
    user_id = user.id
    await dev_login(client, user)
    csrf = await csrf_of(client)
    await client.post("/settings/notifications", data={"csrf_token": csrf, "enabled": "0"})
    session.expire_all()
    assert (await users.get_by_id(session, user_id)).notify_telegram is False
    assert "Уведомления в Telegram выключены" in (await client.get("/settings")).text
    await client.post("/settings/notifications", data={"csrf_token": csrf, "enabled": "1"})
    session.expire_all()
    assert (await users.get_by_id(session, user_id)).notify_telegram is True


async def test_state_changing_requests_require_csrf(client, dev_login, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await dev_login(client, man)
    for path in (
        "/settings/wali/invite",
        "/settings/notifications",
        f"/p/{woman.profile.number}/interest",
        f"/p/{woman.profile.number}/report",
        f"/chats/{conversation.id}/messages",
        f"/chats/{conversation.id}/close",
        "/me/hide",
        "/me/delete",
        "/logout",
    ):
        response = await client.post(path, data={"csrf_token": "forged"})
        assert response.status_code == 403, path
        assert "Сессия устарела" in response.text
    assert (await client.get(f"/chats/{conversation.id}")).status_code == 200
