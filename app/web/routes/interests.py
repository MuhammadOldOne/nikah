"""Interests (/interests): incoming (accept / decline) and outgoing (status, cancel).

Actions are plain POST forms; HTMX swaps the updated card in place, accepting goes to the new chat.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse, Response

from app.services import guardians, interests
from app.services.errors import DomainError
from app.web.deps import IdPath, SessionDep, UserDep, flash, is_partial, safe_next_url
from app.web.render import redirect, render, render_partial, toast_response

router = APIRouter()


@router.get("/interests")
async def interests_page(
    request: Request, session: SessionDep, user: UserDep, tab: Literal["in", "out"] = "in"
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
        },
        session=session,
        user=user,
    )


async def _card_or_redirect(request: Request, session: SessionDep, user: UserDep, interest_id: int) -> Response:
    if not is_partial(request):
        back = (await request.form()).get("back")
        url = safe_next_url(back if isinstance(back, str) else None, "/interests")
        return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    interest = await interests.get_interest(session, interest_id, user)
    wali_ids = await guardians.users_with_active_guardian(session, [interest.from_user_id, interest.to_user_id])
    return render_partial(request, "interests/_card.html", {"interest": interest, "user": user, "wali_ids": wali_ids})


def _error(request: Request, exc: DomainError) -> Response:
    if is_partial(request):
        return toast_response(exc.message)
    flash(request, exc.message, "error")
    return RedirectResponse("/interests", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/interests/{interest_id:int}/accept")
async def accept(request: Request, session: SessionDep, user: UserDep, interest_id: IdPath) -> Response:
    try:
        conversation = await interests.accept(session, interest_id, user)
    except DomainError as exc:
        return _error(request, exc)
    flash(request, "Отклик принят — теперь вы можете переписываться. Начните с приветствия.", "success")
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
