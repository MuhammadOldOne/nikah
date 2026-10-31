"""Fixtures for admin panel tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.models import User


@dataclass
class AdminClient:
    """Logged-in admin: ``await admin_client.post(url, field=value)`` sends the CSRF token."""

    http: httpx.AsyncClient
    csrf: str
    user: User

    async def get(self, url: str, **params: Any) -> httpx.Response:
        return await self.http.get(url, params=params or None)

    async def post(self, url: str, **data: Any) -> httpx.Response:
        return await self.http.post(url, data={"csrf_token": self.csrf, **data})


@pytest.fixture
async def admin(make_user: Callable[..., Any]) -> User:
    return await make_user(is_admin=True, first_name="Модератор")


@pytest.fixture
def admin_client(web_client: httpx.AsyncClient, login_client: Callable[..., str], admin: User) -> AdminClient:
    return AdminClient(web_client, login_client(web_client, admin), admin)
