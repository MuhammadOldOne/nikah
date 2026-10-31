#!/usr/bin/env python3
"""Database of the legacy bot @nikah_istina_bot: safe schema inspection and the one-off import.

--inspect needs only the Python standard library for SQLite (plus asyncpg for PostgreSQL), so this
file can also be copied to another server and run there on its own (Python 3.8+):

    python3 import_legacy.py --inspect /path/to/data.db

It opens the database READ-ONLY and prints ONLY the schema (tables, columns, types, keys) and row
counts, never row contents, so the output can be shared safely.

--import runs inside the project (it needs the ``app`` package and DATABASE_URL, e.g. in the web
container) and moves a COPY of the legacy SQLite ``data.db`` into the new database as
docs/LEGACY_BOT.md §4 prescribes: ids and profile numbers are kept, everything happens in ONE
transaction, and it is a dry run (rolled back) unless --commit is given. It refuses a target that
already has profiles, interests, conversations or messages, and a second import. It prints counters
and warnings only. See docs/LEGACY_IMPORT.md.
"""

from __future__ import annotations

import abc
import argparse
import asyncio
import itertools
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SQLITE_HEADER = b"SQLite format 3\x00"
_POSTGRES_URL = re.compile(r"^postgres(?:ql)?(?:\+\w+)?://", re.IGNORECASE)


class SourceError(Exception):
    """The legacy database cannot be opened or read (Russian message)."""


# --- read-only access to the legacy database ------------------------------------------------------


@dataclass
class Column:
    name: str
    type: str
    not_null: bool
    primary_key: bool


@dataclass
class Table:
    name: str
    rows: int
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[str] = field(default_factory=list)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class LegacySource(abc.ABC):
    """Read-only connection to the legacy database."""

    kind: str

    @abc.abstractmethod
    async def tables(self) -> list[Table]:
        """Schema and row counts of all user tables."""

    @abc.abstractmethod
    async def close(self) -> None: ...


class SqliteSource(LegacySource):
    kind = "SQLite"

    def __init__(self, path: str) -> None:
        try:
            with open(path, "rb") as file:
                header = file.read(len(SQLITE_HEADER))
        except OSError as exc:
            raise SourceError(f"не удалось открыть файл {path}: {exc.strerror}") from exc
        if header != SQLITE_HEADER:
            raise SourceError(
                f"{path} — не база SQLite. Поддерживаются SQLite и PostgreSQL (postgresql://...). "
                "Если у старого бота другая база, напишите разработчику, какая именно."
            )
        uri = "file:" + quote(os.path.abspath(path))
        try:
            self.connection = self._connect(uri + "?mode=ro")
        except sqlite3.OperationalError:
            # A WAL database in a read-only directory cannot be opened with mode=ro alone.
            self.connection = self._connect(uri + "?mode=ro&immutable=1")
        self.connection.execute("PRAGMA query_only = ON")
        self.connection.row_factory = sqlite3.Row

    @staticmethod
    def _connect(uri: str) -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    async def tables(self) -> list[Table]:
        names = [
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                "ORDER BY name"
            )
        ]
        result: list[Table] = []
        for name in names:
            quoted = _quote_ident(name)
            columns = [
                Column(row["name"], row["type"] or "?", bool(row["notnull"]), bool(row["pk"]))
                for row in self.connection.execute(f"PRAGMA table_info({quoted})")
            ]
            foreign_keys = [
                f"{row['from']} -> {row['table']}.{row['to'] or '?'}"
                for row in self.connection.execute(f"PRAGMA foreign_key_list({quoted})")
            ]
            rows = self.connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
            result.append(Table(name, rows, columns, foreign_keys))
        return result

    def column_names(self, table: str) -> set[str] | None:
        """Column names of ``table``; None if there is no such table."""
        exists = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        if exists.fetchone() is None:
            return None
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({_quote_ident(table)})")}

    def select(self, table: str, columns: Iterable[str], order_by: str) -> list[sqlite3.Row]:
        names = ", ".join(_quote_ident(column) for column in columns)
        return self.connection.execute(
            f"SELECT {names} FROM {_quote_ident(table)} ORDER BY {_quote_ident(order_by)}"
        ).fetchall()

    def count(self, table: str) -> int:
        return self.connection.execute(f"SELECT count(*) FROM {_quote_ident(table)}").fetchone()[0]

    async def close(self) -> None:
        self.connection.close()


_PG_TABLES = """
    SELECT c.oid, n.nspname AS schema, c.relname AS name
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_%'
    ORDER BY n.nspname, c.relname
"""
_PG_COLUMNS = """
    SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
           EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = a.attrelid AND i.indisprimary
                   AND a.attnum = ANY (i.indkey)) AS primary_key
    FROM pg_attribute a
    WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""
_PG_FOREIGN_KEYS = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = $1 AND contype = 'f'"


class PostgresSource(LegacySource):
    kind = "PostgreSQL"

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    async def connect(cls, url: str) -> PostgresSource:
        try:
            import asyncpg
        except ImportError as exc:
            raise SourceError(
                "для PostgreSQL нужен пакет asyncpg: pip install asyncpg (или запустите скрипт в контейнере web)"
            ) from exc
        try:
            connection = await asyncpg.connect(
                _POSTGRES_URL.sub("postgresql://", url, count=1),
                server_settings={"default_transaction_read_only": "on"},
            )
        # asyncio.TimeoutError is not the builtin TimeoutError before Python 3.11 (old servers).
        except (OSError, ValueError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:  # noqa: UP041
            raise SourceError(f"не удалось подключиться к PostgreSQL ({type(exc).__name__}: {exc})") from exc
        return cls(connection)

    async def tables(self) -> list[Table]:
        result: list[Table] = []
        for table in await self.connection.fetch(_PG_TABLES):
            name = table["name"] if table["schema"] == "public" else f"{table['schema']}.{table['name']}"
            columns = [
                Column(row["name"], row["type"], row["not_null"], row["primary_key"])
                for row in await self.connection.fetch(_PG_COLUMNS, table["oid"])
            ]
            foreign_keys = [row[0] for row in await self.connection.fetch(_PG_FOREIGN_KEYS, table["oid"])]
            rows = await self.connection.fetchval(
                f"SELECT count(*) FROM {_quote_ident(table['schema'])}.{_quote_ident(table['name'])}"
            )
            result.append(Table(name, rows, columns, foreign_keys))
        return result

    async def close(self) -> None:
        await self.connection.close()


async def open_source(location: str) -> LegacySource:
    """``postgresql://...`` URL or a path to an SQLite file; always opened read-only."""
    if _POSTGRES_URL.match(location):
        return await PostgresSource.connect(location)
    try:
        return SqliteSource(location)
    except sqlite3.Error as exc:
        raise SourceError(f"не удалось открыть базу SQLite: {exc}") from exc


def format_schema(kind: str, tables: list[Table]) -> str:
    """Human-readable schema report: names, types, keys and row counts only."""
    lines = [
        f"Источник: {kind}",
        f"Таблиц: {len(tables)}",
        "Показаны только структура и количество строк. Содержимое строк не выводится.",
        "",
    ]
    for table in tables:
        lines.append(f"Таблица {table.name} (строк: {table.rows})")
        width = max((len(column.name) for column in table.columns), default=0)
        for column in table.columns:
            flags = "PK" if column.primary_key else ("NOT NULL" if column.not_null else "")
            lines.append(f"  {column.name.ljust(width)}  {column.type}  {flags}".rstrip())
        lines.extend(f"  связь: {foreign_key}" for foreign_key in table.foreign_keys)
        lines.append("")
    return "\n".join(lines)


async def inspect(location: str) -> str:
    source = await open_source(location)
    try:
        return format_schema(source.kind, await source.tables())
    finally:
        await source.close()


# --- import --------------------------------------------------------------------------------------------


# Tables and columns of the legacy database (the legacy bot's db.py; tests/fixtures/legacy_schema.sql)
# that the import reads. Other columns (tg_message_id_*, muted.until, ...) are not needed.
LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("user_id", "tg_username", "role", "banned", "created_at"),
    "profiles": (
        "id",
        "user_id",
        "gender",
        "age",
        "city",
        "nationality",
        "about",
        "partner_wishes",
        "status",
        "created_at",
        "updated_at",
    ),
    "interests": ("id", "from_profile_id", "to_profile_id", "status", "created_at"),
    "chats": ("id", "profile_a_id", "profile_b_id", "opened_at", "closed_at"),
    "messages": ("id", "chat_id", "from_profile_id", "text", "sent_at", "flagged", "witness_note"),
    "muted": ("user_id",),
}
# Legacy table -> table of the new database (report and the app_meta marker).
TARGET_TABLES: dict[str, str] = {
    "users": "users",
    "profiles": "profiles",
    "interests": "interests",
    "chats": "conversations",
    "messages": "messages",
}
# The import never mixes legacy people with people of the new system: these must be empty.
MUST_BE_EMPTY = ("profiles", "interests", "conversations", "messages")
# Tables whose ids are kept (old inline buttons point at them): their sequences move past the highest
# legacy id ever issued (sqlite_sequence), so no id is reused. Profile numbers: see apply_plan.
ID_SEQUENCES: dict[str, str] = {"interests": "interests", "chats": "conversations", "messages": "messages"}

LEGACY_GENDERS = {"m": "male", "f": "female"}
LEGACY_PROFILE_STATUSES = {"approved": "active", "pending": "pending", "rejected": "rejected", "paused": "hidden"}
LEGACY_INTEREST_STATUSES = {"pending": "pending", "accepted": "accepted", "declined": "declined"}
# messages.witness_note values the legacy bot writes (handlers/chat.py: flag_message calls).
LEGACY_NOTE_CONTACTS = "contact-leak filter"
LEGACY_NOTE_REPORT = "user_report"
LEGACY_NOTE_WITNESS_PREFIX = "flagged by "  # followed by the witness's Telegram id: never copied


class Warn:
    """Warning counters of the import report (texts only; values are never printed)."""

    PROFILE_WITHOUT_USER = "анкеты, владельца которых нет в users, — пропущены"
    PROFILE_BAD_GENDER = "анкеты с неизвестным полом — пропущены"
    PROFILE_BAD_STATUS = "анкеты с неизвестным статусом — перенесены как ожидающие модерации"
    PROFILE_AGE = (
        "анкеты с возрастом вне 18–80 — год рождения ограничен диапазоном, анкета помечена и отправлена на модерацию"
    )
    TRUNCATED = "значения длиннее поля новой базы (город, национальность, username) — обрезаны"
    PROFILE_TEXT_LIMITS = "анкеты с текстами вне нынешних ограничений длины — перенесены как есть"
    INTEREST_MISSING_PROFILE = "отклики со ссылкой на отсутствующую или пропущенную анкету — пропущены"
    INTEREST_SELF = "отклики анкеты самой себе — пропущены"
    INTEREST_BAD_STATUS = "отклики с неизвестным статусом — пропущены"
    CHAT_MISSING_PROFILE = "чаты со ссылкой на отсутствующую или пропущенную анкету — пропущены"
    CHAT_SELF = "чаты анкеты с самой собой — пропущены"
    CHAT_EXTRA_OPEN = "лишние открытые чаты одной пары — закрыты временем открытия следующего"
    CHAT_NO_INTEREST = "чаты без принятого отклика этой пары — перенесены без ссылки на отклик"
    PERSON_SEVERAL_OPEN = "люди с несколькими открытыми чатами — перенесены как есть"
    BANNED_ACTIVE = "заблокированные с ожидающими откликами или открытыми чатами — перенесены как есть"
    ADMIN_BANNED = "администраторы с блокировкой в старой базе — блокировка не перенесена (админа нельзя заблокировать)"
    MESSAGE_MISSING_CHAT = "сообщения в отсутствующих или пропущенных чатах — пропущены"
    MESSAGE_FOREIGN_SENDER = "сообщения от анкеты, которая не участвует в чате, — пропущены"
    MESSAGE_TOO_LONG = "сообщения длиннее 2000 символов — перенесены как есть"
    MESSAGE_UNKNOWN_NOTE = "неизвестные отметки свидетеля — перенесены как отметка свидетеля"
    MUTED = "строки в muted — не переносятся"


class ImportRefused(Exception):
    """The import cannot run or failed its checks (Russian message); nothing was written."""


@dataclass(frozen=True)
class LegacyUser:
    telegram_id: int
    username: str | None
    role: str | None
    banned: bool
    created_at: datetime


@dataclass(frozen=True)
class LegacyProfile:
    id: int
    telegram_id: int | None
    gender: str | None
    age: int | None
    city: str
    nationality: str | None
    about: str | None
    wishes: str
    status: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class LegacyInterest:
    id: int
    from_profile: int | None
    to_profile: int | None
    status: str | None
    created_at: datetime


@dataclass(frozen=True)
class LegacyChat:
    id: int
    profile_a: int | None
    profile_b: int | None
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class LegacyMessage:
    id: int
    chat_id: int | None
    from_profile: int | None
    text: str
    sent_at: datetime
    flagged: bool
    witness_note: str | None


@dataclass
class LegacyData:
    users: list[LegacyUser]
    profiles: list[LegacyProfile]
    interests: list[LegacyInterest]
    chats: list[LegacyChat]
    messages: list[LegacyMessage]
    muted: int
    # sqlite_sequence: the highest id AUTOINCREMENT ever issued per table (also for deleted rows).
    sequences: dict[str, int]


def parse_time(value: Any) -> datetime:
    """SQLite ``datetime('now')`` text (UTC without a zone), ISO 8601 or a Unix time -> aware UTC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)  # noqa: UP017 (runs on Python 3.8 for --inspect)
    if not isinstance(value, str):
        raise ValueError("not a timestamp")
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)  # noqa: UP017
    return parsed.astimezone(timezone.utc)  # noqa: UP017


def _int(value: Any) -> int | None:
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,18}\s*", value):
        return int(value)
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def read_legacy(source: SqliteSource) -> LegacyData:
    """All rows the import needs, with timestamps parsed. Raises SourceError for a database that is
    not the legacy bot's or has unreadable timestamps (only table/column names and counts are told)."""
    for table, columns in LEGACY_COLUMNS.items():
        present = source.column_names(table)
        if present is None:
            raise SourceError(f"это не база старого бота: нет таблицы {table}")
        missing = [column for column in columns if column not in present]
        if missing:
            raise SourceError(f"это не база старого бота: в таблице {table} нет колонок {', '.join(missing)}")

    bad_times: Counter[str] = Counter()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)  # noqa: UP017 (placeholder; the import is refused)

    def when(table: str, column: str, value: Any) -> datetime:
        try:
            return parse_time(value)
        except (TypeError, ValueError, OverflowError, OSError):
            bad_times[f"{table}.{column}"] += 1
            return epoch

    def rows(table: str, order_by: str) -> list[sqlite3.Row]:
        return source.select(table, LEGACY_COLUMNS[table], order_by)

    users = [
        LegacyUser(
            telegram_id=row["user_id"],
            username=_text(row["tg_username"]),
            role=_text(row["role"]),
            banned=bool(_int(row["banned"])),
            created_at=when("users", "created_at", row["created_at"]),
        )
        for row in rows("users", "user_id")  # INTEGER PRIMARY KEY: always an integer (the rowid)
    ]
    profiles = [
        LegacyProfile(
            id=row["id"],
            telegram_id=_int(row["user_id"]),
            gender=_text(row["gender"]),
            age=_int(row["age"]),
            city=_text(row["city"]) or "",
            nationality=_text(row["nationality"]),
            about=_text(row["about"]),
            wishes=_text(row["partner_wishes"]) or "",
            status=_text(row["status"]),
            created_at=when("profiles", "created_at", row["created_at"]),
            updated_at=when("profiles", "updated_at", row["updated_at"]),
        )
        for row in rows("profiles", "id")
    ]
    interests = [
        LegacyInterest(
            id=row["id"],
            from_profile=_int(row["from_profile_id"]),
            to_profile=_int(row["to_profile_id"]),
            status=_text(row["status"]),
            created_at=when("interests", "created_at", row["created_at"]),
        )
        for row in rows("interests", "id")
    ]
    chats = [
        LegacyChat(
            id=row["id"],
            profile_a=_int(row["profile_a_id"]),
            profile_b=_int(row["profile_b_id"]),
            opened_at=when("chats", "opened_at", row["opened_at"]),
            closed_at=None if row["closed_at"] in (None, "") else when("chats", "closed_at", row["closed_at"]),
        )
        for row in rows("chats", "id")
    ]
    messages = [
        LegacyMessage(
            id=row["id"],
            chat_id=_int(row["chat_id"]),
            from_profile=_int(row["from_profile_id"]),
            text=_text(row["text"]) or "",
            sent_at=when("messages", "sent_at", row["sent_at"]),
            flagged=bool(_int(row["flagged"])),
            witness_note=_text(row["witness_note"]),
        )
        for row in rows("messages", "id")
    ]
    if bad_times:
        details = ", ".join(f"{name}: {count}" for name, count in sorted(bad_times.items()))
        raise SourceError(f"в старой базе есть даты, которые не удалось прочитать ({details}); ничего не перенесено")
    sequences: dict[str, int] = {}
    if source.column_names("sqlite_sequence") is not None:
        for row in source.select("sqlite_sequence", ("name", "seq"), "name"):
            if isinstance(row["seq"], int):
                sequences[str(row["name"])] = row["seq"]
    return LegacyData(users, profiles, interests, chats, messages, source.count("muted"), sequences)


@dataclass
class ImportReport:
    """Counters only: the report never contains names, texts, usernames or ids."""

    source: dict[str, int] = field(default_factory=dict)  # legacy table -> rows in the old database
    imported: dict[str, int] = field(default_factory=dict)  # legacy table -> rows written
    merged_users: int = 0  # legacy users that already had an account in the new database
    warnings: Counter[str] = field(default_factory=Counter)
    verified: bool = False

    def format(self, *, committed: bool) -> str:
        header = ("Таблица", "в старой базе", "перенесено", "пропущено")
        lines = [
            "Перенос данных старого бота (файл SQLite открыт только для чтения).",
            "",
            f"{header[0]:<24}{header[1]:>15}{header[2]:>13}{header[3]:>12}",
        ]
        for table, target in TARGET_TABLES.items():
            name = f"{table} → {target}"
            total, written = self.source.get(table, 0), self.imported.get(table, 0)
            lines.append(f"{name:<24}{total:>15}{written:>13}{total - written:>12}")
        lines.append(f"{'muted (не переносится)':<24}{self.source.get('muted', 0):>15}{'—':>13}{'—':>12}")
        lines += [
            "",
            f"Пользователи, которые уже были в новой базе (объединены по Telegram ID): {self.merged_users}",
            "Сверка: количество строк в новой базе совпадает с перенесённым."
            if self.verified
            else "Сверка не выполнена.",
            "",
        ]
        if self.warnings:
            lines.append("Предупреждения (только количество):")
            lines.extend(f"  - {text}: {count}" for text, count in self.warnings.items())
        else:
            lines.append("Предупреждений нет.")
        lines.append("")
        if committed:
            lines.append("Перенос записан в базу. Повторный перенос невозможен.")
        else:
            lines += [
                "ПРОБНЫЙ ПРОГОН: все изменения отменены, новая база не изменилась.",
                "Чтобы записать перенос, повторите команду с --commit.",
            ]
        return "\n".join(lines)


@dataclass
class ImportPlan:
    """Rows to write, keyed by Telegram ids (new user ids exist only after the users are written)."""

    users: list[dict[str, Any]]
    profiles: list[dict[str, Any]]
    interests: list[dict[str, Any]]
    conversations: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    # Target table -> highest legacy id ever issued (rows, skipped rows and sqlite_sequence).
    floors: dict[str, int]
    report: ImportReport


def _clip(value: str | None, limit: int, warnings: Counter[str]) -> str | None:
    if value is not None and len(value) > limit:
        warnings[Warn.TRUNCATED] += 1
        return value[:limit]
    return value


def _blank_to_none(value: str | None) -> str | None:
    return value if value is not None and value.strip() else None


def plan_import(data: LegacyData, *, admin_ids: Iterable[int], birth_bounds: tuple[int, int]) -> ImportPlan:
    """Map the legacy rows to the new schema (docs/LEGACY_BOT.md §4) and count what is skipped.

    ``birth_bounds`` = (oldest, youngest) allowed birth year today (schemas.birth_year_bounds)."""
    from app.enums import ConversationCloseReason, Gender, InterestStatus, MessageKind, MessageOrigin, ProfileStatus
    from app.models import CONTACT_FILTER_REASON, Profile, User
    from app.schemas import ABOUT_MAX, CHAT_MESSAGE_MAX, CITY_MAX, CITY_MIN, NATIONALITY_MAX, WISHES_MAX, WISHES_MIN
    from app.services.moderation import LEGACY_AGE_FLAG_REASON, REPORT_FLAG_REASON
    from app.services.witness import WITNESS_FLAG_REASON

    report = ImportReport(
        source={
            "users": len(data.users),
            "profiles": len(data.profiles),
            "interests": len(data.interests),
            "chats": len(data.chats),
            "messages": len(data.messages),
            "muted": data.muted,
        }
    )
    warnings = report.warnings
    admins = set(admin_ids)
    username_max = User.__table__.c.username.type.length
    city_column, nationality_column = Profile.__table__.c.city.type.length, Profile.__table__.c.nationality.type.length

    banned: set[int] = set()
    users: list[dict[str, Any]] = []
    for user in data.users:
        username = (user.username or "").strip().lstrip("@") or None
        is_admin = user.role == "admin" or user.telegram_id in admins
        if is_admin and user.banned:
            warnings[Warn.ADMIN_BANNED] += 1
        elif user.banned:
            banned.add(user.telegram_id)
        users.append(
            {
                "telegram_id": user.telegram_id,
                "username": _clip(username, username_max, warnings),
                "is_admin": is_admin,
                "is_banned": user.banned and not is_admin,
                "created_at": user.created_at,
            }
        )
    known_users = {user.telegram_id for user in data.users}

    oldest, youngest = birth_bounds
    owner: dict[int, int] = {}  # legacy profile id -> Telegram id of its owner (imported profiles only)
    profiles: list[dict[str, Any]] = []
    for profile in data.profiles:
        if profile.telegram_id is None or profile.telegram_id not in known_users:
            warnings[Warn.PROFILE_WITHOUT_USER] += 1
            continue
        gender = LEGACY_GENDERS.get(profile.gender or "")
        if gender is None:
            warnings[Warn.PROFILE_BAD_GENDER] += 1
            continue
        status = LEGACY_PROFILE_STATUSES.get(profile.status or "")
        if status is None:
            warnings[Warn.PROFILE_BAD_STATUS] += 1
            status = ProfileStatus.PENDING.value
        flag_reason = None
        birth_year = profile.created_at.year - profile.age if profile.age is not None else None
        if birth_year is None or not oldest <= birth_year <= youngest:
            # Conservative: a possibly underage (or implausible) age is never published unchecked.
            warnings[Warn.PROFILE_AGE] += 1
            birth_year = youngest if birth_year is None else min(max(birth_year, oldest), youngest)
            flag_reason = LEGACY_AGE_FLAG_REASON
            if status == ProfileStatus.ACTIVE.value:
                status = ProfileStatus.PENDING.value
        nationality, about = _blank_to_none(profile.nationality), _blank_to_none(profile.about)
        if (
            not CITY_MIN <= len(profile.city) <= CITY_MAX
            or len(nationality or "") > NATIONALITY_MAX
            or len(about or "") > ABOUT_MAX
            or not WISHES_MIN <= len(profile.wishes) <= WISHES_MAX
        ):
            warnings[Warn.PROFILE_TEXT_LIMITS] += 1
        profiles.append(
            {
                "telegram_id": profile.telegram_id,
                "number": profile.id,
                "gender": Gender(gender),
                "birth_year": birth_year,
                "city": _clip(profile.city, city_column, warnings),
                "nationality": _clip(nationality, nationality_column, warnings),
                "about": about,
                "wishes": profile.wishes,
                "status": ProfileStatus(status),
                "flagged": flag_reason is not None,
                "flag_reason": flag_reason,
                "submitted_at": profile.created_at,
                "approved_at": profile.updated_at if status == ProfileStatus.ACTIVE.value else None,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            }
        )
        owner[profile.id] = profile.telegram_id

    interests: list[dict[str, Any]] = []
    accepted: dict[frozenset[int], list[dict[str, Any]]] = {}
    for interest in data.interests:
        sender, recipient = owner.get(interest.from_profile or 0), owner.get(interest.to_profile or 0)
        if sender is None or recipient is None:
            warnings[Warn.INTEREST_MISSING_PROFILE] += 1
            continue
        if sender == recipient:
            warnings[Warn.INTEREST_SELF] += 1
            continue
        status = LEGACY_INTEREST_STATUSES.get(interest.status or "")
        if status is None:
            warnings[Warn.INTEREST_BAD_STATUS] += 1
            continue
        row = {
            "id": interest.id,
            "from_telegram_id": sender,
            "to_telegram_id": recipient,
            "status": InterestStatus(status),
            "created_at": interest.created_at,
        }
        interests.append(row)
        if status == InterestStatus.ACCEPTED.value:
            accepted.setdefault(frozenset((sender, recipient)), []).append(row)

    chats_by_pair: dict[frozenset[int], list[LegacyChat]] = {}
    for chat in data.chats:
        side_a, side_b = owner.get(chat.profile_a or 0), owner.get(chat.profile_b or 0)
        if side_a is None or side_b is None:
            warnings[Warn.CHAT_MISSING_PROFILE] += 1
        elif side_a == side_b:
            warnings[Warn.CHAT_SELF] += 1
        else:
            chats_by_pair.setdefault(frozenset((side_a, side_b)), []).append(chat)

    conversations: dict[int, dict[str, Any]] = {}
    for pair, chats in chats_by_pair.items():
        chats.sort(key=lambda chat: (chat.opened_at, chat.id))
        # The new schema allows one open chat per pair: an older open chat (a race in the legacy
        # bot) ends when the next one opened.
        open_chats = [chat for chat in chats if chat.closed_at is None]
        superseded = {older.id: newer.opened_at for older, newer in itertools.pairwise(open_chats)}
        warnings[Warn.CHAT_EXTRA_OPEN] += len(superseded)
        # created_from_interest_id: the pair's accepted interests in order, each used once (the
        # legacy data does not say which interest opened which chat).
        candidates = sorted(accepted.get(pair, []), key=lambda row: (row["created_at"], row["id"]))
        for chat in chats:
            interest = next((row for row in candidates if row["created_at"] <= chat.opened_at), None)
            if interest is None and candidates:
                interest = candidates[0]
            if interest is None:
                warnings[Warn.CHAT_NO_INTEREST] += 1
            else:
                candidates.remove(interest)
            closed_at = superseded.get(chat.id, chat.closed_at)
            conversations[chat.id] = {
                "id": chat.id,
                "telegram_ids": tuple(sorted(pair)),
                "profiles": (chat.profile_a, chat.profile_b),
                "created_from_interest_id": interest["id"] if interest else None,
                "created_at": chat.opened_at,
                "last_message_at": None,
                "closed_at": closed_at,
                "close_reason": ConversationCloseReason.CLOSED_BY_USER if closed_at else None,
            }

    messages: list[dict[str, Any]] = []
    for message in data.messages:
        conversation = conversations.get(message.chat_id or 0)
        if conversation is None:
            warnings[Warn.MESSAGE_MISSING_CHAT] += 1
            continue
        if message.from_profile not in conversation["profiles"]:
            warnings[Warn.MESSAGE_FOREIGN_SENDER] += 1
            continue
        if len(message.text) > CHAT_MESSAGE_MAX:
            warnings[Warn.MESSAGE_TOO_LONG] += 1
        note = message.witness_note
        if note is None or not note.strip():
            reason = None
        elif note == LEGACY_NOTE_CONTACTS:
            reason = CONTACT_FILTER_REASON
        elif note == LEGACY_NOTE_REPORT:
            reason = REPORT_FLAG_REASON
        else:
            # «flagged by <Telegram id>» and anything else an admin wrote: the code only, no ids.
            if not note.startswith(LEGACY_NOTE_WITNESS_PREFIX):
                warnings[Warn.MESSAGE_UNKNOWN_NOTE] += 1
            reason = WITNESS_FLAG_REASON
        messages.append(
            {
                "id": message.id,
                "conversation_id": conversation["id"],
                "kind": MessageKind.USER,
                "sender_telegram_id": owner[message.from_profile],
                "text": message.text,
                "origin": MessageOrigin.TELEGRAM,
                "created_at": message.sent_at,
                "read_at": message.sent_at,
                "flagged": message.flagged,
                "flag_reason": reason,
            }
        )
        last = conversation["last_message_at"]
        conversation["last_message_at"] = message.sent_at if last is None else max(last, message.sent_at)

    open_count: Counter[int] = Counter()
    for conversation in conversations.values():
        if conversation["closed_at"] is None:
            open_count.update(conversation["telegram_ids"])
    warnings[Warn.PERSON_SEVERAL_OPEN] += sum(1 for count in open_count.values() if count > 1)
    active = {telegram_id for telegram_id, count in open_count.items() if count}
    active.update(
        telegram_id
        for row in interests
        if row["status"] == InterestStatus.PENDING
        for telegram_id in (row["from_telegram_id"], row["to_telegram_id"])
    )
    warnings[Warn.BANNED_ACTIVE] += len(banned & active)
    warnings[Warn.MUTED] += data.muted

    def floor(table: str, ids: Iterable[int]) -> int:
        return max(max(ids, default=0), data.sequences.get(table, 0))

    report.imported = {
        "users": len(users),
        "profiles": len(profiles),
        "interests": len(interests),
        "chats": len(conversations),
        "messages": len(messages),
    }
    # Drop zero counters so the report lists only what happened.
    report.warnings = Counter({text: count for text, count in warnings.items() if count})
    return ImportPlan(
        users=users,
        profiles=profiles,
        interests=interests,
        conversations=sorted(conversations.values(), key=lambda row: row["id"]),
        messages=messages,
        floors={
            "profiles": floor("profiles", (profile.id for profile in data.profiles)),
            "interests": floor("interests", (interest.id for interest in data.interests)),
            "chats": floor("chats", (chat.id for chat in data.chats)),
            "messages": floor("messages", (message.id for message in data.messages)),
        },
        report=report,
    )


async def _count_rows(session: AsyncSession, table: str) -> int:
    from sqlalchemy import text

    return await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0


async def _advance_id_sequence(session: AsyncSession, table: str, floor: int) -> None:
    """Move the id sequence of ``table`` past max(id, ``floor``); never back."""
    from sqlalchemy import text

    await session.execute(
        text(
            f"SELECT setval(seq, target) FROM (SELECT seq, GREATEST(CAST(:floor AS bigint), "
            f"(SELECT COALESCE(MAX(id), 0) FROM {table}), pg_sequence_last_value(seq)) AS target "
            f"FROM (SELECT pg_get_serial_sequence('{table}', 'id')::regclass AS seq) s) t WHERE target > 0"
        ),
        {"floor": floor},
    )


async def apply_plan(session: AsyncSession, plan: ImportPlan, *, advance_sequences: bool) -> ImportReport:
    """Write the plan in the caller's transaction (the caller commits or rolls back).

    Raises ImportRefused before writing anything if the schema is not migrated, the import was done
    before, or the target already has profiles/interests/conversations/messages; and after writing
    if the row counts do not match. Sequences are advanced only with ``advance_sequences`` (setval
    cannot be rolled back, so a dry run leaves them alone). No events are emitted."""
    from sqlalchemy import insert, select, text

    from app.models import Conversation, Interest, Message, Profile, User
    from app.services import meta
    from app.services import profiles as profile_service
    from app.services.errors import LegacyImportDone

    report = plan.report
    if await session.scalar(text("SELECT to_regclass('public.app_meta')")) is None:
        raise ImportRefused("схема новой базы не обновлена: сначала выполните alembic upgrade head (её применяет web)")
    # Nobody may write people's data while it is checked and filled (the site and the bot wait).
    await session.execute(text("SET LOCAL lock_timeout = '15s'"))
    await session.execute(
        text("LOCK TABLE users, profiles, interests, conversations, messages, app_meta IN SHARE ROW EXCLUSIVE MODE")
    )
    marker = await meta.legacy_import_marker(session)
    if marker is not None:
        raise ImportRefused(f"перенос уже выполнен ({marker.get('imported_at', '?')}); повторный перенос невозможен")
    busy = {table: count for table in MUST_BE_EMPTY if (count := await _count_rows(session, table))}
    if busy:
        found = ", ".join(f"{table}: {count}" for table, count in busy.items())
        raise ImportRefused(
            f"в новой базе уже есть данные ({found}); перенос возможен только в базу без анкет, откликов, "
            "чатов и сообщений (пользователи без анкет допустимы)"
        )

    users_before = await _count_rows(session, "users")
    existing = {user.telegram_id: user for user in await session.scalars(select(User))}
    user_ids: dict[int, int] = {}
    new_users: list[dict[str, Any]] = []
    for row in plan.users:
        user = existing.get(row["telegram_id"])
        if user is None:
            new_users.append(row)
            continue
        report.merged_users += 1
        user.is_admin = user.is_admin or row["is_admin"]
        if row["is_banned"] and not user.is_admin:
            user.is_banned = True
        elif row["is_banned"]:
            report.warnings[Warn.ADMIN_BANNED] += 1
        user.username = user.username or row["username"]
        user.created_at = min(user.created_at, row["created_at"])
        user_ids[user.telegram_id] = user.id
    await session.flush()
    if new_users:
        result = await session.execute(insert(User).returning(User.id, User.telegram_id), new_users)
        user_ids.update({telegram_id: user_id for user_id, telegram_id in result})

    if plan.profiles:
        await session.execute(
            insert(Profile),
            [
                {"user_id": user_ids[row["telegram_id"]], **{k: v for k, v in row.items() if k != "telegram_id"}}
                for row in plan.profiles
            ],
        )
    if plan.interests:
        await session.execute(
            insert(Interest),
            [
                {
                    "id": row["id"],
                    "from_user_id": user_ids[row["from_telegram_id"]],
                    "to_user_id": user_ids[row["to_telegram_id"]],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in plan.interests
            ],
        )
    if plan.conversations:
        conversations = []
        for row in plan.conversations:
            side_a, side_b = sorted(user_ids[telegram_id] for telegram_id in row["telegram_ids"])
            conversations.append(
                {
                    "id": row["id"],
                    "user_a_id": side_a,
                    "user_b_id": side_b,
                    "created_from_interest_id": row["created_from_interest_id"],
                    "created_at": row["created_at"],
                    "last_message_at": row["last_message_at"],
                    "closed_at": row["closed_at"],
                    "close_reason": row["close_reason"],
                }
            )
        await session.execute(insert(Conversation), conversations)
    if plan.messages:
        await session.execute(
            insert(Message),
            [
                {
                    "sender_id": user_ids[row["sender_telegram_id"]],
                    **{k: v for k, v in row.items() if k != "sender_telegram_id"},
                }
                for row in plan.messages
            ],
        )
    await session.flush()

    expected = {
        "users": users_before + len(new_users),
        "profiles": len(plan.profiles),
        "interests": len(plan.interests),
        "conversations": len(plan.conversations),
        "messages": len(plan.messages),
    }
    actual = {table: await _count_rows(session, table) for table in expected}
    if actual != expected:
        mismatch = ", ".join(f"{table}: {actual[table]} вместо {count}" for table, count in expected.items())
        raise ImportRefused(f"сверка количества строк не сошлась ({mismatch}); ничего не записано")
    report.verified = True

    try:
        counts = {TARGET_TABLES[table]: count for table, count in report.imported.items()}
        await meta.mark_legacy_import(session, counts)
    except LegacyImportDone as exc:
        raise ImportRefused("перенос уже выполнен другим запуском; повторный перенос невозможен") from exc
    if advance_sequences:
        await profile_service.advance_number_sequence(session, floor=plan.floors["profiles"])
        for legacy_table, table in ID_SEQUENCES.items():
            await _advance_id_sequence(session, table, plan.floors[legacy_table])
    return report


async def run_import(path: str, *, commit: bool) -> ImportReport:
    """Read the legacy SQLite file (read-only) and import it in one transaction; dry run unless
    ``commit``. Uses the application's database (DATABASE_URL)."""
    if _POSTGRES_URL.match(path):
        raise SourceError("перенос читает только файл SQLite старого бота (data.db), а не PostgreSQL")
    try:
        source = SqliteSource(path)
    except sqlite3.Error as exc:
        raise SourceError(f"не удалось открыть базу SQLite: {exc}") from exc
    try:
        data = read_legacy(source)
    except sqlite3.Error as exc:
        raise SourceError(f"не удалось прочитать базу SQLite: {exc}") from exc
    finally:
        await source.close()

    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.schemas import birth_year_bounds

    plan = plan_import(data, admin_ids=get_settings().ADMIN_TELEGRAM_IDS, birth_bounds=birth_year_bounds())
    async with get_sessionmaker()() as session:
        report = await apply_plan(session, plan, advance_sequences=commit)
        if commit:
            await session.commit()
        else:
            await session.rollback()
    return report


async def _import_cli(path: str, *, commit: bool) -> str:
    from app.db import dispose_engine

    try:
        return (await run_import(path, commit=commit)).format(committed=commit)
    finally:
        await dispose_engine()


def _database_error(exc: Exception) -> str | None:
    """A database error without its details (they may hold personal data): type and constraint."""
    try:
        from sqlalchemy.exc import DBAPIError
    except ImportError:
        return None
    if not isinstance(exc, DBAPIError):
        return None
    constraint = getattr(exc.orig, "constraint_name", None) or getattr(
        getattr(exc.orig, "__cause__", None), "constraint_name", None
    )
    return f"{type(exc).__name__}" + (f", ограничение {constraint}" if constraint else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_legacy",
        description="База старого бота: безопасный просмотр схемы (--inspect) и перенос в новую базу (--import).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--inspect",
        metavar="ИСТОЧНИК",
        help="показать только таблицы, колонки, типы и количество строк (файл SQLite или postgresql://...)",
    )
    mode.add_argument(
        "--import",
        dest="import_from",
        metavar="ФАЙЛ",
        help="перенести копию data.db старого бота в новую базу (без --commit — пробный прогон)",
    )
    parser.add_argument("--commit", action="store_true", help="записать перенос (без флага всё откатывается)")
    args = parser.parse_args(argv)
    if args.inspect and args.commit:
        parser.error("--commit используется только с --import")
    try:
        if args.inspect:
            print(asyncio.run(inspect(args.inspect)))
        else:
            print(asyncio.run(_import_cli(args.import_from, commit=args.commit)))
    except SourceError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except ImportRefused as exc:
        print(f"Перенос не выполнен: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        details = _database_error(exc)
        if details is None:
            raise
        print(f"Перенос не выполнен: ошибка базы данных ({details}); ничего не записано.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
