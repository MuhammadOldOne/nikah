"""Consent page (/welcome) for accounts migrated from the legacy Telegram bot.

They have no recorded consent to personal data processing (152-FZ: a separate consent, also for
religious beliefs), so every logged-in page sends them here first (deps.require_user). The page
explains that the bot became «сайт + бот» and that their profile and chats are kept.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse, Response

from app.models import User
from app.services import users
from app.web.deps import AccountDep, SessionDep, flash, safe_next_url
from app.web.render import render

router = APIRouter()

NEXT_MAX_LENGTH = 512
# Checkbox -> error message when it is not ticked.
CONSENT_CHECKS: dict[str, str] = {
    "consent_pd": "Без согласия на обработку персональных данных пользоваться сервисом нельзя.",
    "accept_rules": "Нужно принять правила сообщества и пользовательское соглашение.",
}


def _after_consent(user: User, next_url: str | None) -> str:
    default = "/catalog" if user.profile is not None else "/profile/new"
    return safe_next_url((next_url or "")[:NEXT_MAX_LENGTH], default)


async def _page(
    request: Request,
    session: SessionDep,
    user: User,
    next_url: str,
    errors: dict[str, str],
    checked: dict[str, bool],
    status_code: int = status.HTTP_200_OK,
) -> Response:
    return await render(
        request,
        "consent/welcome.html",
        {"next_url": next_url, "errors": errors, "checked": checked, "own_profile": user.profile},
        session=session,
        user=user,
        status_code=status_code,
    )


@router.get("/welcome")
async def welcome(request: Request, session: SessionDep, user: AccountDep, next: str | None = None) -> Response:
    if not users.needs_consent(user):
        return RedirectResponse(_after_consent(user, next), status_code=status.HTTP_303_SEE_OTHER)
    return await _page(request, session, user, safe_next_url((next or "")[:NEXT_MAX_LENGTH], ""), {}, {})


@router.post("/welcome")
async def give_consent(request: Request, session: SessionDep, user: AccountDep) -> Response:
    form = await request.form()
    raw_next = form.get("next")
    next_url = safe_next_url(raw_next[:NEXT_MAX_LENGTH] if isinstance(raw_next, str) else None, "")
    checked = {name: form.get(name) == "yes" for name in CONSENT_CHECKS}
    errors = {name: message for name, message in CONSENT_CHECKS.items() if not checked[name]}
    if errors:
        return await _page(request, session, user, next_url, errors, checked, status.HTTP_422_UNPROCESSABLE_CONTENT)
    first_time = users.needs_consent(user)
    await users.set_consent(session, user)
    if first_time:
        flash(request, "Спасибо! Согласие сохранено — добро пожаловать в «Никах Истину».", "success")
    return RedirectResponse(_after_consent(user, next_url), status_code=status.HTTP_303_SEE_OTHER)
