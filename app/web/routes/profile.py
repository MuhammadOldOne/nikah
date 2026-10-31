"""Profile wizard (/profile/new), own profile (/me, /me/edit, /me/complete) and viewing profiles
(/p/{number}: people are known by their profile number only, never by internal ids)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from app.enums import ProfileStatus
from app.models import Profile, User
from app.schemas import (
    FIELD_LABELS,
    INTEREST_MESSAGE_MAX,
    REPORT_COMMENT_MAX,
    ProfileCreate,
    ProfileUpdate,
    humanize_errors,
)
from app.services import guardians, interests, profiles, users
from app.services.errors import (
    DomainError,
    InterestLimitReached,
    InvalidInput,
    ProfileAlreadyExists,
    ProfileNotActive,
    ProfileNotFound,
    ProfileUnderReview,
)
from app.text import profile_title
from app.web.deps import (
    CONSENT_PAGE,
    AccountDep,
    LoginDep,
    NumberPath,
    ProfileUserDep,
    SessionDep,
    flash,
    logout_user,
)
from app.web.forms import (
    CONSENT_FIELDS,
    PROFILE_FIELDS,
    first_error_step,
    form_context,
    form_values,
    profile_values,
)
from app.web.gates import profile_gate
from app.web.render import render
from app.web.reports import submit_report

router = APIRouter()

PENDING_NOTICE = (
    "Анкета отправлена на модерацию — обычно это занимает до суток. "
    "Смотреть анкеты и откликаться можно будет после публикации."
)
DELETE_BACK_PAGES = frozenset({"/me", "/settings", CONSENT_PAGE})


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _own_profile(user: User) -> Profile:
    profile = user.profile
    assert profile is not None  # ProfileUserDep
    return profile


# --- Wizard -----------------------------------------------------------------------------------
# AccountDep: the wizard itself asks for consent (people without a profile have not given it yet).


@router.get("/profile/new")
async def wizard(request: Request, session: SessionDep, user: AccountDep) -> Response:
    if user.profile is not None:
        return _redirect("/me")
    values = dict.fromkeys(PROFILE_FIELDS, "") | {"children": "0"}
    context = form_context(values, {}, None) | {"consents": {}, "initial_step": 1}
    return await render(request, "profile/wizard.html", context, session=session, user=user)


@router.post("/profile/new")
async def wizard_submit(request: Request, session: SessionDep, user: AccountDep) -> Response:
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
        flash(request, f"{profile_title(profile)} создана. {PENDING_NOTICE}", "success")
        return _redirect("/me")
    flash(request, f"{profile_title(profile)} опубликована.", "success")
    return _redirect("/catalog")


# --- Own profile ----------------------------------------------------------------------------------


@router.get("/me")
async def me(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = _own_profile(user)
    return await render(
        request,
        "profile/me.html",
        {
            "profile": profile,
            "missing": profiles.missing_fields(profile),
            "has_guardian": await guardians.has_active_guardian(session, user.id),
        },
        session=session,
        user=user,
    )


@router.get("/me/edit")
async def edit(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = _own_profile(user)
    context = form_context(profile_values(profile), {}, profile.gender.value)
    return await render(request, "profile/edit.html", context, session=session, user=user)


def _saved_notice(profile: Profile, resubmitted: bool) -> str:
    if resubmitted and profile.status == ProfileStatus.PENDING:
        return "Изменения сохранены и отправлены на модерацию."
    if profile.status == ProfileStatus.HIDDEN:
        return "Изменения сохранены. Анкета скрыта — покажите её, когда будете готовы."
    return "Изменения сохранены."


@router.post("/me/edit")
async def edit_submit(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = _own_profile(user)
    values = form_values(await request.form())
    try:
        data = ProfileUpdate.from_form({name: values[name] for name in PROFILE_FIELDS if name != "gender"}, profile)
    except ValidationError as exc:
        context = form_context(values, humanize_errors(exc), profile.gender.value)
        return await render(request, "profile/edit.html", context, session=session, user=user, status_code=422)
    before, submitted_at = profile_values(profile), profile.submitted_at
    try:
        profile = await profiles.update_profile(session, user, data)
    except DomainError as exc:  # e.g. too many submissions: keep what the person typed
        flash(request, exc.message, "error")
        context = form_context(values, {}, profile.gender.value)
        return await render(request, "profile/edit.html", context, session=session, user=user, status_code=422)
    if profile_values(profile) == before:
        flash(request, "Изменений нет.", "info")
    else:
        flash(request, _saved_notice(profile, profile.submitted_at != submitted_at), "success")
    return _redirect("/me")


# --- Completing a profile migrated from the legacy bot -----------------------------------------


def _complete_context(profile: Profile, values: dict[str, str], errors: dict[str, str]) -> dict[str, object]:
    missing = profiles.missing_fields(profile)
    return form_context(values, errors, profile.gender.value) | {
        "profile": profile,
        "missing": missing,
        "field_labels": FIELD_LABELS,
    }


@router.get("/me/complete")
async def complete(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    """«Дополнить анкету»: only the required fields that are still empty (choices and numbers keep
    the profile published: no new moderation)."""
    profile = _own_profile(user)
    missing = profiles.missing_fields(profile)
    if not missing:
        flash(request, "Анкета заполнена полностью.", "info")
        return _redirect("/me")
    context = _complete_context(profile, dict.fromkeys(missing, ""), {})
    return await render(request, "profile/complete.html", context, session=session, user=user)


@router.post("/me/complete")
async def complete_submit(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    profile = _own_profile(user)
    missing = profiles.missing_fields(profile)
    if not missing:
        return _redirect("/me")
    values = form_values(await request.form(), tuple(missing))
    try:
        data = ProfileUpdate.model_validate(values)
    except ValidationError as exc:
        context = _complete_context(profile, values, humanize_errors(exc))
        return await render(request, "profile/complete.html", context, session=session, user=user, status_code=422)
    submitted_at = profile.submitted_at
    try:
        profile = await profiles.update_profile(session, user, data)
    except DomainError as exc:
        flash(request, exc.message, "error")
        context = _complete_context(profile, values, {})
        return await render(request, "profile/complete.html", context, session=session, user=user, status_code=422)
    resubmitted = profile.submitted_at != submitted_at
    flash(request, "Спасибо, анкета дополнена." + (" Она снова на модерации." if resubmitted else ""), "success")
    return _redirect("/me")


# --- Visibility and account -------------------------------------------------------------------


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
    """Allowed to everyone logged in, also banned people and those who did not give consent."""
    form = await request.form()
    if form.get("confirm") != "yes":
        flash(request, "Чтобы удалить аккаунт, отметьте подтверждение.", "error")
        back = form.get("back")
        return _redirect(back if isinstance(back, str) and back in DELETE_BACK_PAGES else "/settings")
    await users.delete_account(session, user)
    logout_user(request)
    flash(request, "Аккаунт и все ваши данные удалены.", "info")
    return _redirect("/")


# --- Viewing other profiles: /p/{number} -------------------------------------------------------


@router.get("/p/{number:int}")
async def view_profile(request: Request, session: SessionDep, user: ProfileUserDep, number: NumberPath) -> Response:
    return await _profile_page(request, session, user, number)


async def _profile_page(
    request: Request,
    session: SessionDep,
    user: User,
    number: int,
    *,
    interest_draft: str = "",
    interest_error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    try:
        profile = await profiles.get_viewable_profile_by_number(session, user, number)
    except ProfileUnderReview as exc:  # a counterpart's edited or rejected profile: say why
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc
    except ProfileNotActive as exc:  # the viewer's own profile is not published yet
        return await profile_gate(request, session, user, exc)
    except ProfileNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    is_own = profile.user_id == user.id
    relation = None if is_own else await interests.get_relation(session, user, profile.user_id)
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
            "interest_draft": interest_draft,
            "interest_error": interest_error,
        },
        session=session,
        user=user,
        status_code=status_code,
    )


async def _target(session: SessionDep, number: int) -> Profile:
    """The profile a form on /p/{number} acts on; the services check whether the user may."""
    profile = await profiles.get_profile_by_number(session, number)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return profile


@router.post("/p/{number:int}/interest")
async def send_interest(request: Request, session: SessionDep, user: ProfileUserDep, number: NumberPath) -> Response:
    target = await _target(session, number)
    form = await request.form()
    message = form.get("message")
    draft = message if isinstance(message, str) else None
    try:
        await interests.send_interest(session, user, target.user_id, draft)
    except ProfileNotFound as exc:  # the same answer as for a number that does not exist
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    except (InvalidInput, InterestLimitReached) as exc:  # the form stays: show the error, keep the text
        return await _profile_page(
            request,
            session,
            user,
            number,
            interest_draft=draft or "",
            interest_error=exc.message,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except DomainError as exc:  # the relation or one's own status changed: the page explains it
        flash(request, exc.message, "error")
    else:
        flash(request, f"Отклик отправлен: {profile_title(target)}. Мы сообщим, когда на него ответят.", "success")
    return _redirect(f"/p/{number}")


@router.post("/p/{number:int}/report")
async def report_profile(request: Request, session: SessionDep, user: ProfileUserDep, number: NumberPath) -> Response:
    target = await _target(session, number)
    if not await submit_report(request, session, user, target.user_id):
        return _redirect(f"/p/{number}")
    flash(
        request,
        f"Жалоба на анкету №{target.number} отправлена модератору. Эта анкета больше не будет видна вам в каталоге.",
        "success",
    )
    return _redirect("/catalog")
