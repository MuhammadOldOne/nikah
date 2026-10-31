"""Admin area (/admin): dashboard, moderation queue, reports, stop-word flags and users.

Access: the router requires an admin on every route (``require_admin`` -> ``users.is_admin``);
POST forms carry the CSRF token (checked app-wide by ``deps.verify_csrf``) and redirect back with a
flash notice. Business rules stay in the services; this module only reads data for the pages.

Privacy: phone numbers are never shown. Telegram ids and usernames appear only on the user pages,
where an admin needs them to find a person; elsewhere people are shown as "Имя (#id)".
Admin actions are logged with ids only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import age_from_birth_year, utcnow
from app.enums import Gender, InterestStatus, ProfileStatus, ReportAction, ReportStatus, label
from app.models import Conversation, Interest, Message, Profile, Report, User
from app.schemas import FIELD_LABELS, PROFILE_OPTIONAL_FIELDS, PROFILE_REQUIRED_FIELDS
from app.services import moderation, profiles, users
from app.services.errors import DomainError, ProfileNotFound, ProfileStateError
from app.services.stopwords import find_stop_words
from app.text import plural
from app.web.deps import AdminDep, SessionDep, flash, require_admin, safe_next_url
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

PER_PAGE = 20
SEARCH_LIMIT = 50
USER_REPORTS_LIMIT = 20
EXCERPT_LENGTH = 300
MAX_PAGE = 10_000

PageQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
NextField = Annotated[str | None, Form(alias="next")]

# (button title, note shown to the profile owner); same wording as the bot's moderation buttons.
REJECT_PRESETS: tuple[tuple[str, str], ...] = (
    ("Неполные данные", "Неполные данные: дополните анкету и сохраните её снова."),
    ("Нарушение правил", "Анкета нарушает правила сообщества."),
    ("Подозрение на фейк", "Анкета похожа на ненастоящую. Если это ошибка, исправьте анкету и сохраните её снова."),
)
STALE_PROFILE = "Анкета изменилась после того, как вы открыли страницу. Проверьте актуальную версию."

_ID_RE = re.compile(r"#\s*(\d{1,18})")
_NUMBER_RE = re.compile(r"\d{1,18}")
_USERNAME_RE = re.compile(r"@?([A-Za-z0-9_]{1,64})")

PROFILE_FIELDS = PROFILE_REQUIRED_FIELDS + PROFILE_OPTIONAL_FIELDS
_TEXT_FIELDS = frozenset({"name", "city", "occupation", "nationality", "wishes", "about"})
_WIDE_FIELDS = frozenset({"wishes", "about"})


class FlaggedKind(StrEnum):
    MESSAGES = "messages"
    INTERESTS = "interests"
    PROFILES = "profiles"


FLAGGED_TITLES: dict[FlaggedKind, str] = {
    FlaggedKind.MESSAGES: "Сообщения в чатах",
    FlaggedKind.INTERESTS: "Сообщения в откликах",
    FlaggedKind.PROFILES: "Анкеты",
}


# --- view helpers ---------------------------------------------------------------------------------


@dataclass(slots=True)
class Pager:
    page: int
    total: int
    path: str
    params: dict[str, str] = field(default_factory=dict)
    per_page: int = PER_PAGE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def url(self, page: int) -> str:
        return f"{self.path}?{urlencode({**self.params, 'page': page})}"


@dataclass(frozen=True, slots=True)
class ProfileRow:
    label: str
    value: Markup
    wide: bool


@dataclass(frozen=True, slots=True)
class AdminNav:
    pending_profiles: int
    open_reports: int


@dataclass(slots=True)
class DashboardStats:
    users_total: int
    users_banned: int
    profiles: dict[ProfileStatus, dict[Gender, int]]
    interests: dict[InterestStatus, int]
    open_conversations: int
    messages_24h: int
    open_reports: int
    flagged_messages: int

    def profiles_by_gender(self, gender: Gender) -> int:
        return sum(by_gender[gender] for by_gender in self.profiles.values())

    def profiles_by_status(self, status: ProfileStatus) -> int:
        return sum(self.profiles[status].values())

    @property
    def profiles_total(self) -> int:
        return sum(self.profiles_by_status(status) for status in ProfileStatus)


def _same_length_lower(text: str) -> str:
    """Lowercase + ё→е like ``find_stop_words``, keeping every character at its position."""
    return "".join(low if len(low := ch.lower()) == 1 else ch for ch in text).replace("ё", "е")


def highlight_stop_words(text: str | None) -> Markup:
    """Escaped ``text`` with every stop word found by the moderation filter wrapped in <mark>."""
    if not text:
        return Markup("")
    words = find_stop_words(text)
    if not words:
        return escape(text)
    haystack = _same_length_lower(text)
    spans: list[tuple[int, int]] = []
    for word in words:
        start = haystack.find(word)
        while start != -1:
            spans.append((start, start + len(word)))
            start = haystack.find(word, start + len(word))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:  # overlapping or adjacent matches form one mark
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[Markup] = []
    position = 0
    for start, end in merged:
        parts.append(escape(text[position:start]))
        parts.append(Markup("<mark>{}</mark>").format(text[start:end]))
        position = end
    parts.append(escape(text[position:]))
    return Markup("").join(parts)


def excerpt(text: str | None, limit: int = EXCERPT_LENGTH) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def profile_rows(profile: Profile) -> list[ProfileRow]:
    """All filled profile fields with Russian labels; free texts have stop words highlighted."""
    rows: list[ProfileRow] = []
    for name in PROFILE_FIELDS:
        value: Any = getattr(profile, name)
        if value is None or value == "":
            continue
        if isinstance(value, StrEnum):
            shown = escape(label(value, profile.gender))
        elif name == "birth_year":
            age = age_from_birth_year(value)
            shown = escape(f"{value} ({age} {plural(age, 'год', 'года', 'лет')})")
        elif name in _TEXT_FIELDS:
            shown = highlight_stop_words(value)
        else:
            shown = escape(str(value))
        rows.append(ProfileRow(FIELD_LABELS[name], shown, name in _WIDE_FIELDS))
    return rows


def profile_version(profile: Profile) -> str:
    """Identifies the submitted content a moderation card shows (stale-form protection)."""
    return _version(profile.submitted_at)


def _version(submitted_at: datetime | None) -> str:
    return submitted_at.isoformat() if submitted_at else ""


def _admin_url(value: str | None, default: str) -> str:
    """Local /admin URL to return to after a form (never an open redirect)."""
    url = safe_next_url(value, default)
    return url if url == "/admin" or url.startswith(("/admin/", "/admin?")) else default


def _back(next_url: str | None, default: str) -> RedirectResponse:
    return RedirectResponse(_admin_url(next_url, default), status_code=status.HTTP_303_SEE_OTHER)


async def _count(session: AsyncSession, *conditions: ColumnElement[bool], model: type[Any]) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def _nav(session: AsyncSession) -> AdminNav:
    return AdminNav(
        pending_profiles=await _count(session, Profile.status == ProfileStatus.PENDING, model=Profile),
        open_reports=await _count(session, Report.status == ReportStatus.OPEN, model=Report),
    )


async def _render(
    request: Request, session: AsyncSession, admin: User, name: str, context: Mapping[str, Any]
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        f"admin/{name}",
        {
            "admin": admin,
            "nav": await _nav(session),
            "highlight": highlight_stop_words,
            "is_admin": users.is_admin,
            "excerpt": excerpt,
            "profile_rows": profile_rows,
            "profile_version": profile_version,
            **context,
        },
    )


# --- dashboard ------------------------------------------------------------------------------------


async def dashboard_stats(session: AsyncSession) -> DashboardStats:
    users_total, users_banned = (
        await session.execute(select(func.count(), func.count().filter(User.is_banned)).select_from(User))
    ).one()
    by_status: dict[ProfileStatus, dict[Gender, int]] = {status: dict.fromkeys(Gender, 0) for status in ProfileStatus}
    for profile_status, gender, count in await session.execute(
        select(Profile.status, Profile.gender, func.count()).group_by(Profile.status, Profile.gender)
    ):
        by_status[profile_status][gender] = count
    interests_by_status = dict.fromkeys(InterestStatus, 0)
    for interest_status, count in await session.execute(
        select(Interest.status, func.count()).group_by(Interest.status)
    ):
        interests_by_status[interest_status] = count
    return DashboardStats(
        users_total=users_total,
        users_banned=users_banned,
        profiles=by_status,
        interests=interests_by_status,
        open_conversations=await _count(session, Conversation.closed_at.is_(None), model=Conversation),
        messages_24h=await _count(session, Message.created_at >= utcnow() - timedelta(hours=24), model=Message),
        open_reports=await _count(session, Report.status == ReportStatus.OPEN, model=Report),
        flagged_messages=await _count(session, Message.flagged.is_(True), model=Message),
    )


@router.get("")
async def dashboard(request: Request, session: SessionDep, admin: AdminDep) -> HTMLResponse:
    return await _render(request, session, admin, "dashboard.html", {"stats": await dashboard_stats(session)})


# --- moderation queue -----------------------------------------------------------------------------


@router.get("/moderation")
async def moderation_queue(request: Request, session: SessionDep, admin: AdminDep, page: PageQuery = 1) -> HTMLResponse:
    total = await _count(session, Profile.status == ProfileStatus.PENDING, model=Profile)
    pager = Pager(page, total, "/admin/moderation")
    items = await profiles.list_pending(session, limit=pager.per_page, offset=pager.offset)
    return await _render(
        request,
        session,
        admin,
        "moderation.html",
        {"profiles": items, "pager": pager, "reject_presets": REJECT_PRESETS},
    )


async def _lock_unchanged(session: AsyncSession, user_id: int, version: str) -> None:
    """Lock the profile row and make sure it still has the content the moderator saw."""
    row = (
        await session.execute(select(Profile.submitted_at).where(Profile.user_id == user_id).with_for_update())
    ).first()
    if row is None:
        raise ProfileNotFound()
    if _version(row.submitted_at) != version:
        raise ProfileStateError(STALE_PROFILE)


@router.post("/moderation/{user_id:int}/approve")
async def approve_profile(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    user_id: int,
    version: Annotated[str, Form(max_length=64)] = "",
    next_url: NextField = None,
) -> RedirectResponse:
    try:
        await _lock_unchanged(session, user_id, version)
        await profiles.approve(session, user_id)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s approved profile %s", admin.id, user_id)
        flash(request, f"Анкета #{user_id} опубликована.", "success")
    return _back(next_url, "/admin/moderation")


@router.post("/moderation/{user_id:int}/reject")
async def reject_profile(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    user_id: int,
    version: Annotated[str, Form(max_length=64)] = "",
    preset: Annotated[str, Form(max_length=8)] = "",
    note: Annotated[str, Form(max_length=4000)] = "",
    next_url: NextField = None,
) -> RedirectResponse:
    reason = note.strip()
    if not reason and preset.isdigit() and int(preset) < len(REJECT_PRESETS):
        reason = REJECT_PRESETS[int(preset)][1]
    try:
        await _lock_unchanged(session, user_id, version)
        await profiles.reject(session, user_id, reason)
    except DomainError as exc:
        flash(request, f"Анкета #{user_id}: {exc.message}", "error")
    else:
        logger.info("admin %s rejected profile %s", admin.id, user_id)
        flash(request, f"Анкета #{user_id} отклонена. Автор получит причину.", "success")
    return _back(next_url, "/admin/moderation")


# --- reports --------------------------------------------------------------------------------------


@router.get("/reports")
async def reports_page(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    status_filter: Annotated[ReportStatus, Query(alias="status")] = ReportStatus.OPEN,
    page: PageQuery = 1,
) -> HTMLResponse:
    total = await _count(session, Report.status == status_filter, model=Report)
    pager = Pager(page, total, "/admin/reports", {"status": status_filter.value})
    items = await moderation.list_reports(session, status_filter, limit=pager.per_page, offset=pager.offset)
    return await _render(
        request,
        session,
        admin,
        "reports.html",
        {"reports": items, "pager": pager, "status": status_filter, "actions": ReportAction},
    )


@router.post("/reports/{report_id:int}/resolve")
async def resolve_report(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    report_id: int,
    action: Annotated[ReportAction, Form()],
    next_url: NextField = None,
) -> RedirectResponse:
    try:
        await moderation.resolve_report(session, report_id, admin, action)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s resolved report %s: %s", admin.id, report_id, action.value)
        flash(request, f"Жалоба #{report_id} рассмотрена: {label(action)}.", "success")
    return _back(next_url, "/admin/reports")


# --- stop-word flags ------------------------------------------------------------------------------


def _flagged_url(kind: FlaggedKind) -> tuple[str, dict[str, str]]:
    return "/admin/flagged", {"kind": kind.value}


@router.get("/flagged")
async def flagged_page(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    kind: FlaggedKind = FlaggedKind.MESSAGES,
    page: PageQuery = 1,
) -> HTMLResponse:
    items: list[Any]
    if kind == FlaggedKind.MESSAGES:
        pager = Pager(page, await _count(session, Message.flagged.is_(True), model=Message), *_flagged_url(kind))
        items = await moderation.list_flagged_messages(session, limit=pager.per_page, offset=pager.offset)
    elif kind == FlaggedKind.INTERESTS:
        pager = Pager(page, await _count(session, Interest.flagged.is_(True), model=Interest), *_flagged_url(kind))
        result = await session.scalars(
            select(Interest)
            .where(Interest.flagged.is_(True))
            .options(
                selectinload(Interest.from_user).selectinload(User.profile),
                selectinload(Interest.to_user).selectinload(User.profile),
            )
            .order_by(Interest.id.desc())
            .limit(pager.per_page)
            .offset(pager.offset)
        )
        items = list(result.all())
    else:
        pager = Pager(page, await _count(session, Profile.flagged.is_(True), model=Profile), *_flagged_url(kind))
        result = await session.scalars(
            select(Profile)
            .where(Profile.flagged.is_(True))
            .options(selectinload(Profile.user))
            .order_by(Profile.updated_at.desc(), Profile.user_id.desc())
            .limit(pager.per_page)
            .offset(pager.offset)
        )
        items = list(result.all())
    return await _render(
        request,
        session,
        admin,
        "flagged.html",
        {"kind": kind, "kinds": FLAGGED_TITLES, "items": items, "pager": pager},
    )


# --- users ----------------------------------------------------------------------------------------


def search_condition(query: str) -> ColumnElement[bool] | None:
    """``#id`` -> user id; digits -> user id or Telegram id; ``@name``/``name`` -> username prefix."""
    query = query.strip()
    if match := _ID_RE.fullmatch(query):
        return User.id == int(match[1])
    if _NUMBER_RE.fullmatch(query):
        number = int(query)
        return or_(User.id == number, User.telegram_id == number)
    if match := _USERNAME_RE.fullmatch(query):
        pattern = match[1].lower().replace("_", "\\_") + "%"
        return func.lower(User.username).like(pattern, escape="\\")
    return None


@router.get("/users")
async def users_page(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    q: Annotated[str, Query(max_length=100)] = "",
) -> HTMLResponse:
    condition = search_condition(q) if q.strip() else None
    found: list[User] = []
    if condition is not None:
        result = await session.scalars(
            select(User).where(condition).options(selectinload(User.profile)).order_by(User.id).limit(SEARCH_LIMIT)
        )
        found = list(result.all())
    return await _render(
        request,
        session,
        admin,
        "users.html",
        {"q": q, "users": found, "invalid_query": bool(q.strip()) and condition is None, "limit": SEARCH_LIMIT},
    )


async def _get_user_or_404(session: AsyncSession, user_id: int) -> User:
    user = await users.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден.")
    return user


@router.get("/users/{user_id:int}")
async def user_page(request: Request, session: SessionDep, admin: AdminDep, user_id: int) -> HTMLResponse:
    user = await _get_user_or_404(session, user_id)
    result = await session.scalars(
        select(Report)
        .where(Report.target_user_id == user_id)
        .options(selectinload(Report.reporter).selectinload(User.profile), selectinload(Report.message))
        .order_by(Report.id.desc())
        .limit(USER_REPORTS_LIMIT)
    )
    return await _render(
        request,
        session,
        admin,
        "user.html",
        {
            "user": user,
            "is_target_admin": users.is_admin(user),
            "reports": list(result.all()),
            "reports_total": await _count(session, Report.target_user_id == user_id, model=Report),
            "reports_filed": await _count(session, Report.reporter_id == user_id, model=Report),
            "flagged_messages": await _count(
                session, Message.sender_id == user_id, Message.flagged.is_(True), model=Message
            ),
        },
    )


@router.post("/users/{user_id:int}/ban")
async def ban_user(
    request: Request, session: SessionDep, admin: AdminDep, user_id: int, next_url: NextField = None
) -> RedirectResponse:
    user = await _get_user_or_404(session, user_id)
    if user.is_banned:
        flash(request, f"Пользователь #{user_id} уже заблокирован.", "info")
        return _back(next_url, f"/admin/users/{user_id}")
    try:
        await users.ban(session, user)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s banned user %s", admin.id, user_id)
        flash(request, f"Пользователь #{user_id} заблокирован: анкета скрыта, чаты закрыты.", "success")
    return _back(next_url, f"/admin/users/{user_id}")


@router.post("/users/{user_id:int}/unban")
async def unban_user(
    request: Request, session: SessionDep, admin: AdminDep, user_id: int, next_url: NextField = None
) -> RedirectResponse:
    user = await _get_user_or_404(session, user_id)
    if not user.is_banned:
        flash(request, f"Пользователь #{user_id} не заблокирован.", "info")
    else:
        await users.unban(session, user)
        logger.info("admin %s unbanned user %s", admin.id, user_id)
        flash(
            request, f"Пользователь #{user_id} разблокирован. Закрытые чаты и отклики не восстанавливаются.", "success"
        )
    return _back(next_url, f"/admin/users/{user_id}")
