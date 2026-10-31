"""Preparation of the end-to-end run, called by the e2e runner from the repository root:

    python -m tests.e2e.prepare wipe              # empty the e2e database (its name must contain "test")
    python -m tests.e2e.prepare legacy PATH       # write a synthetic legacy data.db, print what it holds

The legacy file follows tests/fixtures/legacy_schema.sql (the old bot's DDL) and holds made-up rows
only: a brother and a sister (profiles №512 and №513, both approved), his accepted interest, their
open chat with three messages (one already hidden by the old bot's contact filter), and a profile
sequence at 520 (numbers 514–520 were used by deleted profiles and must never come back). The
runner imports it with ``scripts/import_legacy.py --import … --commit`` into the emptied database.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg

from app.config import get_settings
from app.services.events import asyncpg_dsn

# ``tests/import`` is a package whose name is a keyword: import its synthetic-database builder by name.
LegacyDb = importlib.import_module("tests.import.conftest").LegacyDb

LEGACY_BROTHER, LEGACY_SISTER = 9_200_000_011, 9_200_000_012
LEGACY_SEQUENCE = 520
LEGACY_MESSAGES = (
    ("brother", "Ассаляму алейкум! Пишу вам из старого бота.", "2025-04-01 10:05:00"),
    ("sister", "Ва алейкум ассалам. Рада знакомству.", "2025-04-01 10:06:00"),
    ("brother", "Мой номер [скрыто свидетелем]", "2025-04-01 10:07:00"),
)


async def wipe() -> dict[str, Any]:
    url = get_settings().DATABASE_URL
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in name:
        raise SystemExit("the e2e run empties its database: its name must contain 'test'")
    connection = await asyncpg.connect(asyncpg_dsn(url))
    try:
        await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await connection.close()
    return {"wiped": name}


def legacy(path: Path) -> dict[str, Any]:
    path.unlink(missing_ok=True)
    db = LegacyDb(path)
    db.execute("UPDATE sqlite_sequence SET seq = 511 WHERE name = 'profiles'")
    profiles = {
        "brother": db.profile(LEGACY_BROTHER, gender="m", age=31, city="Казань", created_at="2025-03-01 10:00:00"),
        "sister": db.profile(
            LEGACY_SISTER,
            gender="f",
            age=27,
            city="Казань",
            nationality="татарка",
            wishes="Ищу верующего и заботливого мужа.",
            created_at="2025-03-02 10:00:00",
        ),
    }
    interest = db.interest(profiles["brother"], profiles["sister"], "accepted", "2025-04-01 09:00:00")
    chat = db.chat(profiles["brother"], profiles["sister"], "2025-04-01 10:00:00")
    for author, text, sent_at in LEGACY_MESSAGES:
        hidden = "[скрыто" in text
        note = "contact-leak filter" if hidden else None
        db.message(chat, profiles[author], text, sent_at, flagged=hidden, note=note)
    db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'profiles'", (LEGACY_SEQUENCE,))
    return {
        "path": str(path),
        "telegramIds": {"brother": LEGACY_BROTHER, "sister": LEGACY_SISTER},
        "numbers": profiles,
        "interestId": interest,
        "chatId": chat,
        "messages": [text for _, text, _ in LEGACY_MESSAGES],
        "sequence": LEGACY_SEQUENCE,
    }


def main(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.prepare")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("wipe")
    commands.add_parser("legacy").add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "wipe":
        return asyncio.run(wipe())
    return legacy(args.path)


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:]), ensure_ascii=False))
