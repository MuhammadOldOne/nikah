"""Page rendering helpers: the layout context (current user, nav badges) and HTMX error replies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import chat, interests
from app.web.templating import templates


@dataclass(frozen=True, slots=True)
class NavCounts:
    """Badges in the navigation: pending incoming interests and unread messages."""

    interests: int
    unread: int


async def nav_counts(session: AsyncSession, user: User) -> NavCounts:
    return NavCounts(
        interests=await interests.pending_incoming_count(session, user),
        unread=await chat.unread_total(session, user),
    )


async def render(
    request: Request,
    name: str,
    context: Mapping[str, Any] | None = None,
    *,
    session: AsyncSession | None = None,
    user: User | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a full page. Pass ``session`` and ``user`` to show the logged-in navigation with badges."""
    ctx: dict[str, Any] = {"user": user, "nav": None}
    if user is not None and session is not None and not user.is_banned:
        ctx["nav"] = await nav_counts(session, user)
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def render_partial(
    request: Request, name: str, context: Mapping[str, Any] | None = None, *, status_code: int = 200
) -> HTMLResponse:
    """Render an HTML fragment for HTMX / fetch requests."""
    return templates.TemplateResponse(request, name, dict(context or {}), status_code=status_code)


def toast_trigger(message: str, level: str = "error") -> str:
    """``HX-Trigger`` header value that makes app.js show a notice (chat.js shows it by the composer)."""
    return json.dumps({"toast": {"message": message, "level": level}})  # ASCII-only: safe as a header


def toast_response(message: str, level: str = "error", *, status_code: int = 200) -> Response:
    """Reply to an HTMX request with a notice and no swap (app.js shows the ``toast`` event)."""
    return Response(status_code=status_code, headers={"HX-Reswap": "none", "HX-Trigger": toast_trigger(message, level)})


def redirect(request: Request, url: str) -> Response:
    """303 redirect, or ``HX-Redirect`` for HTMX/fetch requests (returned, not raised: the
    request's DB session commits only when the endpoint returns normally)."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
