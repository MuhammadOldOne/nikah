"""Foundation checks for app/web: health check, guards, CSRF and session helpers."""

from datetime import UTC, datetime
from typing import Annotated

import httpx
import pytest
from fastapi import Form, Request

from app.services import users
from app.web import deps
from app.web.deps import AdminDep, OptionalUserDep, ProfileUserDep, SessionDep, UserDep
from app.web.main import SESSION_COOKIE, create_app
from app.web.templating import format_msk_date, templates
from tests.conftest import session_cookie


@pytest.fixture
async def client():
    app = create_app()

    @app.get("/_t/user")
    async def user_page(user: UserDep) -> dict[str, int]:
        return {"id": user.id}

    @app.get("/_t/profile")
    async def profile_page(user: ProfileUserDep) -> dict[str, int]:
        return {"id": user.id}

    @app.get("/_t/admin")
    async def admin_page(user: AdminDep) -> dict[str, int]:
        return {"id": user.id}

    @app.post("/_t/form")
    async def form_page(user: OptionalUserDep, value: Annotated[str, Form()]) -> dict[str, str]:
        return {"value": value}

    @app.post("/_t/login/{user_id}")
    async def login(request: Request, session: SessionDep, user_id: int) -> dict[str, str]:
        user = await users.get_by_id(session, user_id)
        assert user is not None
        deps.login_user(request, user)
        return {"csrf": deps.get_csrf_token(request)}

    @app.get("/_t/render")
    async def render(request: Request):
        return templates.TemplateResponse(request, "base.html", {})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_guards_redirect_anonymous_users_to_login(client):
    response = await client.get("/_t/user?x=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F_t%2Fuser%3Fx%3D1"
    htmx = await client.get("/_t/user", headers={"HX-Request": "true"})
    assert htmx.status_code == 204 and htmx.headers["HX-Redirect"].startswith("/login?next=")


async def test_profile_and_admin_guards(client, login_client, make_user, make_profile):
    user = await make_user()
    login_client(client, user)
    assert (await client.get("/_t/user")).json() == {"id": user.id}
    response = await client.get("/_t/profile")
    assert response.status_code == 303 and response.headers["location"] == "/profile/new"
    assert (await client.get("/_t/admin")).status_code == 403

    await make_profile(user)
    assert (await client.get("/_t/profile")).json() == {"id": user.id}
    admin = await make_user(is_admin=True)
    login_client(client, admin)
    assert (await client.get("/_t/admin")).json() == {"id": admin.id}


async def test_banned_users_are_rejected(client, login_client, make_user):
    login_client(client, await make_user(is_banned=True))
    assert (await client.get("/_t/user")).status_code == 403


async def test_deleted_user_session_is_anonymous(client, login_client, make_user, session):
    user = await make_user()
    login_client(client, user)
    await users.delete_account(session, user)
    await session.commit()
    assert (await client.get("/_t/user")).status_code == 303


async def test_csrf_is_required_for_unsafe_methods(client, login_client, make_user):
    csrf = login_client(client, await make_user())
    assert (await client.post("/_t/form", data={"value": "x"})).status_code == 403
    assert (await client.post("/_t/form", data={"value": "x", "csrf_token": "wrong"})).status_code == 403
    ok = await client.post("/_t/form", data={"value": "x", "csrf_token": csrf})
    assert ok.status_code == 200 and ok.json() == {"value": "x"}
    header = await client.post("/_t/form", data={"value": "y"}, headers={"X-CSRF-Token": csrf})
    assert header.json() == {"value": "y"}


async def test_login_user_starts_fresh_session(client, login_client, make_user):
    first, second = await make_user(), await make_user()
    csrf = login_client(client, first)
    response = await client.post(f"/_t/login/{second.id}", headers={"X-CSRF-Token": csrf})
    new_csrf = response.json()["csrf"]
    assert new_csrf != csrf
    assert (await client.get("/_t/user")).json() == {"id": second.id}


async def test_session_cookie_flags(client, make_user):
    user = await make_user()
    client.cookies.clear()
    # Anonymous POST is rejected by CSRF, so log in through the endpoint with a pre-set token.
    client.cookies.set(SESSION_COOKIE, session_cookie({"csrf": "t"}))
    response = await client.post(f"/_t/login/{user.id}", headers={"X-CSRF-Token": "t"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


async def test_template_rendering(client):
    response = await client.get("/_t/render")
    assert response.status_code == 200 and '<html lang="ru">' in response.text


def test_safe_next_url():
    assert deps.safe_next_url("/catalog?page=2") == "/catalog?page=2"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil", "catalog"):
        assert deps.safe_next_url(bad) == "/"


def test_moscow_date_format():
    assert format_msk_date(datetime(2026, 10, 31, 22, 0, tzinfo=UTC)) == "1 ноября 2026"


def test_create_app_is_importable_as_module_attribute():
    from app.web import main

    assert main.app is not None and create_app() is not main.app


async def test_shared_web_client_fixture(web_client):
    assert (await web_client.get("/healthz")).status_code == 200
