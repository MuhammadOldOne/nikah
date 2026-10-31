"""Synthetic legacy databases for the import tests.

The schema is tests/fixtures/legacy_schema.sql (DDL of the legacy bot, no data); every row is made
up. ``legacy`` builds a file the way the legacy bot's db.py writes it (profiles numbered from 500,
SQLite timestamps as UTC text, foreign keys not enforced).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_schema.sql"


class LegacyDb:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connect() as db:
            db.executescript(SCHEMA.read_text())
            # db.py init_db: the first profile gets id 500.
            db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('profiles', 499)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _insert(self, sql: str, params: tuple[object, ...]) -> int:
        with self._connect() as db:
            return int(db.execute(sql, params).lastrowid)

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self._connect() as db:
            db.execute(sql, params)

    def user(
        self,
        telegram_id: int,
        username: str | None = None,
        *,
        role: str = "user",
        banned: bool = False,
        created_at: str = "2025-02-01 08:00:00",
    ) -> int:
        self._insert(
            "INSERT INTO users (user_id, tg_username, role, banned, created_at) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, username, role, int(banned), created_at),
        )
        return telegram_id

    def profile(
        self,
        telegram_id: int,
        *,
        gender: str = "m",
        age: int = 30,
        city: str = "Казань",
        nationality: str = "татарин",
        about: str = "Работаю, читаю намаз.",
        wishes: str = "Ищу верующую супругу.",
        status: str = "approved",
        created_at: str = "2025-03-01 10:00:00",
        updated_at: str = "2025-03-01 12:00:00",
        with_user: bool = True,
    ) -> int:
        if with_user:
            self.user(telegram_id)
        return self._insert(
            "INSERT INTO profiles (user_id, gender, age, city, nationality, about, partner_wishes, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, gender, age, city, nationality, about, wishes, status, created_at, updated_at),
        )

    def interest(
        self, from_id: int, to_id: int, status: str = "pending", created_at: str = "2025-04-01 09:00:00"
    ) -> int:
        return self._insert(
            "INSERT INTO interests (from_profile_id, to_profile_id, status, created_at) VALUES (?, ?, ?, ?)",
            (from_id, to_id, status, created_at),
        )

    def chat(self, profile_a: int, profile_b: int, opened_at: str, closed_at: str | None = None) -> int:
        low, high = sorted((profile_a, profile_b))  # db.py open_chat orders the pair
        return self._insert(
            "INSERT INTO chats (profile_a_id, profile_b_id, opened_at, closed_at) VALUES (?, ?, ?, ?)",
            (low, high, opened_at, closed_at),
        )

    def message(
        self,
        chat_id: int,
        from_profile: int,
        text: str,
        sent_at: str,
        *,
        flagged: bool = False,
        note: str | None = None,
    ) -> int:
        return self._insert(
            "INSERT INTO messages (chat_id, from_profile_id, tg_message_id_src, tg_message_id_dst, text, sent_at,"
            " flagged, witness_note) VALUES (?, ?, 11, 22, ?, ?, ?, ?)",
            (chat_id, from_profile, text, sent_at, int(flagged), note),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@pytest.fixture
def legacy(tmp_path: Path) -> LegacyDb:
    return LegacyDb(tmp_path / "data.db")
