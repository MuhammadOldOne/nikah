"""Profile wizard (/profile/new), own profile (/me, /me/edit) and viewing profiles (/u/{id})."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from app.enums import ProfileStatus
from app.schemas import INTEREST_MESSAGE_MAX, REPORT_COMMENT_MAX, ProfileCreate, ProfileUpdate, humanize_errors
from app.services import guardians, interests, profiles, users
from app.services.errors import DomainError, ProfileAlreadyExists, ProfileNotFound
from app.web.deps import LoginDep, ProfileUserDep, SessionDep, UserDep, flash, logout_user
from app.web.forms import CONSENT_FIELDS, PROFILE_FIELDS, first_error_step, form_context, form_values, profile_values
from app.web.render import render
from app.web.reports import submit_report

router = APIRouter()

PENDING_NOTICE = (
    "Анкета отправлена на модерацию — обычно это занимает до суток. Смотреть анкеты можно уже сейчас, "
    "а откликаться — после публикации."
)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


# --- Wizard -----------------------------------------------------------------------------------


@router.get("/profile/new")
async def wizard(request: Request, session: SessionDep, user: UserDep) -> Response:
    if user.profile is not None:
        return _redirect("/me")
    values = dict.fromkeys(PROFILE_FIELDS, "") | {"children": "0"}
    context = form_context(values, {}, None) | {"consents": {}, "initial_step": 1}
    return await render(request, "profile/wizard.html", context, session=session, user=user)


@router.post("/profile/new")
async def wizard_submit(request: Request, session: SessionDep, user: UserDep) -> Response:
    if user.profile is not None:
        return _redirect("/me")
    form = await request.form()
    values = form_values(form)
    consents = {name: form.get(name) == "yes" for name in CONSENT_FIELDS}
    errors = {name: message for name, message in CONSENT_FIELDS.items() if not consents[name]}
    data: ProfileCreate | None = None
    try:
        data = ProfileCreate.model_validate(values)
    except ValidationError as exc:
        errors = humanize_errors(exc) | errors
    if data is None or errors:
        context = form_context(values, errors, values["gender"]) | {
            "consents": consents,
            "initial_step": first_error_step(errors),
        }
        return await render(request, "profile/wizard.html", context, session=session, user=user, status_code=422)
    await users.set_consent(session, user)
    try:
        profile = await profiles.create_profile(session, user, data)
    except ProfileAlreadyExists:
        return _redirect("/me")
    if profile.status == ProfileStatus.PENDING:
        flash(request, PENDING_NOTICE, "success")
    else:
        flash(request, "Анкета опубликована.", "success")
    return _redirect("/catalog")


# --- Own profile ----------------------------------------------------------------------------------


@router.get("/me")
async def me(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = user.profile
    assert profile is not None
    return await render(
        request,
        "profile/me.html",
        {
            "profile": profile,
            "has_guardian": await guardians.has_active_guardian(session, user.id),
        },
        session=session,
        user=user,
    )


@router.get("/me/edit")
async def edit(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = user.profile
    assert profile is not None
    context = form_context(profile_values(profile), {}, profile.gender.value)
    return await render(request, "profile/edit.html", context, session=session, user=user)


@router.post("/me/edit")
async def edit_submit(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = user.profile
    assert profile is not None
    values = form_values(await request.form())
    try:
        data = ProfileUpdate.model_validate({name: values[name] for name in PROFILE_FIELDS if name != "gender"})
    except ValidationError as exc:
        context = form_context(values, humanize_errors(exc), profile.gender.value)
        return await render(request, "profile/edit.html", context, session=session, user=user, status_code=422)
    before = profile_values(profile)
    try:
        profile = await profiles.update_profile(session, user, data)
    except DomainError as exc:
        flash(request, exc.message, "error")
        return _redirect("/me")
    if profile_values(profile) == before:
        flash(request, "Изменений нет.", "info")
    elif profile.status == ProfileStatus.PENDING:
        flash(request, "Изменения сохранены и отправлены на модерацию.", "success")
    elif profile.status == ProfileStatus.HIDDEN:
        flash(request, "Изменения сохранены. Анкета скрыта — покажите её, когда будете готовы.", "success")
    else:
        flash(request, "Изменения сохранены.", "success")
    return _redirect("/me")


@router.post("/me/hide")
async def hide(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    try:
        await profiles.hide(session, user)
        flash(request, "Анкета скрыта: её не видно в каталоге. Начатые переписки продолжаются.", "success")
    except DomainError as exc:
        flash(request, exc.message, "error")
    return _redirect("/me")


@router.post("/me/show")
async def show(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    try:
        profile = await profiles.show(session, user)
    except DomainError as exc:
        flash(request, exc.message, "error")
        return _redirect("/me")
    if profile.status == ProfileStatus.PENDING:
        flash(request, "Анкета снова отправлена на модерацию и появится в каталоге после проверки.", "success")
    else:
        flash(request, "Анкета снова видна в каталоге.", "success")
    return _redirect("/me")


@router.post("/me/delete")
async def delete_account(request: Request, session: SessionDep, user: LoginDep) -> Response:
    form = await request.form()
    if form.get("confirm") != "yes":
        flash(request, "Чтобы удалить аккаунт, отметьте подтверждение.", "error")
        return _redirect("/me" if form.get("back") == "/me" else "/settings")
    await users.delete_account(session, user)
    logout_user(request)
    flash(request, "Аккаунт и все ваши данные удалены.", "info")
    return _redirect("/")


# --- Viewing other profiles -------------------------------------------------------------------


@router.get("/u/{user_id:int}")
async def view_profile(request: Request, session: SessionDep, user: ProfileUserDep, user_id: int) -> Response:
    try:
        profile = await profiles.get_viewable_profile(session, user, user_id)
    except ProfileNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    is_own = profile.user_id == user.id
    relation = None if is_own else await interests.get_relation(session, user, user_id)
    return await render(
        request,
        "profile/view.html",
        {
            "profile": profile,
            "is_own": is_own,
            "relation": relation,
            "own_profile": user.profile,
            "has_guardian": await guardians.has_active_guardian(session, profile.user_id),
            "message_max": INTEREST_MESSAGE_MAX,
            "comment_max": REPORT_COMMENT_MAX,
        },
        session=session,
        user=user,
    )


@router.post("/u/{user_id:int}/interest")
async def send_interest(request: Request, session: SessionDep, user: ProfileUserDep, user_id: int) -> Response:
    form = await request.form()
    message = form.get("message")
    try:
        await interests.send_interest(session, user, user_id, message if isinstance(message, str) else None)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        flash(request, "Отклик отправлен. Мы сообщим, когда на него ответят.", "success")
    return _redirect(f"/u/{user_id}")


@router.post("/u/{user_id:int}/report")
async def report_profile(request: Request, session: SessionDep, user: ProfileUserDep, user_id: int) -> Response:
    if not await submit_report(request, session, user, user_id):
        return _redirect(f"/u/{user_id}")
    flash(request, "Жалоба отправлена модератору. Этот человек больше не будет виден вам в каталоге.", "success")
    return _redirect("/catalog")
