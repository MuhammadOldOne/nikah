"""Login: /login, Telegram deep link polling, Login Widget, magic links, logout, dev login.

Every successful login goes through ``deps.login_user`` (fresh session: no fixation) and redirects
to /profile/new when the person has no profile yet, otherwise to a validated local ``next``.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import get_settings
from app.enums import LoginTokenState
from app.models import User
from app.services import auth, users
from app.web.deps import OptionalUserDep, SessionDep, flash, login_user, logout_user, safe_next_url
from app.web.render import render
from app.web.security import CSP_TELEGRAM_WIDGET

router = APIRouter()

SESSION_LOGIN_TOKEN = "login_token"
SESSION_LOGIN_NEXT = "login_next"
DEFAULT_AFTER_LOGIN = "/catalog"
NEXT_MAX_LENGTH = 512


def after_login_url(user: User, next_url: str | None) -> str:
    if user.profile is None:
        return "/profile/new"
    return safe_next_url((next_url or "")[:NEXT_MAX_LENGTH], DEFAULT_AFTER_LOGIN)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login")
async def login_page(request: Request, session: SessionDep, user: OptionalUserDep, next: str | None = None) -> Response:
    if user is not None and not user.is_banned:
        return _redirect(after_login_url(user, next))
    next_url = safe_next_url((next or "")[:NEXT_MAX_LENGTH], "")
    # Reuse the browser's token while it is still usable (e.g. after returning from Telegram).
    token = request.session.get(SESSION_LOGIN_TOKEN)
    state = await auth.login_token_state(session, token) if isinstance(token, str) else LoginTokenState.EXPIRED
    if isinstance(token, str) and state == LoginTokenState.CONFIRMED:
        # Already confirmed in the bot (also the way in without JavaScript: just reload the page).
        user_id = await auth.poll_login_token(session, token)
        confirmed_user = await users.get_by_id(session, user_id) if user_id is not None else None
        if confirmed_user is not None:
            return _finish_login(request, confirmed_user, next_url)
        state = LoginTokenState.CONSUMED
    resumed = isinstance(token, str) and state == LoginTokenState.PENDING
    if not isinstance(token, str) or not resumed:
        token = await auth.create_login_token(session)
        request.session[SESSION_LOGIN_TOKEN] = token
    request.session[SESSION_LOGIN_NEXT] = next_url
    settings = get_settings()
    response = await render(
        request,
        "auth/login.html",
        {
            "deep_link": auth.login_deep_link(token),
            "poll_url": f"/auth/poll?token={quote(token)}&next={quote(next_url, safe='')}",
            "resumed": resumed,
            "widget_enabled": settings.TELEGRAM_WIDGET_ENABLED,
        },
    )
    if settings.TELEGRAM_WIDGET_ENABLED:
        response.headers["Content-Security-Policy"] = CSP_TELEGRAM_WIDGET
    return response


@router.get("/auth/poll")
async def poll(
    request: Request,
    session: SessionDep,
    token: Annotated[str, Query(max_length=128)],
    next: str | None = None,
) -> JSONResponse:
    """Polled by login.js every 2 s. Only the browser that opened /login may use its token."""
    own = request.session.get(SESSION_LOGIN_TOKEN)
    if not isinstance(own, str) or not hmac.compare_digest(own.encode(), token.encode()):
        return JSONResponse({"status": "expired"})
    user_id = await auth.poll_login_token(session, token)
    user = await users.get_by_id(session, user_id) if user_id is not None else None
    if user is not None:
        login_user(request, user)
        return JSONResponse({"status": "ok", "redirect": after_login_url(user, next)})
    state = await auth.login_token_state(session, token)
    if state in (LoginTokenState.PENDING, LoginTokenState.CONFIRMED):
        return JSONResponse({"status": "pending"})
    request.session.pop(SESSION_LOGIN_TOKEN, None)
    if state == LoginTokenState.CONSUMED:
        return JSONResponse(
            {"status": "failed", "message": "Войти не удалось: аккаунт заблокирован или ссылка уже использована."}
        )
    return JSONResponse({"status": "expired"})


@router.get("/auth/telegram")
async def telegram_widget(request: Request, session: SessionDep) -> Response:
    """Telegram Login Widget callback (data-auth-url). Only Telegram's own fields are in the query."""
    settings = get_settings()
    if not settings.TELEGRAM_WIDGET_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    widget_user = auth.verify_telegram_widget(dict(request.query_params), settings.TELEGRAM_TOKEN.get_secret_value())
    if widget_user is None:
        flash(request, "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз.", "error")
        return _redirect("/login")
    user = await users.get_or_create_by_telegram(
        session, widget_user.telegram_id, widget_user.username, widget_user.first_name
    )
    return _finish_login(request, user, request.session.get(SESSION_LOGIN_NEXT))


@router.get("/auth/magic/{token}")
async def magic_link(request: Request, session: SessionDep, token: str) -> Response:
    """One-time link from the bot («🌐 Открыть сайт»)."""
    user_id = await auth.consume_magic_token(session, token[:128])
    user = await users.get_by_id(session, user_id) if user_id is not None else None
    if user is None:
        flash(request, "Ссылка для входа устарела или уже использована. Войдите заново.", "error")
        return _redirect("/login")
    return _finish_login(request, user, None)


@router.get("/auth/dev-login")
async def dev_login(
    request: Request,
    session: SessionDep,
    telegram_id: Annotated[int, Query(gt=0)],
    username: Annotated[str | None, Query(max_length=64)] = None,
    first_name: Annotated[str | None, Query(max_length=128)] = None,
    next: str | None = None,
) -> Response:
    """Development and e2e tests only: log in as any Telegram id. 404 unless ENV=dev and enabled."""
    settings = get_settings()
    if settings.ENV != "dev" or not settings.DEV_LOGIN_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = await users.get_or_create_by_telegram(session, telegram_id, username, first_name)
    return _finish_login(request, user, next)


def _finish_login(request: Request, user: User, next_url: object) -> Response:
    if user.is_banned:
        logout_user(request)
        flash(request, "Ваш аккаунт заблокирован модератором.", "error")
        return _redirect("/blocked")
    login_user(request, user)
    return _redirect(after_login_url(user, next_url if isinstance(next_url, str) else None))


@router.post("/logout")
async def logout(request: Request) -> Response:
    logout_user(request)
    flash(request, "Вы вышли из аккаунта.", "info")
    return _redirect("/")
