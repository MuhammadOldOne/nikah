"""Operations scripts: make_admin and the legacy database inspector (the import: tests/import)."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import User
from scripts import import_legacy
from scripts.import_legacy import inspect
from scripts.make_admin import CommandError, set_admin
from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ("Зарема Секретова", "+79990001122", "zarema_secret", "987654321")


# --- make_admin -----------------------------------------------------------------------------------


async def _fresh(session, user_id: int) -> User:
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


async def test_make_admin_grant_and_revoke(session, make_user):
    user = await make_user(telegram_id=424242)
    assert "администратор" in await set_admin(424242, grant=True)
    assert (await _fresh(session, user.id)).is_admin
    assert "больше не администратор" in await set_admin(424242, grant=False)
    assert not (await _fresh(session, user.id)).is_admin


async def test_make_admin_warns_about_env_admins(make_user, override_settings):
    await make_user(telegram_id=515151)
    override_settings(ADMIN_TELEGRAM_IDS=[515151])
    assert "ADMIN_TELEGRAM_IDS" in await set_admin(515151, grant=False)


async def test_make_admin_errors(make_user):
    with pytest.raises(CommandError, match="/start"):
        await set_admin(1, grant=True)
    await make_user(telegram_id=616161, is_banned=True)
    with pytest.raises(CommandError, match="заблокирован"):
        await set_admin(616161, grant=True)


# --- import_legacy --inspect ----------------------------------------------------------------------


# A table name that needs identifier quoting: anketa "v1"
ODD_TABLE = '"anketa ""v1"""'


@pytest.fixture
def legacy_sqlite(tmp_path: Path) -> Path:
    path = tmp_path / "bot.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL, name TEXT, phone TEXT)")
        db.execute(f"CREATE TABLE {ODD_TABLE} (user_id INTEGER REFERENCES users(id), about TEXT, username VARCHAR(64))")
        db.execute("INSERT INTO users (telegram_id, name, phone) VALUES (?, ?, ?)", (987654321, SECRETS[0], SECRETS[1]))
        db.execute(f"INSERT INTO {ODD_TABLE} VALUES (1, ?, ?)", (f"{SECRETS[0]} о себе", SECRETS[2]))
        db.execute(f"INSERT INTO {ODD_TABLE} VALUES (1, NULL, NULL)")
    return path


def _assert_no_secrets(output: str) -> None:
    for secret in SECRETS:
        assert secret not in output


def test_inspect_sqlite_prints_schema_only(legacy_sqlite: Path, capsys):
    before = hashlib.sha256(legacy_sqlite.read_bytes()).hexdigest()
    assert import_legacy.main(["--inspect", str(legacy_sqlite)]) == 0
    output = capsys.readouterr().out
    assert "Источник: SQLite" in output and "Таблиц: 2" in output
    assert "Таблица users (строк: 1)" in output
    assert 'Таблица anketa "v1" (строк: 2)' in output
    assert "telegram_id  INTEGER  NOT NULL" in output and "id           INTEGER  PK" in output
    assert "связь: user_id -> users.id" in output
    _assert_no_secrets(output)
    assert hashlib.sha256(legacy_sqlite.read_bytes()).hexdigest() == before  # opened read-only


def test_inspect_works_without_the_app_package(legacy_sqlite: Path, tmp_path: Path):
    """The file can be copied to the old server and run with a bare Python (standard library)."""
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    script = standalone / "import_legacy.py"
    script.write_text((ROOT / "scripts" / "import_legacy.py").read_text())
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--inspect", str(legacy_sqlite)],
        capture_output=True,
        text=True,
        cwd=standalone,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Таблица users (строк: 1)" in result.stdout
    _assert_no_secrets(result.stdout + result.stderr)


async def test_inspect_postgres(make_user):
    await make_user(telegram_id=987654321, username="zarema_secret", first_name="Зарема Секретова")
    output = await inspect(TEST_DATABASE_URL)
    assert "Источник: PostgreSQL" in output
    assert "Таблица users (строк: 1)" in output
    assert "telegram_id" in output and "bigint" in output
    assert "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE" in output
    _assert_no_secrets(output)


def test_inspect_rejects_other_files(tmp_path: Path, capsys):
    path = tmp_path / "users.json"
    path.write_text('{"name": "Зарема Секретова"}')
    assert import_legacy.main(["--inspect", str(path)]) == 1
    captured = capsys.readouterr()
    assert "не база SQLite" in captured.err
    _assert_no_secrets(captured.out + captured.err)
    assert import_legacy.main(["--inspect", str(tmp_path / "missing.db")]) == 1


def test_commit_only_with_import(legacy_sqlite: Path):
    with pytest.raises(SystemExit):
        import_legacy.main(["--inspect", str(legacy_sqlite), "--commit"])
