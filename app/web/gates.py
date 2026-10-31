"""Friendly pages for the legacy bot's access rule (PREMODERATION): until one's own profile is
published, the catalog, other people's profiles and sending interests stay closed. The person
gets a page that says why and what to do instead of an error."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import profiles
from app.services.errors import ProfileNotActive
from app.web.deps import is_partial
from app.web.render import redirect, render


async def profile_gate(request: Request, session: AsyncSession, user: User, error: ProfileNotActive) -> Response:
    """The gate page for ``user`` (pending / rejected with the reason / hidden). An HTMX request
    (e.g. a catalog filter) reloads the whole page, which then shows the gate."""
    if is_partial(request):
        return redirect(request, request.url.path)
    profile = user.profile
    return await render(
        request,
        "profile/gate.html",
        {
            "own_profile": profile,
            "reason": error.message,
            "missing": profiles.missing_fields(profile) if profile is not None else [],
        },
        session=session,
        user=user,
    )
