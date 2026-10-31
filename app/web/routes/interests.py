"""Interests (/interests): incoming (accept / decline) and outgoing (status, cancel).

Actions are plain POST forms; HTMX swaps the updated card in place, accepting goes to the new chat.
A refused accept (e.g. one open chat per person, MAX_OPEN_CONVERSATIONS) is explained on the card.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse, Response

from app.models import User
from app.services import guardians, interests
from app.services.errors import DomainError, InterestNotFound
from app.web.deps import IdPath, SessionDep, UserDep, flash, is_partial, safe_next_url
from app.web.render import redirect, render, render_partial, toast_response

router = APIRouter()


@router.get("/interests")
async def interests_page(
    request: Request, session: SessionDep, user: UserDep, tab: Literal["in", "out"] = "in"
) -> Response:
    return await _page(request, session, user, tab)


async def _page(
    request: Request,
    session: SessionDep,
    user: User,
    tab: str,
    card_errors: dict[int, str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    incoming = await interests.list_incoming(session, user)
    outgoing = await interests.list_outgoing(session, user)
    people = {i.from_user_id for i in incoming} | {i.to_user_id for i in outgoing}
    return await render(
        request,
        "interests/index.html",
        {
            "tab": tab,
            "incoming": incoming,
            "outgoing": outgoing,
            "wali_ids": await guardians.users_with_active_guardian(session, people),
            "own_profile": user.profile,
            "card_errors": card_errors or {},
        },
        session=session,
        user=user,
        status_code=status_code,
    )


async def _card(
    request: Request, session: SessionDep, user: User, interest_id: int, card_error: str | None = None
) -> Response:
    interest = await interests.get_interest(session, interest_id, user)
    wali_ids = await guardians.users_with_active_guardian(session, [interest.from_user_id, interest.to_user_id])
    context = {"interest": interest, "user": user, "wali_ids": wali_ids, "card_errors": {interest_id: card_error}}
    return render_partial(request, "interests/_card.html", context)


async def _card_or_redirect(request: Request, session: SessionDep, user: UserDep, interest_id: int) -> Response:
    if not is_partial(request):
        back = (await request.form()).get("back")
        url = safe_next_url(back if isinstance(back, str) else None, "/interests")
        return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    return await _card(request, session, user, interest_id)


def _error(request: Request, exc: DomainError) -> Response:
    if is_partial(request):
        return toast_response(exc.message)
    flash(request, exc.message, "error")
    return RedirectResponse("/interests", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/interests/{interest_id:int}/accept")
async def accept(request: Request, session: SessionDep, user: UserDep, interest_id: IdPath) -> Response:
    try:
        conversation = await interests.accept(session, interest_id, user)
    except InterestNotFound as exc:
        return _error(request, exc)
    except DomainError as exc:  # e.g. one of the two already has an open chat: say it on the card
        if is_partial(request):
            return await _card(request, session, user, interest_id, exc.message)
        return await _page(request, session, user, "in", {interest_id: exc.message}, status.HTTP_409_CONFLICT)
    interest = await interests.get_interest(session, interest_id, user)
    sender = interest.from_user.profile if interest.from_user else None
    who = f" с анкетой №{sender.number}" if sender else ""
    flash(request, f"Отклик принят — чат{who} открыт. Начните с приветствия.", "success")
    return redirect(request, f"/chats/{conversation.id}")


@router.post("/interests/{interest_id:int}/decline")
async def decline(request: Request, session: SessionDep, user: UserDep, interest_id: IdPath) -> Response:
    try:
        await interests.decline(session, interest_id, user)
    except DomainError as exc:
        return _error(request, exc)
    if not is_partial(request):
        flash(request, "Отклик отклонён. Отправитель получит деликатное уведомление.", "info")
    return await _card_or_redirect(request, session, user, interest_id)


@router.post("/interests/{interest_id:int}/cancel")
async def cancel(request: Request, session: SessionDep, user: UserDep, interest_id: IdPath) -> Response:
    try:
        await interests.cancel(session, interest_id, user)
    except DomainError as exc:
        return _error(request, exc)
    if not is_partial(request):
        flash(request, "Отклик отозван.", "info")
    return await _card_or_redirect(request, session, user, interest_id)
