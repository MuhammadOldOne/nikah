"""Operations scripts: make_admin and the legacy database inspector/importer."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.enums import ProfileStatus
from app.models import Profile, User
from scripts import import_legacy
from scripts.import_legacy import LegacyRecord, import_records, inspect
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


def test_import_mapping_is_not_written_yet(legacy_sqlite: Path, capsys):
    assert import_legacy.main(["--import", str(legacy_sqlite)]) == 2
    assert "Заполните маппинг после --inspect" in capsys.readouterr().err


def test_commit_only_with_import(legacy_sqlite: Path):
    with pytest.raises(SystemExit):
        import_legacy.main(["--inspect", str(legacy_sqlite), "--commit"])


# --- import_legacy write path (used once the MAPPING is filled in) --------------------------------


def _profile(**overrides) -> dict:
    data = {
        "gender": "female",
        "name": "Марьям",
        "birth_year": 1995,
        "city": "Казань",
        "marital_status": "never_married",
        "children": 0,
        "education": "higher",
        "employment": "employed",
        "wishes": "Ищу верующего мужа для создания семьи.",
    }
    return {**data, **overrides}


async def test_import_records(session, make_user):
    existing = await make_user(telegram_id=1001, first_name="Уже здесь")
    consent = datetime(2025, 1, 1, tzinfo=UTC)
    records = [
        LegacyRecord(telegram_id=1001, first_name="Перезапись", consent_at=consent, profile=_profile()),
        LegacyRecord(telegram_id=1002, username="maryam", first_name="Марьям", consent_at=consent, profile=_profile()),
        LegacyRecord(telegram_id=1003, consent_at=None, profile=_profile()),
        LegacyRecord(telegram_id=1004, consent_at=consent, profile=_profile(about="пишите @zarema_secret")),
        LegacyRecord(telegram_id=1005, consent_at=consent, profile=_profile(about="Помогу с инвестициями")),
    ]
    report = await import_records(session, records)
    await session.commit()

    assert (report.users_created, report.users_existing, report.skipped_no_consent) == (3, 1, 1)
    assert report.profiles_created == 2 and dict(report.profiles_invalid) == {"about": 1}
    text = report.format(committed=True)
    assert "Анкет перенесено: 2" in text and "about: 1" in text
    _assert_no_secrets(text)

    assert (await _fresh(session, existing.id)).first_name == "Уже здесь"
    imported = {user.telegram_id: user for user in await session.scalars(select(User).where(User.telegram_id > 1001))}
    assert set(imported) == {1002, 1004, 1005}
    assert imported[1002].consent_at == consent and imported[1002].username == "maryam"
    profiles = {p.user_id: p for p in await session.scalars(select(Profile))}
    assert set(profiles) == {imported[1002].id, imported[1005].id}
    assert all(p.status == ProfileStatus.PENDING and p.submitted_at for p in profiles.values())
    assert profiles[imported[1005].id].flagged and not profiles[imported[1002].id].flagged
