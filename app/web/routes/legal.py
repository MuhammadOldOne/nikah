"""Legal pages: /privacy, /consent, /terms, /rules (templates that a lawyer must review)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.deps import OptionalUserDep, SessionDep
from app.web.render import render

router = APIRouter()

PAGES = {
    "privacy": "Политика конфиденциальности",
    "consent": "Согласие на обработку персональных данных",
    "terms": "Пользовательское соглашение",
    "rules": "Правила сообщества",
}


async def _legal_page(request: Request, session: SessionDep, user: OptionalUserDep, page: str) -> HTMLResponse:
    viewer = None if user is None or user.is_banned else user
    return await render(request, f"legal/{page}.html", {"page_title": PAGES[page]}, session=session, user=viewer)


@router.get("/privacy")
async def privacy(request: Request, session: SessionDep, user: OptionalUserDep) -> HTMLResponse:
    return await _legal_page(request, session, user, "privacy")


@router.get("/consent")
async def consent(request: Request, session: SessionDep, user: OptionalUserDep) -> HTMLResponse:
    return await _legal_page(request, session, user, "consent")


@router.get("/terms")
async def terms(request: Request, session: SessionDep, user: OptionalUserDep) -> HTMLResponse:
    return await _legal_page(request, session, user, "terms")


@router.get("/rules")
async def rules(request: Request, session: SessionDep, user: OptionalUserDep) -> HTMLResponse:
    return await _legal_page(request, session, user, "rules")
