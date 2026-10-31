"""Fixtures for website tests: an app per test, an httpx client and the dev login."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.clock import current_year
from app.models import User

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def app() -> FastAPI:
    from app.web.main import create_app

    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        yield http


DevLogin = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def dev_login(override_settings: Callable[..., Any]) -> DevLogin:
    """``await dev_login(client, user, next=None)`` logs in through /auth/dev-login."""
    override_settings(DEV_LOGIN_ENABLED=True)

    async def login(http: httpx.AsyncClient, user: User, next: str | None = None) -> httpx.Response:
        params: dict[str, Any] = {"telegram_id": user.telegram_id, "first_name": user.first_name or "Тест"}
        if next is not None:
            params["next"] = next
        return await http.get("/auth/dev-login", params=params)

    return login


async def csrf_of(http: httpx.AsyncClient, page: str = "/settings") -> str:
    """The CSRF token rendered into the forms of ``page`` for the client's session."""
    response = await http.get(page)
    match = CSRF_RE.search(response.text)
    assert match, f"no CSRF token on {page} ({response.status_code})"
    return match.group(1)


def profile_form(**overrides: Any) -> dict[str, Any]:
    """A valid wizard submission (with all three confirmations)."""
    data: dict[str, Any] = {
        "gender": "female",
        "name": "Марьям",
        "birth_year": str(current_year() - 25),
        "city": "Казань",
        "marital_status": "never_married",
        "children": "0",
        "education": "higher",
        "employment": "employed",
        "wishes": "Ищу верующего, ответственного мужа, готового к созданию семьи.",
        "height": "",
        "weight": "",
        "occupation": "",
        "nationality": "",
        "prayer": "",
        "nikah_timeline": "",
        "relocation": "",
        "about": "",
        "consent_pd": "yes",
        "confirm_age": "yes",
        "accept_rules": "yes",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}
