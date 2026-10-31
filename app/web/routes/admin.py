"""Admin area (/admin): dashboard (with the legacy import status), moderation queue, reports,
flagged content (contact filter, stop words, witness, reports), witness messages, chats and users.

Access: the router requires an admin on every route (``require_admin`` -> ``users.is_admin``);
POST forms carry the CSRF token (checked app-wide by ``deps.verify_csrf``) and redirect back with a
flash notice. Business rules stay in the services; this module only reads data for the pages.

Privacy: people are shown by profile number («Анкета №512»), as in the bot; internal user ids
appear only in admin URLs. Phone numbers are never shown. Telegram ids and usernames appear only
on the user pages and in the search results, where an admin needs them to find a person.
Admin actions are logged with ids only. Admins see chat histories because they are the witness
(свидетель) of every conversation: they already receive a copy of each message in Telegram.
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
from sqlalchemy import ColumnElement, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import age_from_birth_year, utcnow
from app.enums import Gender, InterestStatus, MessageKind, ProfileStatus, ReportAction, ReportStatus, label
from app.models import CONTACT_FILTER_REASON, Conversation, Interest, Message, Profile, Report, User
from app.schemas import FIELD_LABELS, PROFILE_FREE_TEXT_FIELDS, PROFILE_OPTIONAL_FIELDS, PROFILE_REQUIRED_FIELDS
from app.services import chat, meta, moderation, profiles, users
from app.services.errors import DomainError, ProfileNotFound, ProfileStateError
from app.services.moderation import LEGACY_AGE_FLAG_REASON, REPORT_FLAG_REASON
from app.services.stopwords import find_stop_words
from app.services.witness import WITNESS_FLAG_REASON
from app.text import plural, profile_title
from app.web.deps import AdminDep, IdPath, SessionDep, flash, require_admin, safe_next_url
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

PER_PAGE = 20
SEARCH_LIMIT = 50
USER_REPORTS_LIMIT = 20
USER_CHATS_LIMIT = 20
CHAT_PAGE_SIZE = 100
EXCERPT_LENGTH = 300
MAX_PAGE = 10_000
TELEGRAM_ID_MAX = 2**63 - 1  # users.telegram_id is BIGINT
PROFILE_NUMBER_MAX = 2**31 - 1  # profiles.number is INTEGER

PageQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
BeforeQuery = Annotated[int | None, Query(alias="before", ge=1, le=2**63 - 1)]
NextField = Annotated[str | None, Form(alias="next")]

# (button title, note shown to the profile owner); same wording as the bot's moderation buttons.
REJECT_PRESETS: tuple[tuple[str, str], ...] = (
    ("Неполные данные", "Неполные данные: дополните анкету и сохраните её снова."),
    ("Нарушение правил", "Анкета нарушает правила сообщества."),
    ("Подозрение на фейк", "Анкета похожа на ненастоящую. Если это ошибка, исправьте анкету и сохраните её снова."),
)
STALE_PROFILE = "Анкета изменилась после того, как вы открыли страницу. Проверьте актуальную версию."

# «№512» / «#512»: a profile number (as in the bot's /ban); bare digits: Telegram id or number.
_PROFILE_NUMBER_RE = re.compile(r"[№#]\s*(\d{1,10})")
_DIGITS_RE = re.compile(r"\d{1,19}")
_USERNAME_RE = re.compile(r"@?([A-Za-z0-9_]{1,64})")

PROFILE_FIELDS = PROFILE_REQUIRED_FIELDS + PROFILE_OPTIONAL_FIELDS
_TEXT_FIELDS = frozenset(PROFILE_FREE_TEXT_FIELDS)
_WIDE_FIELDS = frozenset({"wishes", "about"})


class FlaggedKind(StrEnum):
    MESSAGES = "messages"
    WITNESS = "witness"
    INTERESTS = "interests"
    PROFILES = "profiles"


FLAGGED_TITLES: dict[FlaggedKind, str] = {
    FlaggedKind.MESSAGES: "Сообщения в чатах",
    FlaggedKind.WITNESS: "Сообщения свидетеля",
    FlaggedKind.INTERESTS: "Сообщения в откликах",
    FlaggedKind.PROFILES: "Анкеты",
}

# Codes in ``flag_reason`` columns (anything else there is a stop word found by the scam filter).
FLAG_REASON_LABELS: dict[str, str] = {
    CONTACT_FILTER_REASON: "фильтр контактов",
    REPORT_FLAG_REASON: "жалоба",
    WITNESS_FLAG_REASON: "свидетель",
    LEGACY_AGE_FLAG_REASON: "возраст при переносе",
}


# Tables of the legacy import marker, in the order of the import report.
LEGACY_TABLE_LABELS: dict[str, str] = {
    "users": "Пользователи",
    "profiles": "Анкеты",
    "interests": "Отклики",
    "conversations": "Чаты",
    "messages": "Сообщения",
}


class ChatsStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


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
class FlagReasons:
    """A ``flag_reason`` split for display: known codes as Russian labels, the rest stop words."""

    labels: list[str]
    stop_words: list[str]


@dataclass(frozen=True, slots=True)
class AdminNav:
    pending_profiles: int
    open_reports: int


@dataclass(frozen=True, slots=True)
class LegacyImportStatus:
    """The ``legacy_import`` marker (services.meta) next to today's numbers, for the cutover check."""

    imported_at: datetime | None
    counts: dict[str, int]
    users_without_consent: int
    incomplete_profiles: int


@dataclass(frozen=True, slots=True)
class ChatRow:
    conversation: Conversation
    messages: int
    flagged: int


@dataclass(slots=True)
class DashboardStats:
    users_total: int
    users_banned: int
    profiles: dict[ProfileStatus, dict[Gender, int]]
    interests: dict[InterestStatus, int]
    conversations_total: int
    open_conversations: int
    messages_total: int
    messages_24h: int
    open_reports: int
    flagged_messages: int

    def current(self, table: str) -> int | None:
        """Today's row count of a table named in the legacy import marker."""
        return {
            "users": self.users_total,
            "profiles": self.profiles_total,
            "interests": sum(self.interests.values()),
            "conversations": self.conversations_total,
            "messages": self.messages_total,
        }.get(table)

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


def flag_reasons(reason: str | None) -> FlagReasons:
    parts = [part.strip() for part in (reason or "").split(",") if part.strip()]
    return FlagReasons(
        labels=[FLAG_REASON_LABELS[part] for part in parts if part in FLAG_REASON_LABELS],
        stop_words=[part for part in parts if part not in FLAG_REASON_LABELS],
    )


def user_title(user: User | None) -> str:
    """How the admin pages name a person: by profile number, like the bot («Анкета №512»)."""
    if user is None:
        return "аккаунт удалён"
    return profile_title(user.profile) if user.profile else "аккаунт без анкеты"


def excerpt(text: str | None, limit: int = EXCERPT_LENGTH) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def profile_rows(profile: Profile) -> list[ProfileRow]:
    """Filled profile fields with Russian labels (free texts have stop words highlighted); empty
    required fields (profiles migrated from the legacy bot) show as «не указано»."""
    rows: list[ProfileRow] = []
    for name in PROFILE_FIELDS:
        value: Any = getattr(profile, name)
        if value is None or value == "":
            if name in PROFILE_REQUIRED_FIELDS:
                rows.append(ProfileRow(FIELD_LABELS[name], Markup('<span class="adm-muted">не указано</span>'), False))
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
            "flag_reasons": flag_reasons,
            "user_title": user_title,
            "profile_rows": profile_rows,
            "profile_version": profile_version,
            "reject_presets": REJECT_PRESETS,
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
        conversations_total=await _count(session, true(), model=Conversation),
        open_conversations=await _count(session, Conversation.closed_at.is_(None), model=Conversation),
        messages_total=await _count(session, true(), model=Message),
        messages_24h=await _count(session, Message.created_at >= utcnow() - timedelta(hours=24), model=Message),
        open_reports=await _count(session, Report.status == ReportStatus.OPEN, model=Report),
        flagged_messages=await _count(session, Message.flagged.is_(True), model=Message),
    )


async def legacy_import_status(session: AsyncSession) -> LegacyImportStatus | None:
    """None until scripts/import_legacy.py --commit has run."""
    marker = await meta.legacy_import_marker(session)
    if marker is None:
        return None
    try:
        imported_at: datetime | None = datetime.fromisoformat(str(marker.get("imported_at")))
    except ValueError:
        imported_at = None
    counts = {str(table): count for table, count in (marker.get("counts") or {}).items()}
    incomplete = or_(*(getattr(Profile, name).is_(None) for name in PROFILE_REQUIRED_FIELDS))
    return LegacyImportStatus(
        imported_at=imported_at,
        # JSONB does not keep key order: show the tables in the report's order.
        counts={
            table: int(counts[table])
            for table in (*LEGACY_TABLE_LABELS, *sorted(set(counts) - set(LEGACY_TABLE_LABELS)))
            if table in counts
        },
        users_without_consent=await _count(session, User.consent_at.is_(None), model=User),
        incomplete_profiles=await _count(session, incomplete, model=Profile),
    )


@router.get("")
async def dashboard(request: Request, session: SessionDep, admin: AdminDep) -> HTMLResponse:
    context = {
        "stats": await dashboard_stats(session),
        "legacy": await legacy_import_status(session),
        "legacy_tables": LEGACY_TABLE_LABELS,
    }
    return await _render(request, session, admin, "dashboard.html", context)


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
        {"profiles": items, "pager": pager},
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
    user_id: IdPath,
    version: Annotated[str, Form(max_length=64)] = "",
    next_url: NextField = None,
) -> RedirectResponse:
    try:
        await _lock_unchanged(session, user_id, version)
        profile = await profiles.approve(session, user_id)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s approved profile %s", admin.id, user_id)
        flash(request, f"{profile_title(profile)} опубликована.", "success")
    return _back(next_url, "/admin/moderation")


@router.post("/moderation/{user_id:int}/reject")
async def reject_profile(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    user_id: IdPath,
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
        profile = await profiles.reject(session, user_id, reason)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s rejected profile %s", admin.id, user_id)
        flash(request, f"{profile_title(profile)} отклонена. Автор получит причину.", "success")
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
    report_id: IdPath,
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


# --- flagged content and witness messages --------------------------------------------------------


def _with_conversation() -> tuple[Any, ...]:
    """Load a message's sender and both participants of its conversation (with profiles)."""
    conversation = selectinload(Message.conversation)
    return (
        selectinload(Message.sender).selectinload(User.profile),
        conversation.selectinload(Conversation.user_a).selectinload(User.profile),
        conversation.selectinload(Conversation.user_b).selectinload(User.profile),
    )


async def _messages_page(session: AsyncSession, condition: ColumnElement[bool], pager: Pager) -> list[Message]:
    result = await session.scalars(
        select(Message)
        .where(condition)
        .options(*_with_conversation())
        .order_by(Message.id.desc())
        .limit(pager.per_page)
        .offset(pager.offset)
    )
    return list(result.all())


@router.get("/flagged")
async def flagged_page(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    kind: FlaggedKind = FlaggedKind.MESSAGES,
    page: PageQuery = 1,
) -> HTMLResponse:
    items: list[Any]
    params = {"kind": kind.value}
    if kind in (FlaggedKind.MESSAGES, FlaggedKind.WITNESS):
        condition = Message.flagged.is_(True) if kind == FlaggedKind.MESSAGES else Message.kind == MessageKind.WITNESS
        pager = Pager(page, await _count(session, condition, model=Message), "/admin/flagged", params)
        items = await _messages_page(session, condition, pager)
    elif kind == FlaggedKind.INTERESTS:
        pager = Pager(page, await _count(session, Interest.flagged.is_(True), model=Interest), "/admin/flagged", params)
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
        pager = Pager(page, await _count(session, Profile.flagged.is_(True), model=Profile), "/admin/flagged", params)
        result = await session.scalars(
            select(Profile)
            .where(Profile.flagged.is_(True))
            .options(selectinload(Profile.user).selectinload(User.profile))
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


# --- chats (the witness's view) -------------------------------------------------------------------


def _with_participants() -> tuple[Any, ...]:
    return (
        selectinload(Conversation.user_a).selectinload(User.profile),
        selectinload(Conversation.user_b).selectinload(User.profile),
    )


async def _chat_rows(session: AsyncSession, conversations: list[Conversation]) -> list[ChatRow]:
    ids = [conversation.id for conversation in conversations]
    counts: dict[int, tuple[int, int]] = {}
    if ids:
        for conversation_id, total, flagged in await session.execute(
            select(Message.conversation_id, func.count(), func.count().filter(Message.flagged))
            .where(Message.conversation_id.in_(ids))
            .group_by(Message.conversation_id)
        ):
            counts[conversation_id] = (total, flagged)
    return [ChatRow(conversation, *counts.get(conversation.id, (0, 0))) for conversation in conversations]


@router.get("/chats")
async def chats_page(
    request: Request,
    session: SessionDep,
    admin: AdminDep,
    status_filter: Annotated[ChatsStatus, Query(alias="status")] = ChatsStatus.OPEN,
    page: PageQuery = 1,
) -> HTMLResponse:
    condition = (
        Conversation.closed_at.is_(None) if status_filter == ChatsStatus.OPEN else Conversation.closed_at.is_not(None)
    )
    pager = Pager(
        page, await _count(session, condition, model=Conversation), "/admin/chats", {"status": status_filter.value}
    )
    result = await session.scalars(
        select(Conversation)
        .where(condition)
        .options(*_with_participants())
        .order_by(func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(), Conversation.id.desc())
        .limit(pager.per_page)
        .offset(pager.offset)
    )
    rows = await _chat_rows(session, list(result.all()))
    return await _render(request, session, admin, "chats.html", {"rows": rows, "pager": pager, "status": status_filter})


@router.get("/chats/{conversation_id:int}")
async def chat_page(
    request: Request, session: SessionDep, admin: AdminDep, conversation_id: IdPath, before: BeforeQuery = None
) -> HTMLResponse:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id).options(*_with_participants())
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден.")
    messages = await chat.list_messages(session, conversation, before_id=before, limit=CHAT_PAGE_SIZE)
    earlier = bool(messages) and bool(
        await _count(session, Message.conversation_id == conversation_id, Message.id < messages[0].id, model=Message)
    )
    senders = {conversation.user_a_id: conversation.user_a, conversation.user_b_id: conversation.user_b}
    return await _render(
        request,
        session,
        admin,
        "chat.html",
        {
            "conversation": conversation,
            "messages": messages,
            "senders": senders,
            "earlier": earlier,
            "before": before,
            "total": await _count(session, Message.conversation_id == conversation_id, model=Message),
        },
    )


# --- users ----------------------------------------------------------------------------------------


def search_condition(query: str) -> ColumnElement[bool] | None:
    """For a query on users outer-joined with profiles: «№512» / «#512» -> profile number (as in the
    bot's /ban); digits -> Telegram id or profile number; ``@name`` / ``name`` -> username prefix.
    None for anything else (and for numbers no column can hold)."""
    query = query.strip()
    if match := _PROFILE_NUMBER_RE.fullmatch(query):
        number = int(match[1])
        return Profile.number == number if number <= PROFILE_NUMBER_MAX else None
    if _DIGITS_RE.fullmatch(query):
        value = int(query)
        conditions = [User.telegram_id == value] if value <= TELEGRAM_ID_MAX else []
        if value <= PROFILE_NUMBER_MAX:
            conditions.append(Profile.number == value)
        return or_(*conditions) if conditions else None
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
            select(User)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(condition)
            .options(selectinload(User.profile))
            .order_by(Profile.number.nulls_last(), User.id)
            .limit(SEARCH_LIMIT)
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
async def user_page(request: Request, session: SessionDep, admin: AdminDep, user_id: IdPath) -> HTMLResponse:
    user = await _get_user_or_404(session, user_id)
    result = await session.scalars(
        select(Report)
        .where(Report.target_user_id == user_id)
        .options(selectinload(Report.reporter).selectinload(User.profile), selectinload(Report.message))
        .order_by(Report.id.desc())
        .limit(USER_REPORTS_LIMIT)
    )
    conversations = await session.scalars(
        select(Conversation)
        .where(or_(Conversation.user_a_id == user_id, Conversation.user_b_id == user_id))
        .options(*_with_participants())
        .order_by(Conversation.id.desc())
        .limit(USER_CHATS_LIMIT)
    )
    return await _render(
        request,
        session,
        admin,
        "user.html",
        {
            "user": user,
            "is_target_admin": users.is_admin(user),
            "chats": await _chat_rows(session, list(conversations.all())),
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
    request: Request, session: SessionDep, admin: AdminDep, user_id: IdPath, next_url: NextField = None
) -> RedirectResponse:
    user = await _get_user_or_404(session, user_id)
    title = user_title(user).capitalize()
    if user.is_banned:
        flash(request, f"{title}: пользователь уже заблокирован.", "info")
        return _back(next_url, f"/admin/users/{user_id}")
    try:
        await users.ban(session, user)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        logger.info("admin %s banned user %s", admin.id, user_id)
        flash(request, f"{title}: пользователь заблокирован, анкета скрыта, чаты закрыты.", "success")
    return _back(next_url, f"/admin/users/{user_id}")


@router.post("/users/{user_id:int}/unban")
async def unban_user(
    request: Request, session: SessionDep, admin: AdminDep, user_id: IdPath, next_url: NextField = None
) -> RedirectResponse:
    user = await _get_user_or_404(session, user_id)
    title = user_title(user).capitalize()
    if not user.is_banned:
        flash(request, f"{title}: пользователь не заблокирован.", "info")
    else:
        await users.unban(session, user)
        logger.info("admin %s unbanned user %s", admin.id, user_id)
        flash(request, f"{title}: пользователь разблокирован. Закрытые чаты и отклики не восстанавливаются.", "success")
    return _back(next_url, f"/admin/users/{user_id}")
