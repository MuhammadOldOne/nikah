#!/usr/bin/env python3
"""Legacy bot database: safe schema inspection and import into the new database.

--inspect needs only the Python standard library for SQLite (plus asyncpg for PostgreSQL), so this
file can also be copied to the old server and run there on its own (Python 3.8+):

    python3 import_legacy.py --inspect /path/to/bot.db
    python3 import_legacy.py --inspect 'postgresql://user:password@127.0.0.1:5432/botdb'

It opens the database READ-ONLY and prints ONLY the schema (tables, columns, types, keys) and row
counts, never row contents, so the output can be shared safely.

--import runs inside the project (it needs the ``app`` package and DATABASE_URL): it reads legacy
rows through the MAPPING section below and writes users and profiles into the new database. It is a
dry run (rolled back) unless --commit is given. See docs/LEGACY_IMPORT.md.
"""

from __future__ import annotations

import abc
import argparse
import asyncio
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
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
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Rows of a read-only query (for the MAPPING section)."""

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

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query, args)]

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

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in await self.connection.fetch(query, *args)]

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


# --- import ---------------------------------------------------------------------------------------


@dataclass
class LegacyRecord:
    """One person from the legacy bot, already converted to the new vocabulary."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    # When the person consented to data processing in the old bot, and only if the lawyer confirmed
    # that this consent covers the new service. None: the person is not imported (asked again).
    consent_at: datetime | None = None
    # Fields of app.schemas.ProfileCreate (gender, name, birth_year, city, marital_status, ...).
    profile: dict[str, Any] | None = None


# ==================================== MAPPING ====================================================
# Fill in after --inspect, once the legacy schema is known (docs/LEGACY_IMPORT.md, step 3).
# Read rows with ``await source.fetch("SELECT ... FROM ...")`` and convert values to the new
# vocabulary: the enums of app/enums.py (gender "male"/"female", marital_status "never_married" /
# "divorced" / "widowed", education, employment, ...) within the limits of app/schemas.py.
async def read_legacy_records(source: LegacySource) -> list[LegacyRecord]:
    raise NotImplementedError("Заполните маппинг после --inspect")


# ================================== END MAPPING ==================================================


@dataclass
class ImportReport:
    users_created: int = 0
    users_existing: int = 0
    skipped_no_consent: int = 0
    profiles_created: int = 0
    # Field name -> number of profiles rejected by validation (values are never reported).
    profiles_invalid: Counter[str] = field(default_factory=Counter)

    def format(self, *, committed: bool) -> str:
        lines = [
            "Импорт записан в базу." if committed else "Пробный прогон: ничего не записано (добавьте --commit).",
            f"Новых пользователей: {self.users_created}",
            f"Уже были в новой базе (не изменялись): {self.users_existing}",
            f"Пропущено без согласия на обработку данных: {self.skipped_no_consent}",
            f"Анкет перенесено: {self.profiles_created}",
        ]
        if self.profiles_invalid:
            lines.append("Анкеты не прошли проверку (поле: сколько анкет):")
            lines.extend(f"  {name}: {count}" for name, count in self.profiles_invalid.most_common())
        return "\n".join(lines)


async def import_records(session: AsyncSession, records: Iterable[LegacyRecord]) -> ImportReport:
    """Create accounts and profiles for legacy people (the caller commits or rolls back).

    Accounts that already exist in the new database (same Telegram id) are never changed. People
    without a recorded consent are skipped. Profiles are validated like new ones and go to the
    moderation queue (published at once if PREMODERATION is off); no notifications are sent.
    """
    from pydantic import ValidationError
    from sqlalchemy import select

    from app.clock import utcnow
    from app.config import get_settings
    from app.enums import ProfileStatus
    from app.models import Profile, User
    from app.schemas import ProfileCreate, humanize_errors
    from app.services.stopwords import flag_reason

    report = ImportReport()
    premoderation = get_settings().PREMODERATION
    for record in records:
        if await session.scalar(select(User.id).where(User.telegram_id == record.telegram_id)) is not None:
            report.users_existing += 1
            continue
        if record.consent_at is None:
            report.skipped_no_consent += 1
            continue
        data: ProfileCreate | None = None
        if record.profile is not None:
            try:
                data = ProfileCreate.model_validate(record.profile)
            except ValidationError as exc:
                report.profiles_invalid.update(humanize_errors(exc).keys())
        user = User(
            telegram_id=record.telegram_id,
            username=record.username[:64] if record.username else None,
            first_name=record.first_name[:128] if record.first_name else None,
            consent_at=record.consent_at,
        )
        session.add(user)
        await session.flush()
        report.users_created += 1
        if data is None:
            continue
        now = utcnow()
        reason = flag_reason(data.name, data.city, data.occupation, data.nationality, data.wishes, data.about)
        session.add(
            Profile(
                user_id=user.id,
                **data.model_dump(),
                status=ProfileStatus.PENDING if premoderation else ProfileStatus.ACTIVE,
                submitted_at=now,
                approved_at=None if premoderation else now,
                flagged=reason is not None,
                flag_reason=reason,
            )
        )
        report.profiles_created += 1
    await session.flush()
    return report


async def run_import(location: str, *, commit: bool) -> ImportReport:
    source = await open_source(location)
    try:
        records = await read_legacy_records(source)
    finally:
        await source.close()

    from app.db import dispose_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            report = await import_records(session, records)
            if commit:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await dispose_engine()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import_legacy",
        description="Данные старого бота: безопасный просмотр схемы (--inspect) и перенос в новую базу (--import).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--inspect",
        metavar="ИСТОЧНИК",
        help="показать только таблицы, колонки, типы и количество строк (файл SQLite или postgresql://...)",
    )
    mode.add_argument(
        "--import", dest="import_from", metavar="ИСТОЧНИК", help="перенести пользователей и анкеты в новую базу"
    )
    parser.add_argument("--commit", action="store_true", help="записать результат импорта (без флага — пробный прогон)")
    args = parser.parse_args(argv)
    if args.inspect and args.commit:
        parser.error("--commit используется только с --import")
    try:
        if args.inspect:
            print(asyncio.run(inspect(args.inspect)))
        else:
            report = asyncio.run(run_import(args.import_from, commit=args.commit))
            print(report.format(committed=args.commit))
    except NotImplementedError as exc:
        print(exc, file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
