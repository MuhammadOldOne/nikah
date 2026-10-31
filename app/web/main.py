"""ASGI application: ``uvicorn app.web.main:app``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.db import dispose_engine
from app.logutil import exc_summary
from app.services.errors import DomainError, NotFound
from app.web.broker import EventBroker
from app.web.deps import AccountBanned, RedirectTo, SessionDep, is_partial, logout_user, verify_csrf
from app.web.render import toast_response
from app.web.routes import admin, auth, catalog, chats, interests, legal, live, profile, public
from app.web.routes import settings as settings_routes
from app.web.security import SecurityHeadersMiddleware, install_access_log_redaction
from app.web.templating import templates

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "istina_session"
SESSION_MAX_AGE = 30 * 24 * 3600

ROBOTS_TXT = """User-agent: *
Allow: /$
Allow: /login
Allow: /privacy
Allow: /consent
Allow: /terms
Allow: /rules
Allow: /static/
Disallow: /
"""

ERROR_TITLES = {
    400: "Неверный запрос",
    403: "Доступ закрыт",
    404: "Страница не найдена",
    405: "Действие недоступно",
    500: "Что-то пошло не так",
}
ERROR_TEXTS = {
    400: "Проверьте ссылку или вернитесь назад и попробуйте ещё раз.",
    403: "У вас нет доступа к этой странице.",
    404: "Возможно, анкета скрыта, удалена или ссылка устарела.",
    405: "Это действие нельзя выполнить таким способом.",
    500: "Мы уже знаем об ошибке. Попробуйте обновить страницу чуть позже.",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    broker: EventBroker = app.state.broker
    await broker.start()
    try:
        yield
    finally:
        await broker.stop()
        await dispose_engine()


def _error_page(request: Request, status_code: int, message: str | None = None) -> Response:
    title = ERROR_TITLES.get(status_code, ERROR_TITLES[400 if status_code < 500 else 500])
    text_ = message or ERROR_TEXTS.get(status_code, ERROR_TEXTS[400 if status_code < 500 else 500])
    if is_partial(request):
        return toast_response(text_, status_code=status_code)
    return templates.TemplateResponse(
        request,
        "errors/error.html",
        {"user": None, "nav": None, "minimal": True, "status_code": status_code, "title": title, "message": text_},
        status_code=status_code,
    )


async def _redirect(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RedirectTo)
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": exc.url})
    return RedirectResponse(exc.url, status_code=303)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if isinstance(exc, AccountBanned):
        logout_user(request)  # banned users are logged out and told why
        if request.headers.get("HX-Request"):
            return Response(status_code=exc.status_code, headers={"HX-Redirect": "/blocked"})
        return templates.TemplateResponse(
            request, "auth/blocked.html", {"user": None, "nav": None}, status_code=exc.status_code
        )
    default_details = {404: "Not Found", 405: "Method Not Allowed"}
    message = None if exc.detail in (None, default_details.get(exc.status_code)) else str(exc.detail)
    return _error_page(request, exc.status_code, message)


async def _validation_error(request: Request, exc: Exception) -> Response:
    return _error_page(request, 400)


async def _domain_error(request: Request, exc: Exception) -> Response:
    """Safety net for business-rule errors that a route did not handle itself."""
    assert isinstance(exc, DomainError)
    return _error_page(request, 404 if isinstance(exc, NotFound) else 400, exc.message)


class UnhandledErrorMiddleware:
    """Answers unhandled errors with the styled 500 page and logs them without personal data.

    Error messages may contain user data (database errors quote values, e.g. profile fields or
    chat texts), so only the type and stack frames are logged. Starlette's own 500 handler would
    re-raise the error and the server would log it again in full, so errors stop here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            response_started = response_started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error("unhandled error on %s %s: %s", scope["method"], scope["path"], exc_summary(exc))
            if not response_started:  # a streaming response that failed midway just ends
                await _error_page(Request(scope, receive), 500)(scope, receive, send)


async def healthz(session: SessionDep) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


async def robots_txt() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS_TXT)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Истина",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_csrf)],
    )
    app.state.broker = EventBroker()
    # Added first = innermost: its 500 page still gets the session and the security headers.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SECRET_KEY.get_secret_value(),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=bool(settings.SESSION_COOKIE_SECURE),
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_prod)
    app.add_exception_handler(RedirectTo, _redirect)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(DomainError, _domain_error)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/robots.txt", robots_txt, methods=["GET"], include_in_schema=False)
    for module in (public, auth, profile, catalog, interests, chats, live, settings_routes, admin, legal):
        app.include_router(module.router)
    install_access_log_redaction()
    return app


app = create_app()
