"""Settings (/settings): wali (guardian) link and Telegram notifications."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse, Response

from app.services import guardians, users
from app.services.errors import DomainError
from app.web.deps import SessionDep, UserDep, flash
from app.web.render import render

router = APIRouter()


def _back(anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/settings{anchor}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/settings")
async def settings_page(request: Request, session: SessionDep, user: UserDep) -> Response:
    invite = await guardians.get_pending_invite(session, user)
    return await render(
        request,
        "settings/index.html",
        {
            "guardian": await guardians.get_active_guardian(session, user.id),
            "invite": invite,
            "invite_link": guardians.invite_link(invite.invite_token) if invite else None,
            "wards": await guardians.list_wards(session, user),
        },
        session=session,
        user=user,
    )


@router.post("/settings/wali/invite")
async def create_wali_invite(request: Request, session: SessionDep, user: UserDep) -> Response:
    try:
        await guardians.create_invite(session, user)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        flash(request, "Ссылка для вали готова. Отправьте её вали — она действует 7 дней.", "success")
    return _back("#wali")


@router.post("/settings/wali/revoke")
async def revoke_wali(request: Request, session: SessionDep, user: UserDep) -> Response:
    had_guardian = await guardians.get_active_guardian(session, user.id) is not None
    if await guardians.revoke(session, user):
        if had_guardian:
            flash(request, "Вали отключён: копии переписки больше не отправляются.", "success")
        else:
            flash(request, "Приглашение для вали отменено.", "success")
    return _back("#wali")


@router.post("/settings/notifications")
async def notifications(request: Request, session: SessionDep, user: UserDep) -> Response:
    enabled = (await request.form()).get("enabled") == "1"
    await users.set_notify(session, user, enabled)
    flash(
        request,
        "Уведомления в Telegram включены." if enabled else "Уведомления в Telegram выключены.",
        "success",
    )
    return _back("#notifications")
