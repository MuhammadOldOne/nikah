"""Security headers for every response (pure ASGI middleware, safe for streaming responses) and
redaction of login secrets from the server's access log."""

from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# No inline scripts or styles anywhere: all JS and CSS are files under /static.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
# The login page with the Telegram Login Widget additionally loads telegram.org (script + iframe).
CSP_TELEGRAM_WIDGET = CSP.replace("script-src 'self'", "script-src 'self' https://telegram.org").replace(
    "frame-src 'none'", "frame-src https://oauth.telegram.org"
)

STATIC_PREFIX = "/static/"
BASE_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware:
    """Adds CSP, anti-framing, referrer, MIME-sniffing and caching headers.

    A route may send its own ``Content-Security-Policy`` (the login page does for the widget).
    Pages are ``no-store`` (they contain personal data); static files are cacheable.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        query: bytes = scope.get("query_string", b"")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP)
                for name, value in BASE_HEADERS.items():
                    headers.setdefault(name, value)
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000")
                if path.startswith(STATIC_PREFIX):
                    versioned = query.startswith(b"v=")
                    headers.setdefault(
                        "Cache-Control", "public, max-age=31536000, immutable" if versioned else "public, max-age=3600"
                    )
                else:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


ACCESS_LOGGER = "uvicorn.access"
AUTH_PREFIX = "/auth/"


class AccessLogRedaction(logging.Filter):
    """Drops the query of ``/auth/*`` requests from uvicorn's access log lines: it carries login
    credentials (signed Telegram Login Widget data, login tokens), and magic link tokens are in
    the path. The record's arguments are ``(client, method, path_with_query, http_version, status)``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str) and args[2].startswith(AUTH_PREFIX):
            path = args[2].partition("?")[0]
            if path.startswith(f"{AUTH_PREFIX}magic/"):
                path = f"{AUTH_PREFIX}magic/…"
            record.args = (args[0], args[1], path, args[3], args[4])
        return True


def install_access_log_redaction() -> None:
    """Attach AccessLogRedaction to uvicorn's access logger (idempotent)."""
    access_logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(item, AccessLogRedaction) for item in access_logger.filters):
        access_logger.addFilter(AccessLogRedaction())
