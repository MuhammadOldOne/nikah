"""Alembic: revision 0003 upgrades a database that holds 0002 data, matches the models and
downgrades cleanly. Runs alembic in a subprocess against a scratch database next to the test one."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from app.services.events import asyncpg_dsn
from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[2]
BASE_URL, TEST_DB = TEST_DATABASE_URL.rsplit("/", 1)
SCRATCH_DB = f"{TEST_DB}_migrations"
SCRATCH_URL = f"{BASE_URL}/{SCRATCH_DB}"

DATA_0002 = """
INSERT INTO users (id, telegram_id) VALUES (1, 1001), (2, 1002), (3, 1003);
INSERT INTO profiles (user_id, gender, name, birth_year, city, marital_status, children, education, employment,
                      wishes, status, created_at)
VALUES (2, 'female', 'Фатима', 1995, 'Казань', 'never_married', 0, 'higher', 'employed', 'Ищу мужа для семьи',
        'active', now() - interval '2 days'),
       (1, 'male', 'Ахмад', 1990, 'Казань', 'never_married', 0, 'higher', 'employed', 'Ищу жену для семьи',
        'pending', now() - interval '1 day');
INSERT INTO interests (id, from_user_id, to_user_id, status) VALUES (1, 1, 2, 'accepted');
INSERT INTO conversations (id, user_a_id, user_b_id, created_from_interest_id) VALUES (1, 1, 2, 1);
INSERT INTO messages (id, conversation_id, sender_id, text, origin) VALUES (1, 1, 1, 'Салам', 'web');
"""


def alembic(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": SCRATCH_URL},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def sql(database: str, *statements: str) -> list[list[asyncpg.Record]]:
    """Run ``statements`` in ``database`` (a fresh event loop: this test is synchronous); returns the
    rows of each SELECT (other statements may hold several commands and return [])."""

    async def run() -> list[list[asyncpg.Record]]:
        conn = await asyncpg.connect(asyncpg_dsn(f"{BASE_URL}/{database}"))
        try:
            results: list[list[asyncpg.Record]] = []
            for statement in statements:
                if statement.lstrip().upper().startswith("SELECT"):
                    results.append(await conn.fetch(statement))
                else:
                    await conn.execute(statement)
                    results.append([])
            return results
        finally:
            await conn.close()

    return asyncio.run(run())


@pytest.fixture
def scratch_database():
    sql(TEST_DB, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"', f'CREATE DATABASE "{SCRATCH_DB}"')
    yield
    sql(TEST_DB, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')


def test_0003_upgrades_0002_data_and_downgrades(scratch_database):
    alembic("upgrade", "0002")
    sql(SCRATCH_DB, DATA_0002)

    alembic("upgrade", "head")
    assert "No new upgrade operations detected" in alembic("check")
    [numbers, next_number] = sql(
        SCRATCH_DB,
        "SELECT user_id, number FROM profiles ORDER BY number",
        "SELECT nextval('profiles_number_seq') AS n",
    )
    assert [(row["user_id"], row["number"]) for row in numbers] == [(2, 500), (1, 501)]  # by creation time
    assert next_number[0]["n"] == 502
    [dialogs] = sql(SCRATCH_DB, "SELECT count(*) AS n FROM bot_dialog_states")  # 0004: the bot's dialog state
    assert dialogs[0]["n"] == 0
    # Legacy states are valid now: an incomplete profile, both directions pending, a second chat,
    # a witness message.
    sql(
        SCRATCH_DB,
        "INSERT INTO profiles (user_id, gender, birth_year, city, wishes) VALUES (3, 'male', 1990, 'Уфа', 'x')",
        "INSERT INTO interests (id, from_user_id, to_user_id) VALUES (2, 2, 1), (3, 1, 2)",
        "UPDATE conversations SET closed_at = now() WHERE id = 1",
        "INSERT INTO conversations (id, user_a_id, user_b_id) VALUES (2, 1, 2)",
        "INSERT INTO messages (id, conversation_id, kind, text, origin) VALUES (2, 2, 'witness', 'т', 'telegram')",
    )

    with pytest.raises(AssertionError, match="cannot downgrade to 0002"):  # 0002 cannot hold this
        alembic("downgrade", "0002")
    sql(
        SCRATCH_DB,
        "DELETE FROM profiles WHERE user_id = 3",
        "DELETE FROM interests WHERE id IN (2, 3)",
        "DELETE FROM conversations WHERE id = 2",
    )
    alembic("downgrade", "0002")
    [profiles, sequences] = sql(
        SCRATCH_DB,
        "SELECT user_id, name FROM profiles ORDER BY user_id",
        "SELECT count(*) AS n FROM pg_class WHERE relname = 'profiles_number_seq'",
    )
    assert [(row["user_id"], row["name"]) for row in profiles] == [(1, "Анкета 501"), (2, "Анкета 500")]
    assert sequences[0]["n"] == 0
    alembic("upgrade", "head")  # and up again
