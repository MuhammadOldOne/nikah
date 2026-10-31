"""Live updates for every logged-in page: the SSE stream (/events) and navigation badges."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.web.deps import SessionDep, UserDep
from app.web.render import nav_counts
from app.web.sse import badge_events, sse_response

router = APIRouter()


@router.get("/events")
async def events_stream(request: Request, user: UserDep) -> StreamingResponse:
    return sse_response(request, user.id, badge_events)


@router.get("/nav/badges")
async def badges(session: SessionDep, user: UserDep) -> JSONResponse:
    return JSONResponse(asdict(await nav_counts(session, user)))
