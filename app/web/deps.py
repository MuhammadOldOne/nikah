"""FastAPI dependencies: DB session, current user, access guards, CSRF and login helpers.

Use the ``Annotated`` aliases in endpoints, e.g.::

    @router.get("/me")
    async def me(request: Request, session: SessionDep, user: ProfileUserDep): ...

CSRF protection is applied app-wide (see main.create_app): every POST/PUT/PATCH/DELETE must send
the session token as form field ``csrf_token`` or header ``X-CSRF-Token`` (templates:
``{{ csrf_token() }}``; HTMX: ``hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'``).
"""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import users

SESSION_USER_KEY = "uid"
SESSION_VERSION_KEY = "sv"  # users.session_version at login; a mismatch ends the session
SESSION_CSRF_KEY = "csrf"
SESSION_FLASH_KEY = "flash"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
DB_ID_MAX = 2**63 - 1  # ids are BIGINT: larger numbers are a bad request, not a database error

# Ids in URLs (``/u/{user_id:int}`` etc.): out-of-range values get the 400 page.
IdPath = Annotated[int, Path(ge=1, le=DB_ID_MAX)]

# scope="function": the session commits right after the endpoint returns, before the response is
# sent, so a redirect never reaches the browser before its data is committed. Always use this alias
# (one session per request); do not hold it inside streaming responses (SSE) — open session_scope().
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


class AccountBanned(HTTPException):
    """403 for banned users; the app's handler logs them out and explains why."""

    def __init__(self) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "Ваш аккаунт заблокирован модератором.")


class RedirectTo(Exception):
    """Raised by guards; turned into a 303 redirect (or ``HX-Redirect`` for HTMX requests)."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def safe_next_url(value: str | None, default: str = "/") -> str:
    """Allow only local absolute paths as post-login redirect targets (no open redirects)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ch.isspace() or not ch.isprintable() for ch in value):  # browsers drop tabs/newlines: "/\t/x" -> "//x"
        return default
    return value


def login_redirect_url(request: Request) -> str:
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"/login?next={quote(path, safe='')}"


async def current_user(request: Request, session: SessionDep) -> User | None:
    """The logged-in user (``profile`` loaded) or None. Also refreshes ``last_seen_at``.

    The session is signed but stored only in the browser; it stays valid while the user's
    ``session_version`` is the one recorded at login (logout everywhere and bans raise it).
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not isinstance(user_id, int):
        return None
    user = await users.get_by_id(session, user_id)
    if user is None or request.session.get(SESSION_VERSION_KEY, 0) != user.session_version:
        request.session.clear()  # account deleted or the session was ended on the server
        return None
    await users.touch_last_seen(session, user)
    return user


OptionalUserDep = Annotated[User | None, Depends(current_user)]


async def require_login(request: Request, user: OptionalUserDep) -> User:
    """Any logged-in user, including banned ones (logout, account deletion, legal pages)."""
    if user is None:
        raise RedirectTo(login_redirect_url(request))
    return user


async def require_user(user: Annotated[User, Depends(require_login)]) -> User:
    """A logged-in, not banned user."""
    if user.is_banned:
        raise AccountBanned()
    return user


async def require_profile(user: Annotated[User, Depends(require_user)]) -> User:
    """A logged-in user who has a profile (any status); otherwise redirect to /profile/new."""
    if user.profile is None:
        raise RedirectTo("/profile/new")
    return user


async def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not users.is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав.")
    return user


LoginDep = Annotated[User, Depends(require_login)]
UserDep = Annotated[User, Depends(require_user)]
ProfileUserDep = Annotated[User, Depends(require_profile)]
AdminDep = Annotated[User, Depends(require_admin)]


def get_csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """App-wide dependency: reject unsafe requests without a valid CSRF token (403)."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(SESSION_CSRF_KEY)
    sent: object = request.headers.get(CSRF_HEADER)
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            sent = (await request.form()).get(CSRF_FORM_FIELD)
    if (
        not isinstance(expected, str)
        or not isinstance(sent, str)
        or not hmac.compare_digest(expected.encode(), sent.encode())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Сессия устарела. Обновите страницу и попробуйте ещё раз.")


def is_partial(request: Request) -> bool:
    """True for HTMX (and chat.js) requests that expect an HTML fragment instead of a full page."""
    return bool(request.headers.get("HX-Request")) and not request.headers.get("HX-History-Restore-Request")


def flash(request: Request, message: str, level: str = "info") -> None:
    """Queue a one-time notice for the next rendered page (levels: info, success, error)."""
    queued = request.session.get(SESSION_FLASH_KEY)
    items = queued if isinstance(queued, list) else []
    items.append({"level": level, "message": message})
    request.session[SESSION_FLASH_KEY] = items[-5:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    queued = request.session.pop(SESSION_FLASH_KEY, None)
    return [item for item in queued if isinstance(item, dict)] if isinstance(queued, list) else []


def login_user(request: Request, user: User) -> None:
    """Start a fresh session for ``user`` (clears old data: prevents session fixation)."""
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_VERSION_KEY] = user.session_version


def logout_user(request: Request) -> None:
    """Forget the session in this browser (``users.end_web_sessions`` ends it everywhere)."""
    request.session.clear()
