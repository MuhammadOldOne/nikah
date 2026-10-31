"""Shared test fixtures for all test packages (services, web, bot, admin).

Environment is fixed here *before* the app is imported. Each test starts with empty tables.
Factories commit, so data is visible to other connections (web app, bot, listeners).

    async def test_x(session, make_user, make_profile):
        user = await make_user()
        profile = await make_profile(user, gender=Gender.FEMALE)
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Protocol

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah_test")
if "test" not in TEST_DATABASE_URL.rsplit("/", 1)[-1]:
    raise RuntimeError("Tests drop the whole schema: TEST_DATABASE_URL must point to a *test* database")
os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL,
        "ENV": "dev",
        "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789",
        "TELEGRAM_TOKEN": "123456:TEST-TOKEN",
        "BOT_USERNAME": "nikah_istina_bot",
        "SITE_URL": "http://testserver",
        "ADMIN_TELEGRAM_IDS": "",
        "PREMODERATION": "true",
        "INTEREST_DAILY_LIMIT": "10",
        "DEV_LOGIN_ENABLED": "false",
        "TELEGRAM_WIDGET_ENABLED": "false",
        "SESSION_COOKIE_SECURE": "false",
    }
)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import itsdangerous  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.clock import current_year, utcnow  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import dispose_engine, get_engine, get_sessionmaker  # noqa: E402
from app.enums import (  # noqa: E402
    Education,
    Employment,
    Gender,
    MaritalStatus,
    ProfileStatus,
)
from app.models import Base, Conversation, Profile, User  # noqa: E402
from app.services import interests  # noqa: E402
from app.services.events import EVENTS_CHANNEL, asyncpg_dsn  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _database_schema() -> AsyncIterator[None]:
    """Recreate the schema from the models once per test run."""
    async with get_engine().begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
async def _clean_tables(_database_schema: None) -> None:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session for the test body. Services only flush: commit explicitly when another
    connection must see the data. Uncommitted changes are rolled back at teardown."""
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def override_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Settings]]:
    """``override_settings(PREMODERATION=False)`` changes the cached settings for one test."""

    def apply(**values: Any) -> Settings:
        current = get_settings()
        for name, value in values.items():
            if name not in Settings.model_fields:
                raise AttributeError(name)
            monkeypatch.setattr(current, name, value)
        return current

    yield apply


_telegram_ids = itertools.count(100_000_001)


class MakeUser(Protocol):
    def __call__(
        self,
        *,
        telegram_id: int | None = None,
        username: str | None = None,
        first_name: str | None = "Тест",
        consent: bool = True,
        is_admin: bool = False,
        is_banned: bool = False,
        notify_telegram: bool = True,
    ) -> Awaitable[User]: ...


class MakeProfile(Protocol):
    def __call__(
        self,
        user: User | None = None,
        *,
        gender: Gender = Gender.MALE,
        status: ProfileStatus = ProfileStatus.ACTIVE,
        **fields: Any,
    ) -> Awaitable[Profile]: ...


@pytest.fixture
def make_user(session: AsyncSession) -> MakeUser:
    """Create and commit a user (consent given by default)."""

    async def factory(
        *,
        telegram_id: int | None = None,
        username: str | None = None,
        first_name: str | None = "Тест",
        consent: bool = True,
        is_admin: bool = False,
        is_banned: bool = False,
        notify_telegram: bool = True,
    ) -> User:
        user = User(
            telegram_id=telegram_id if telegram_id is not None else next(_telegram_ids),
            username=username,
            first_name=first_name,
            consent_at=utcnow() if consent else None,
            is_admin=is_admin,
            is_banned=is_banned,
            banned_at=utcnow() if is_banned else None,
            notify_telegram=notify_telegram,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user, ["profile"])
        return user

    return factory


@pytest.fixture
def make_profile(session: AsyncSession, make_user: MakeUser) -> MakeProfile:
    """Create and commit a valid profile (ACTIVE and approved by default) for ``user`` or a new user.

    Extra keyword arguments override profile fields (e.g. ``birth_year=1990, city="Казань"``).
    """

    async def factory(
        user: User | None = None,
        *,
        gender: Gender = Gender.MALE,
        status: ProfileStatus = ProfileStatus.ACTIVE,
        **fields: Any,
    ) -> Profile:
        user = user or await make_user()
        now = utcnow()
        values: dict[str, Any] = {
            "name": "Ахмад" if gender == Gender.MALE else "Фатима",
            "birth_year": current_year() - 30,
            "city": "Казань",
            "marital_status": MaritalStatus.NEVER_MARRIED,
            "children": 0,
            "education": Education.HIGHER,
            "employment": Employment.EMPLOYED,
            "wishes": "Ищу верующего спутника жизни для создания семьи.",
            "submitted_at": now,
            "approved_at": now if status in (ProfileStatus.ACTIVE, ProfileStatus.HIDDEN) else None,
        }
        values.update(fields)
        profile = Profile(user=user, gender=gender, status=status, **values)
        session.add(profile)
        await session.commit()
        return profile

    return factory


@pytest.fixture
def make_pair(make_profile: MakeProfile) -> Callable[[], Awaitable[tuple[User, User]]]:
    """``man, woman = await make_pair()``: two users with ACTIVE profiles of opposite genders."""

    async def factory() -> tuple[User, User]:
        man = await make_profile(gender=Gender.MALE)
        woman = await make_profile(gender=Gender.FEMALE)
        return man.user, woman.user

    return factory


@pytest.fixture
def make_conversation(session: AsyncSession) -> Callable[[User, User], Awaitable[Conversation]]:
    """Open a conversation the regular way (interest from the first user, accepted by the second).
    Both users need ACTIVE profiles of opposite genders. Commits."""

    async def factory(sender: User, recipient: User) -> Conversation:
        interest = await interests.send_interest(session, sender, recipient.id)
        conversation = await interests.accept(session, interest.id, recipient)
        await session.commit()
        return conversation

    return factory


@pytest.fixture
async def notifications() -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    """Queue of decoded NOTIFY payloads received on the ``events`` channel by a separate connection."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    conn = await asyncpg.connect(asyncpg_dsn(TEST_DATABASE_URL))
    await conn.add_listener(EVENTS_CHANNEL, lambda _c, _pid, _ch, payload: queue.put_nowait(json.loads(payload)))
    try:
        yield queue
    finally:
        await conn.close()


@pytest.fixture
async def web_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client for a fresh ``create_app()`` (lifespan is not run).

    Use this (httpx + ASGITransport on the test event loop) rather than Starlette's TestClient,
    which runs the app in another event loop and breaks the shared asyncpg pool.
    """
    from app.web.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def session_cookie(data: dict[str, Any]) -> str:
    """A signed session cookie value exactly as Starlette's SessionMiddleware writes it."""
    payload = base64.b64encode(json.dumps(data).encode())
    signer = itsdangerous.TimestampSigner(get_settings().SECRET_KEY.get_secret_value())
    return signer.sign(payload).decode()


@pytest.fixture
def login_client() -> Callable[[httpx.AsyncClient, User], str]:
    """``csrf = login_client(web_client, user)`` logs the client in; send ``csrf`` as
    ``csrf_token`` (form) or ``X-CSRF-Token`` (header) with unsafe requests."""
    from app.web.deps import SESSION_CSRF_KEY, SESSION_USER_KEY
    from app.web.main import SESSION_COOKIE

    def apply(client: httpx.AsyncClient, user: User) -> str:
        csrf = f"csrf-{user.id}"
        client.cookies.set(SESSION_COOKIE, session_cookie({SESSION_USER_KEY: user.id, SESSION_CSRF_KEY: csrf}))
        return csrf

    return apply
