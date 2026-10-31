"""Login flows: deep link + polling, magic link, Telegram widget, dev login, logout, bans."""

import hashlib
import hmac
import re
import time

import httpx

from app.enums import LoginTokenState
from app.services import auth, users
from tests.web.conftest import csrf_of


def _login_token(html: str) -> str:
    match = re.search(r"start=login_([A-Za-z0-9_\-]+)", html)
    assert match
    return match.group(1)


async def test_dev_login_is_404_unless_enabled(client, make_user):
    user = await make_user()
    response = await client.get("/auth/dev-login", params={"telegram_id": user.telegram_id})
    assert response.status_code == 404


async def test_dev_login_is_404_in_prod(client, make_user, override_settings):
    user = await make_user()
    override_settings(ENV="prod", DEV_LOGIN_ENABLED=True)
    response = await client.get("/auth/dev-login", params={"telegram_id": user.telegram_id})
    assert response.status_code == 404


async def test_dev_login_creates_account_and_sends_to_wizard(client, dev_login, session):
    response = await client.get(
        "/auth/dev-login", params={"telegram_id": 777, "username": "abdullah", "first_name": "Абдуллах"}
    )
    assert response.status_code == 303 and response.headers["location"] == "/profile/new"
    user = await users.get_by_telegram_id(session, 777)
    assert user is not None and (user.username, user.first_name) == ("abdullah", "Абдуллах")
    assert (await client.get("/catalog")).headers["location"] == "/profile/new"


async def test_login_redirects_to_safe_next_only(client, dev_login, make_profile):
    profile = await make_profile()
    ok = await dev_login(client, profile.user, next="/p/5?x=1")
    assert ok.status_code == 303 and ok.headers["location"] == "/p/5?x=1"
    for evil in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        client.cookies.clear()
        response = await dev_login(client, profile.user, next=evil)
        assert response.headers["location"] == "/catalog", evil


async def test_login_without_profile_goes_to_wizard_even_with_next(client, dev_login, make_user):
    response = await dev_login(client, await make_user(), next="/chats")
    assert response.headers["location"] == "/profile/new"


async def test_deep_link_login_with_polling(client, session, make_profile):
    profile = await make_profile()
    page = await client.get("/login?next=/interests")
    assert page.status_code == 200
    assert "Войти через Telegram" in page.text
    token = _login_token(page.text)
    poll_url = f"/auth/poll?token={token}&next=%2Finterests"

    assert (await client.get(poll_url)).json() == {"status": "pending"}
    # Reloading the page keeps the same token while it is pending.
    assert _login_token((await client.get("/login")).text) == token

    await auth.confirm_login_token(session, token, profile.user)  # done by the bot
    await session.commit()
    result = (await client.get(poll_url)).json()
    assert result == {"status": "ok", "redirect": "/interests"}
    assert (await client.get("/catalog")).status_code == 200
    assert await auth.login_token_state(session, token) == LoginTokenState.CONSUMED


async def test_login_page_finishes_confirmed_login_without_js(client, session, make_profile):
    profile = await make_profile()
    token = _login_token((await client.get("/login")).text)
    await auth.confirm_login_token(session, token, profile.user)
    await session.commit()
    response = await client.get("/login")
    assert response.status_code == 303 and response.headers["location"] == "/catalog"


async def test_poll_rejects_tokens_of_other_browsers(client, app, session, make_profile):
    profile = await make_profile()
    token = await auth.create_login_token(session)
    await auth.confirm_login_token(session, token, profile.user)
    await session.commit()
    await client.get("/login")  # this browser has its own token
    assert (await client.get(f"/auth/poll?token={token}")).json() == {"status": "expired"}
    assert (await client.get("/catalog")).status_code == 303
    assert await auth.login_token_state(session, token) == LoginTokenState.CONFIRMED


async def test_login_session_is_regenerated(client, session, make_profile):
    profile = await make_profile()
    token = _login_token((await client.get("/login")).text)
    cookie_before = client.cookies.get("istina_session")
    await auth.confirm_login_token(session, token, profile.user)
    await session.commit()
    await client.get(f"/auth/poll?token={token}")
    assert client.cookies.get("istina_session") != cookie_before
    # The login token no longer lives in the new session.
    assert (await client.get(f"/auth/poll?token={token}")).json() == {"status": "expired"}


async def test_magic_link_logs_in_once(client, session, make_profile):
    profile = await make_profile()
    url = await auth.create_magic_link(session, profile.user)
    await session.commit()
    path = url.removeprefix("http://testserver")
    response = await client.get(path)
    assert response.status_code == 303 and response.headers["location"] == "/catalog"
    assert (await client.get("/me")).status_code == 200

    client.cookies.clear()
    reused = await client.get(path)
    assert reused.headers["location"] == "/login"
    assert "Ссылка для входа устарела" in (await client.get("/login")).text


def _widget_query(telegram_id: int, bot_token: str, **fields: str) -> dict[str, str]:
    data = {"id": str(telegram_id), "first_name": "Юсуф", "auth_date": str(int(time.time())), **fields}
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hashlib.sha256(bot_token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


async def test_telegram_widget_callback(client, session, override_settings, settings):
    assert (await client.get("/auth/telegram", params={"id": "1", "hash": "x"})).status_code == 404
    override_settings(TELEGRAM_WIDGET_ENABLED=True)
    login_page = await client.get("/login")
    assert "https://telegram.org" in login_page.headers["content-security-policy"]
    assert "telegram-widget.js" in login_page.text

    bad = await client.get("/auth/telegram", params={"id": "5", "auth_date": "1", "hash": "deadbeef"})
    assert bad.headers["location"] == "/login"

    query = _widget_query(555, settings.TELEGRAM_TOKEN.get_secret_value(), username="yusuf")
    response = await client.get("/auth/telegram", params=query)
    assert response.status_code == 303 and response.headers["location"] == "/profile/new"
    user = await users.get_by_telegram_id(session, 555)
    assert user is not None and user.username == "yusuf"


async def test_telegram_widget_url_cannot_be_replayed(client, override_settings, settings):
    override_settings(TELEGRAM_WIDGET_ENABLED=True)
    query = _widget_query(556, settings.TELEGRAM_TOKEN.get_secret_value())
    assert (await client.get("/auth/telegram", params=query)).headers["location"] == "/profile/new"
    await client.post("/logout", data={"csrf_token": await csrf_of(client, "/profile/new")})

    client.cookies.clear()  # e.g. someone opening the URL from the browser history
    replay = await client.get("/auth/telegram", params=query)
    assert replay.headers["location"] == "/login"
    assert (await client.get("/settings")).status_code == 303

    stale = _widget_query(557, settings.TELEGRAM_TOKEN.get_secret_value(), auth_date=str(int(time.time()) - 600))
    assert (await client.get("/auth/telegram", params=stale)).headers["location"] == "/login"


async def test_widget_is_not_on_login_page_by_default(client):
    response = await client.get("/login")
    assert "telegram-widget.js" not in response.text
    assert "telegram.org" not in response.headers["content-security-policy"]


async def test_logout_clears_session(client, dev_login, make_profile):
    profile = await make_profile()
    await dev_login(client, profile.user)
    csrf = await csrf_of(client)
    response = await client.post("/logout", data={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert (await client.get("/me")).status_code == 303


async def test_logout_ends_the_session_on_every_device(client, app, dev_login, make_profile):
    profile = await make_profile()
    await dev_login(client, profile.user)
    copied = dict(client.cookies)  # the same session in another browser (a relative's phone)
    await client.post("/logout", data={"csrf_token": await csrf_of(client)})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as other:
        other.cookies.update(copied)
        assert (await other.get("/settings")).status_code == 303
    await dev_login(client, profile.user)  # logging in again works
    assert (await client.get("/settings")).status_code == 200


async def test_unban_does_not_revive_old_sessions(client, dev_login, session, make_profile):
    profile = await make_profile()
    await dev_login(client, profile.user)
    copied = dict(client.cookies)
    user = await users.get_by_id(session, profile.user_id)
    await users.ban(session, user)
    await users.unban(session, user)
    await session.commit()
    client.cookies.update(copied)
    assert (await client.get("/settings")).status_code == 303


async def test_logout_requires_csrf(client, dev_login, make_profile):
    profile = await make_profile()
    await dev_login(client, profile.user)
    assert (await client.post("/logout")).status_code == 403
    assert (await client.get("/me")).status_code == 200


async def test_banned_user_cannot_log_in(client, dev_login, make_user):
    banned = await make_user(is_banned=True)
    response = await dev_login(client, banned)
    assert response.headers["location"] == "/blocked"
    page = await client.get("/blocked")
    assert page.status_code == 403 and "Аккаунт заблокирован" in page.text
    assert (await client.get("/me")).status_code == 303


async def test_banned_user_is_logged_out_with_message(client, dev_login, session, make_profile):
    profile = await make_profile()
    await dev_login(client, profile.user)
    await users.ban(session, profile.user)
    await session.commit()
    response = await client.get("/catalog")
    assert response.status_code == 403
    assert "Аккаунт заблокирован" in response.text
    assert (await client.get("/me")).headers["location"].startswith("/login")
    htmx = await client.get("/catalog", headers={"HX-Request": "true"})
    assert htmx.headers["HX-Redirect"].startswith("/login")  # already logged out


async def test_next_with_control_characters_is_rejected(client, dev_login, make_profile):
    profile = await make_profile()
    for evil in ("/\t/evil.example", "/\n/evil.example", "/ /evil.example"):
        client.cookies.clear()
        response = await dev_login(client, profile.user, next=evil)
        assert response.headers["location"] == "/catalog", repr(evil)
