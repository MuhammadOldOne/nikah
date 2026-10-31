"""Website login through Telegram.

- Deep link: the site creates a ``login`` token and shows ``t.me/<bot>?start=login_<token>``;
  the bot calls ``confirm_login_token`` after the user presses a confirmation button; the site
  polls ``poll_login_token`` and logs the browser in (single use).
- Magic link: the bot calls ``create_magic_link``; the site calls ``consume_magic_token``.
- Telegram Login Widget: ``verify_telegram_widget``.

Tokens live 10 minutes and only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import affected_rows
from app.enums import LoginTokenKind, LoginTokenState
from app.models import LoginToken, User
from app.services.errors import TokenInvalid, UserBanned

TOKEN_TTL = timedelta(minutes=10)
WIDGET_MAX_AGE = timedelta(days=1)
LOGIN_PREFIX = "login_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login_deep_link(token: str) -> str:
    return f"https://t.me/{get_settings().BOT_USERNAME}?start={LOGIN_PREFIX}{token}"


async def create_login_token(
    session: AsyncSession, kind: LoginTokenKind = LoginTokenKind.LOGIN, user: User | None = None
) -> str:
    """Create a token and return it (raw; only its hash is stored). ``magic`` tokens need ``user``."""
    if kind == LoginTokenKind.MAGIC and user is None:
        raise ValueError("a magic token must belong to a user")
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        LoginToken(
            token_hash=_hash(token),
            kind=kind,
            user_id=user.id if user else None,
            confirmed_at=now if kind == LoginTokenKind.MAGIC else None,
            expires_at=now + TOKEN_TTL,
        )
    )
    await session.flush()
    return token


async def confirm_login_token(session: AsyncSession, token: str, user: User) -> None:
    """Bot side: bind a pending ``login`` token to ``user``.

    Idempotent for the same user; raises TokenInvalid if the token is unknown, expired, consumed
    or already confirmed by someone else, and UserBanned for banned users.
    """
    row = await session.scalar(
        select(LoginToken)
        .where(LoginToken.token_hash == _hash(token), LoginToken.kind == LoginTokenKind.LOGIN)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if row is None or row.consumed_at is not None or row.expires_at <= utcnow():
        raise TokenInvalid()
    if user.is_banned:
        raise UserBanned()
    if row.confirmed_at is not None:
        if row.user_id != user.id:
            raise TokenInvalid()
        return
    row.user_id = user.id
    row.confirmed_at = utcnow()
    await session.flush()


async def _consume(session: AsyncSession, token: str, kind: LoginTokenKind) -> int | None:
    result = await session.execute(
        update(LoginToken)
        .where(
            LoginToken.token_hash == _hash(token),
            LoginToken.kind == kind,
            LoginToken.confirmed_at.is_not(None),
            LoginToken.consumed_at.is_(None),
            LoginToken.expires_at > func.now(),
        )
        .values(consumed_at=func.now())
        .returning(LoginToken.user_id)
        .execution_options(synchronize_session=False)
    )
    user_id = result.scalar_one_or_none()
    if user_id is None:
        return None
    banned = await session.scalar(select(User.is_banned).where(User.id == user_id))
    return None if banned is None or banned else user_id


async def poll_login_token(session: AsyncSession, token: str) -> int | None:
    """Site side: the confirmed user's id (the token is consumed: single use), else None."""
    return await _consume(session, token, LoginTokenKind.LOGIN)


async def login_token_state(session: AsyncSession, token: str) -> LoginTokenState:
    """For the login page: keep polling while PENDING/CONFIRMED, stop on CONSUMED/EXPIRED."""
    row = await session.scalar(select(LoginToken).where(LoginToken.token_hash == _hash(token)))
    if row is None:
        return LoginTokenState.EXPIRED
    if row.consumed_at is not None:
        return LoginTokenState.CONSUMED
    if row.expires_at <= utcnow():
        return LoginTokenState.EXPIRED
    return LoginTokenState.CONFIRMED if row.confirmed_at is not None else LoginTokenState.PENDING


async def create_magic_link(session: AsyncSession, user: User) -> str:
    """Bot side: one-time site login URL ``{SITE_URL}/auth/magic/{token}`` valid for 10 minutes."""
    if user.is_banned:
        raise UserBanned()
    token = await create_login_token(session, LoginTokenKind.MAGIC, user)
    return f"{get_settings().SITE_URL}/auth/magic/{token}"


async def consume_magic_token(session: AsyncSession, token: str) -> int | None:
    """Site side: the user id for a valid magic token (single use), else None."""
    return await _consume(session, token, LoginTokenKind.MAGIC)


async def purge_expired_tokens(session: AsyncSession, older_than: timedelta = timedelta(days=1)) -> int:
    """Delete tokens that expired more than ``older_than`` ago. Returns the number deleted."""
    result = await session.execute(delete(LoginToken).where(LoginToken.expires_at < utcnow() - older_than))
    return affected_rows(result)


@dataclass(frozen=True, slots=True)
class TelegramWidgetUser:
    telegram_id: int
    username: str | None
    first_name: str | None


def verify_telegram_widget(
    data: Mapping[str, Any],
    bot_token: str,
    *,
    max_age: timedelta = WIDGET_MAX_AGE,
    now: float | None = None,
) -> TelegramWidgetUser | None:
    """Check Telegram Login Widget data (https://core.telegram.org/widgets/login#checking-authorization).

    ``data`` must contain exactly the fields Telegram sent (id, first_name, username, auth_date,
    hash, …) and nothing else. Returns the user or None if the hash or age check fails.
    """
    received_hash = data.get("hash")
    if not bot_token or not isinstance(received_hash, str):
        return None
    fields = {key: str(value) for key, value in data.items() if key != "hash" and value is not None}
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received_hash.encode()):
        return None
    try:
        auth_date = int(fields["auth_date"])
        telegram_id = int(fields["id"])
    except (KeyError, ValueError):
        return None
    current = time.time() if now is None else now
    if current - auth_date > max_age.total_seconds() or auth_date - current > 60:
        return None
    return TelegramWidgetUser(
        telegram_id=telegram_id,
        username=fields.get("username") or None,
        first_name=fields.get("first_name") or None,
    )
