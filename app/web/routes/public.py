"""Landing page (/) and the notice for blocked accounts (/blocked)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.services import profiles
from app.web.deps import OptionalUserDep, SessionDep
from app.web.render import render

router = APIRouter()


@router.get("/")
async def landing(request: Request, session: SessionDep, user: OptionalUserDep) -> HTMLResponse:
    viewer = None if user and user.is_banned else user
    profile = viewer.profile if viewer else None
    context = {
        "counts": await profiles.public_counts(session),
        # Logged-in owners of a profile migrated from the bot are offered «Дополнить анкету».
        "missing": profiles.missing_fields(profile) if profile else [],
    }
    return await render(request, "landing.html", context, session=session, user=viewer)


@router.get("/blocked")
async def blocked(request: Request) -> HTMLResponse:
    return await render(request, "auth/blocked.html", status_code=403)
