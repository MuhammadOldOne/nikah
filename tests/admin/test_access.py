"""Every admin route is closed to anyone but admins; POST actions need the CSRF token."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app.web.routes import admin as admin_routes

ADMIN_ROUTES: list[tuple[str, str]] = sorted(
    (method, re.sub(r"\{[^}]+\}", "1", route.path))
    for route in admin_routes.router.routes
    if isinstance(route, APIRoute)
    for method in route.methods
)


def test_router_covers_all_sections():
    paths = {path for _, path in ADMIN_ROUTES}
    sections = {"/admin", "/admin/moderation", "/admin/reports", "/admin/flagged", "/admin/chats", "/admin/users"}
    assert sections <= paths
    assert all(path.startswith("/admin") for path in paths)


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_non_admin_gets_403(web_client, login_client, make_user, make_profile, method, path):
    user = await make_user()
    await make_profile(user)
    csrf = login_client(web_client, user)
    response = await web_client.request(method, path, data={"csrf_token": csrf} if method == "POST" else None)
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_anonymous_is_sent_to_login(web_client, method, path):
    response = await web_client.request(method, path)
    if method == "GET":
        assert response.status_code == 303 and response.headers["location"].startswith("/login?next=")
    else:
        assert response.status_code == 403  # no session, no CSRF token


async def test_banned_admin_is_rejected(web_client, login_client, make_user):
    login_client(web_client, await make_user(is_admin=True, is_banned=True))
    assert (await web_client.get("/admin")).status_code == 403


async def test_admin_from_settings(web_client, login_client, make_user, override_settings):
    user = await make_user()
    override_settings(ADMIN_TELEGRAM_IDS=[user.telegram_id])
    login_client(web_client, user)
    assert (await web_client.get("/admin")).status_code == 200


async def test_admin_pages_are_not_indexed_and_not_cached(admin_client):
    response = await admin_client.get("/admin")
    assert response.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "style=" not in response.text and "<script" not in response.text  # CSP: no inline code


@pytest.mark.parametrize(
    "path",
    ["/admin/moderation/1/approve", "/admin/moderation/1/reject", "/admin/reports/1/resolve", "/admin/users/1/ban"],
)
async def test_admin_posts_require_csrf(admin_client, path):
    response = await admin_client.http.post(path, data={"version": "", "action": "dismiss"})
    assert response.status_code == 403
    wrong = await admin_client.http.post(path, data={"version": "", "action": "dismiss", "csrf_token": "wrong"})
    assert wrong.status_code == 403


async def test_site_navigation_links_to_admin_only_for_admins(web_client, login_client, make_user, make_profile):
    user = (await make_profile()).user
    login_client(web_client, user)
    assert 'href="/admin"' not in (await web_client.get("/settings")).text

    admin = (await make_profile(await make_user(is_admin=True))).user
    login_client(web_client, admin)
    assert 'href="/admin"' in (await web_client.get("/settings")).text
