"""scripts/import_legacy.py --import: the legacy SQLite database -> the new database (LEGACY_BOT.md §4).

Every row is fabricated. Telegram ids are 9-digit numbers that never coincide with the counters of
the report, so the CLI output can be searched for them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app.enums import ConversationCloseReason, Gender, InterestStatus, MessageKind, MessageOrigin, ProfileStatus
from app.models import AppMeta, Base, Conversation, Interest, Message, Profile, User
from app.schemas import birth_year_bounds
from app.services import interests, meta, profiles, users
from app.services.errors import OpenConversationLimit
from app.services.moderation import LEGACY_AGE_FLAG_REASON
from scripts import import_legacy
from scripts.import_legacy import ImportRefused, SourceError, Warn, run_import

ROOT = Path(__file__).resolve().parents[2]

ADMIN, BROTHER, SISTER = 700100400, 700100200, 700100300
# Fabricated personal data: none of it may appear in the script's output.
PERSONAL = (
    "admin_secret",
    "brother_secret",
    "sister_secret",
    "Зеленодольск",
    "Нурлат",
    "Тайный текст переписки",
    "Пожелания брата к супруге",
    str(ADMIN),
    str(BROTHER),
    str(SISTER),
)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


async def _users(session) -> dict[int, User]:
    result = await session.scalars(
        select(User).options(selectinload(User.profile)).execution_options(populate_existing=True)
    )
    return {user.telegram_id: user for user in result}


async def _by_id(session, model) -> dict[int, Any]:
    key = model.user_id if model is Profile else model.id
    result = await session.scalars(select(model).order_by(key).execution_options(populate_existing=True))
    return {row.number if model is Profile else row.id: row for row in result}


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


def _basic(legacy) -> dict[str, int]:
    """A brother and a sister who talked once; the chat is closed."""
    legacy.user(ADMIN, "admin_secret", role="admin", created_at="2024-12-31 23:00:00")
    legacy.user(BROTHER, "brother_secret", created_at="2025-02-01 08:00:00")
    brother = legacy.profile(
        BROTHER,
        with_user=False,
        age=30,
        city="Зеленодольск",
        nationality="татарин",
        about="О себе брата",
        wishes="Пожелания брата к супруге",
        created_at="2025-03-01 10:00:00",
        updated_at="2025-03-02 11:00:00",
    )
    sister = legacy.profile(
        SISTER,
        gender="f",
        age=24,
        city="Нурлат",
        nationality="  ",
        about="О себе сестры",
        wishes="Пожелания сестры к супругу",
        status="pending",
        created_at="2025-03-05 07:30:00",
        updated_at="2025-03-05 07:30:00",
    )
    interest = legacy.interest(brother, sister, "accepted", "2025-04-01 09:00:00")
    chat = legacy.chat(brother, sister, "2025-04-02 10:00:00", "2025-04-03 18:00:00")
    first = legacy.message(chat, brother, "Тайный текст переписки", "2025-04-02 10:05:00")
    second = legacy.message(
        chat,
        sister,
        "Ва алейкум ассалям [скрыто свидетелем]",
        "2025-04-02 10:06:00",
        flagged=True,
        note="contact-leak filter",
    )
    return {"brother": brother, "sister": sister, "interest": interest, "chat": chat, "first": first, "second": second}


# --- mapping --------------------------------------------------------------------------------------


async def test_maps_every_field(session, legacy):
    ids = _basic(legacy)

    report = await run_import(str(legacy.path), commit=True)

    people = await _users(session)
    assert set(people) == {ADMIN, BROTHER, SISTER}
    admin, brother, sister = people[ADMIN], people[BROTHER], people[SISTER]
    assert admin.is_admin and not brother.is_admin and not sister.is_admin
    assert (brother.username, sister.username, admin.username) == ("brother_secret", None, "admin_secret")
    assert brother.created_at == utc("2025-02-01 08:00:00") and admin.created_at == utc("2024-12-31 23:00:00")
    assert all(user.consent_at is None and user.first_name is None and not user.is_banned for user in people.values())
    assert users.needs_consent(brother)

    by_number = await _by_id(session, Profile)
    assert set(by_number) == {ids["brother"], ids["sister"]} == {500, 501}
    man, woman = by_number[500], by_number[501]
    assert (man.user_id, woman.user_id) == (brother.id, sister.id)
    assert (man.gender, woman.gender) == (Gender.MALE, Gender.FEMALE)
    assert (man.birth_year, woman.birth_year) == (1995, 2001)  # year(created_at) − age
    assert (man.city, man.nationality, man.about, man.wishes) == (
        "Зеленодольск",
        "татарин",
        "О себе брата",
        "Пожелания брата к супруге",
    )
    assert woman.nationality is None  # blank optional text -> NULL
    assert (man.status, woman.status) == (ProfileStatus.ACTIVE, ProfileStatus.PENDING)
    assert man.submitted_at == man.created_at == utc("2025-03-01 10:00:00")
    assert man.approved_at == man.updated_at == utc("2025-03-02 11:00:00")
    assert woman.approved_at is None and woman.submitted_at == utc("2025-03-05 07:30:00")
    for profile in (man, woman):
        assert profiles.missing_fields(profile) == ["marital_status", "children", "education", "employment"]
        assert not profile.flagged and profile.flag_reason is None and profile.moderation_note is None
        assert profile.height is None and profile.occupation is None

    [interest] = (await _by_id(session, Interest)).values()
    assert interest.id == ids["interest"]
    assert (interest.from_user_id, interest.to_user_id) == (brother.id, sister.id)
    assert interest.status == InterestStatus.ACCEPTED and interest.created_at == utc("2025-04-01 09:00:00")
    assert interest.message is None and interest.decided_at is None and not interest.flagged

    [conversation] = (await _by_id(session, Conversation)).values()
    assert conversation.id == ids["chat"]
    assert (conversation.user_a_id, conversation.user_b_id) == tuple(sorted((brother.id, sister.id)))
    assert conversation.created_at == utc("2025-04-02 10:00:00")
    assert conversation.closed_at == utc("2025-04-03 18:00:00")
    assert conversation.close_reason == ConversationCloseReason.CLOSED_BY_USER and conversation.closed_by_id is None
    assert conversation.created_from_interest_id == ids["interest"]
    assert conversation.last_message_at == utc("2025-04-02 10:06:00")

    messages = await _by_id(session, Message)
    assert list(messages) == [ids["first"], ids["second"]]
    first, second = messages[ids["first"]], messages[ids["second"]]
    assert (first.sender_id, second.sender_id) == (brother.id, sister.id)
    assert first.text == "Тайный текст переписки" and first.conversation_id == ids["chat"]
    for message in (first, second):
        assert message.kind == MessageKind.USER and message.origin == MessageOrigin.TELEGRAM
        assert message.read_at == message.created_at
    assert first.created_at == utc("2025-04-02 10:05:00") and not first.flagged and first.flag_reason is None
    assert second.flagged and second.flag_reason == "contact-leak filter" and second.contacts_hidden

    assert report.imported == {"users": 3, "profiles": 2, "interests": 1, "chats": 1, "messages": 2}
    assert report.verified and report.merged_users == 0 and not report.warnings
    marker = await meta.legacy_import_marker(session)
    assert marker["counts"] == {"users": 3, "profiles": 2, "interests": 1, "conversations": 1, "messages": 2}


async def test_statuses_and_out_of_range_ages(session, legacy):
    numbers = {
        status: legacy.profile(700000010 + index, status=status)
        for index, status in enumerate(("approved", "pending", "rejected", "paused"))
    }
    oldest, youngest = birth_year_bounds()
    minor = legacy.profile(700000020, age=2025 - (youngest + 1), created_at="2025-06-01 10:00:00")
    elder = legacy.profile(700000021, age=2025 - (oldest - 1), created_at="2025-06-01 10:00:00", status="paused")
    absurd = legacy.profile(700000022, age=100000, created_at="2025-06-01 10:00:00")

    report = await run_import(str(legacy.path), commit=True)

    by_number = await _by_id(session, Profile)
    expected = {
        "approved": ProfileStatus.ACTIVE,
        "pending": ProfileStatus.PENDING,
        "rejected": ProfileStatus.REJECTED,
        "paused": ProfileStatus.HIDDEN,
    }
    for legacy_status, number in numbers.items():
        profile = by_number[number]
        assert profile.status == expected[legacy_status]
        assert (profile.approved_at is not None) is (legacy_status == "approved")
        assert not profile.flagged
    # A possibly underage or implausible age: clamped into 18–80, flagged, never published unchecked.
    assert by_number[minor].birth_year == youngest and by_number[absurd].birth_year == oldest
    assert by_number[elder].birth_year == oldest
    for number in (minor, absurd):
        assert by_number[number].status == ProfileStatus.PENDING and by_number[number].approved_at is None
    assert by_number[elder].status == ProfileStatus.HIDDEN
    assert all(
        by_number[n].flagged and by_number[n].flag_reason == LEGACY_AGE_FLAG_REASON for n in (minor, elder, absurd)
    )
    assert report.warnings == {Warn.PROFILE_AGE: 3}


async def test_witness_notes_become_codes_without_telegram_ids(session, legacy):
    brother = legacy.profile(BROTHER)
    sister = legacy.profile(SISTER, gender="f")
    legacy.interest(brother, sister, "accepted", "2025-05-01 09:00:00")
    chat = legacy.chat(brother, sister, "2025-05-01 10:00:00")
    notes = {
        "contact-leak filter": "contact-leak filter",
        "user_report": "user report",
        f"flagged by {ADMIN}": "witness",
        "проверить, похоже на обман": "witness",
        None: None,
    }
    ids = {
        note: legacy.message(
            chat, brother, f"Сообщение {index}", f"2025-05-01 10:0{index}:00", flagged=note is not None, note=note
        )
        for index, note in enumerate(notes)
    }

    report = await run_import(str(legacy.path), commit=False)
    assert report.warnings == {Warn.MESSAGE_UNKNOWN_NOTE: 1}
    await run_import(str(legacy.path), commit=True)

    messages = await _by_id(session, Message)
    for note, reason in notes.items():
        message = messages[ids[note]]
        assert message.flag_reason == reason and message.flagged is (note is not None)
    assert not any(str(ADMIN) in (message.flag_reason or "") for message in messages.values())


async def test_every_legacy_state(session, legacy):
    """States the legacy bot can produce (handlers/browse.py, chat.py; FKs are not enforced)."""
    legacy.user(ADMIN, role="admin")
    a = legacy.profile(700000001)  # brother
    b = legacy.profile(700000002, gender="f", about="о" * 2500, wishes="Да")  # texts beyond today's limits
    c = legacy.profile(700000003, gender="f", city="Г" * 70)  # longer than the column: cut
    d = legacy.profile(700000004)
    legacy.user(700000005, banned=True)
    e = legacy.profile(700000005, gender="f", with_user=False)
    orphan = legacy.profile(700000006, with_user=False)  # its user row is missing
    legacy.execute(
        "INSERT INTO muted (user_id, until, reason)"
        " VALUES (700000001, '2030-01-01', 'флуд'), (700000002, '2030-01-01', NULL)"
    )

    # A and B: interests both ways, accepted one after the other -> two chats, the first closed.
    a_to_b = legacy.interest(a, b, "accepted", "2025-04-01 09:00:00")
    chat_1 = legacy.chat(a, b, "2025-04-01 12:00:00", "2025-04-10 12:00:00")
    b_to_a = legacy.interest(b, a, "accepted", "2025-04-11 09:00:00")
    chat_2 = legacy.chat(a, b, "2025-04-12 12:00:00")
    # C and D: both directions pending at once.
    d_to_c = legacy.interest(d, c, "pending", "2025-05-01 09:00:00")
    c_to_d = legacy.interest(c, d, "pending", "2025-05-01 10:00:00")
    # A also has an open chat with C (accepted C -> A): A has two open chats.
    c_to_a = legacy.interest(c, a, "accepted", "2025-05-02 09:00:00")
    chat_3 = legacy.chat(a, c, "2025-05-02 12:00:00")
    # D and B talked without any accepted interest (closed).
    chat_4 = legacy.chat(d, b, "2025-05-03 12:00:00", "2025-05-04 12:00:00")
    # D and E: two open chats of one pair (a race in the legacy accept) -> the older is closed.
    d_to_e = legacy.interest(d, e, "declined", "2025-05-05 09:00:00")
    chat_5 = legacy.chat(d, e, "2025-05-06 12:00:00")
    chat_6 = legacy.chat(d, e, "2025-05-07 12:00:00")
    # References to rows that do not exist (or were skipped).
    legacy.interest(a, 999, "pending", "2025-05-08 09:00:00")
    legacy.interest(orphan, b, "pending", "2025-05-08 09:00:00")
    legacy.chat(d, 998, "2025-05-09 12:00:00")
    m_1 = legacy.message(chat_1, a, "Ассаляму алейкум", "2025-04-01 12:05:00")
    m_2 = legacy.message(chat_2, b, "я" * 2100, "2025-04-12 12:05:00")  # longer than 2000: kept as is
    m_3 = legacy.message(chat_3, c, "Здравствуйте", "2025-05-02 12:05:00")
    legacy.message(77, a, "в несуществующий чат", "2025-05-02 12:06:00")
    legacy.message(chat_3, d, "от чужой анкеты", "2025-05-02 12:07:00")

    report = await run_import(str(legacy.path), commit=True)

    assert report.source == {"users": 6, "profiles": 6, "interests": 8, "chats": 7, "messages": 5, "muted": 2}
    assert report.imported == {"users": 6, "profiles": 5, "interests": 6, "chats": 6, "messages": 3}
    assert report.warnings == {
        Warn.PROFILE_WITHOUT_USER: 1,
        Warn.PROFILE_TEXT_LIMITS: 2,
        Warn.TRUNCATED: 1,
        Warn.INTEREST_MISSING_PROFILE: 2,
        Warn.CHAT_MISSING_PROFILE: 1,
        Warn.CHAT_EXTRA_OPEN: 1,
        Warn.CHAT_NO_INTEREST: 3,
        Warn.PERSON_SEVERAL_OPEN: 1,
        Warn.BANNED_ACTIVE: 1,
        Warn.MESSAGE_MISSING_CHAT: 1,
        Warn.MESSAGE_FOREIGN_SENDER: 1,
        Warn.MESSAGE_TOO_LONG: 1,
        Warn.MUTED: 2,
    }

    people = await _users(session)
    uid = {number: people[700000000 + index].id for index, number in enumerate((a, b, c, d, e), start=1)}
    assert people[ADMIN].is_admin and people[ADMIN].profile is None
    assert people[700000005].is_banned and 700000006 not in people  # the orphan profile had no user row
    by_number = await _by_id(session, Profile)
    assert set(by_number) == {a, b, c, d, e}
    assert len(by_number[b].about) == 2500 and by_number[b].wishes == "Да"
    assert by_number[c].city == "Г" * 60

    rows = await _by_id(session, Interest)
    assert set(rows) == {a_to_b, b_to_a, d_to_c, c_to_d, c_to_a, d_to_e}
    assert {(rows[i].from_user_id, rows[i].to_user_id, rows[i].status) for i in (d_to_c, c_to_d)} == {
        (uid[d], uid[c], InterestStatus.PENDING),
        (uid[c], uid[d], InterestStatus.PENDING),
    }
    assert rows[d_to_e].status == InterestStatus.DECLINED

    conversations = await _by_id(session, Conversation)
    assert set(conversations) == {chat_1, chat_2, chat_3, chat_4, chat_5, chat_6}
    for conversation in conversations.values():
        assert conversation.user_a_id < conversation.user_b_id
    assert conversations[chat_1].created_from_interest_id == a_to_b and conversations[chat_1].closed_at is not None
    assert conversations[chat_2].created_from_interest_id == b_to_a and conversations[chat_2].is_open
    assert conversations[chat_3].created_from_interest_id == c_to_a and conversations[chat_3].is_open
    assert conversations[chat_4].created_from_interest_id is None and conversations[chat_4].last_message_at is None
    assert conversations[chat_5].closed_at == utc("2025-05-07 12:00:00")  # when the newer one opened
    assert conversations[chat_5].close_reason == ConversationCloseReason.CLOSED_BY_USER
    assert conversations[chat_6].is_open and conversations[chat_5].created_from_interest_id is None

    messages = await _by_id(session, Message)
    assert set(messages) == {m_1, m_2, m_3}
    assert len(messages[m_2].text) == 2100 and messages[m_2].sender_id == uid[b]


# --- sequences ------------------------------------------------------------------------------------


async def test_numbers_and_ids_continue_without_reuse(session, legacy, make_profile):
    man = legacy.profile(BROTHER)
    woman = legacy.profile(SISTER, gender="f")
    interest = legacy.interest(man, woman, "accepted")
    chat = legacy.chat(man, woman, "2025-04-02 10:00:00")
    message = legacy.message(chat, man, "Ассаляму алейкум", "2025-04-02 10:05:00")
    # Rows deleted in the legacy bot: AUTOINCREMENT never hands out their ids again.
    for table, seq in (("profiles", 512), ("interests", 40), ("chats", 9), ("messages", 120)):
        legacy.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
    legacy.execute(
        "DELETE FROM sqlite_sequence WHERE rowid NOT IN (SELECT max(rowid) FROM sqlite_sequence GROUP BY name)"
    )
    assert (man, woman, interest, chat, message) == (500, 501, 1, 1, 1)

    await run_import(str(legacy.path), commit=True)

    assert (await make_profile(gender=Gender.FEMALE)).number == 513
    people = await _users(session)
    new_interest = Interest(from_user_id=people[SISTER].id, to_user_id=people[BROTHER].id)
    new_conversation = Conversation(
        user_a_id=min(p.id for p in people.values()), user_b_id=max(p.id for p in people.values())
    )
    session.add_all([new_interest, new_conversation])
    await session.flush()
    new_message = Message(
        conversation_id=new_conversation.id, sender_id=people[BROTHER].id, text="Новое", origin=MessageOrigin.WEB
    )
    session.add(new_message)
    await session.flush()
    assert (new_interest.id, new_conversation.id, new_message.id) == (41, 10, 121)
    await session.rollback()


async def test_without_gaps_the_next_number_is_max_plus_one(session, legacy, make_profile):
    legacy.profile(BROTHER)
    legacy.profile(SISTER, gender="f")
    await run_import(str(legacy.path), commit=True)
    assert (await make_profile()).number == 502


async def test_imported_people_keep_working_with_the_services(session, legacy):
    """A migrated sister consents, then answers pending legacy interests under today's rules."""
    first = legacy.profile(700000001)
    second = legacy.profile(700000002)
    sister = legacy.profile(SISTER, gender="f")
    pending_1 = legacy.interest(first, sister, "pending", "2025-04-01 09:00:00")
    pending_2 = legacy.interest(second, sister, "pending", "2025-04-01 10:00:00")
    legacy.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES ('chats', 30)")
    await run_import(str(legacy.path), commit=True)

    user = await users.get_by_telegram_id(session, SISTER)
    assert users.needs_consent(user)
    await users.set_consent(session, user)
    conversation = await interests.accept(session, pending_1, user)
    assert conversation.id == 31  # continues after the legacy chats
    with pytest.raises(OpenConversationLimit):  # one open chat per person, as in the legacy bot
        await interests.accept(session, pending_2, user)
    await session.rollback()


# --- dry run, refusals, re-import, admins ---------------------------------------------------------


async def _snapshot(session) -> dict[str, Any]:
    counts = {
        table.name: await session.scalar(text(f"SELECT count(*) FROM {table.name}"))
        for table in Base.metadata.sorted_tables
    }
    sequences = {
        name: tuple((await session.execute(text(f"SELECT last_value, is_called FROM {name}"))).one())
        for name in ("profiles_number_seq", "interests_id_seq", "conversations_id_seq", "messages_id_seq")
    }
    people = sorted(
        tuple(row)
        for row in await session.execute(select(User.telegram_id, User.username, User.is_admin, User.created_at))
    )
    await session.rollback()
    return {"counts": counts, "sequences": sequences, "users": people}


async def test_dry_run_changes_nothing(session, legacy, make_user):
    _basic(legacy)
    await make_user(telegram_id=BROTHER, username=None, is_admin=True)
    before = await _snapshot(session)

    report = await run_import(str(legacy.path), commit=False)

    assert report.verified and report.imported["messages"] == 2 and report.merged_users == 1
    assert "ПРОБНЫЙ ПРОГОН" in report.format(committed=False)
    assert await _snapshot(session) == before
    assert await meta.legacy_import_marker(session) is None
    await run_import(str(legacy.path), commit=True)  # a dry run does not block the real import
    assert await _count(session, Message) == 2


async def test_refuses_a_target_with_profiles(session, legacy, make_profile):
    _basic(legacy)
    await make_profile()
    users_before = await _count(session, User)
    await session.rollback()

    with pytest.raises(ImportRefused, match="profiles: 1"):
        await run_import(str(legacy.path), commit=True)

    assert await _count(session, User) == users_before
    assert await _count(session, AppMeta) == 0


async def test_refuses_a_target_with_conversations(session, legacy, make_pair, make_conversation):
    _basic(legacy)
    man, woman = await make_pair()
    await make_conversation(man, woman)
    with pytest.raises(ImportRefused, match="interests: 1, conversations: 1"):
        await run_import(str(legacy.path), commit=False)


async def test_second_import_is_refused(session, legacy):
    _basic(legacy)
    await run_import(str(legacy.path), commit=True)
    with pytest.raises(ImportRefused, match="уже выполнен"):
        await run_import(str(legacy.path), commit=True)


async def test_marker_alone_blocks_the_import(session, legacy):
    _basic(legacy)
    await meta.mark_legacy_import(session, {"users": 0})
    await session.commit()
    with pytest.raises(ImportRefused, match="уже выполнен"):
        await run_import(str(legacy.path), commit=True)
    assert await _count(session, Profile) == 0


async def test_existing_accounts_are_merged_and_keep_admin_rights(session, legacy, make_user, override_settings):
    env_admin = 700000077
    override_settings(ADMIN_TELEGRAM_IDS=[env_admin])
    legacy.user(ADMIN, "admin_secret", created_at="2024-06-01 08:00:00")  # role 'user' in the old bot
    legacy.user(env_admin, "env_admin")
    legacy.user(700000088, role="admin", banned=True)
    legacy.profile(BROTHER, created_at="2025-03-01 10:00:00")
    existing = await make_user(telegram_id=ADMIN, username=None, first_name="Модератор", is_admin=True)
    member = await make_user(telegram_id=BROTHER, username="new_name", consent=True)
    consent_at = member.consent_at

    report = await run_import(str(legacy.path), commit=True)

    assert report.merged_users == 2 and report.imported["users"] == 4
    assert report.warnings == {Warn.ADMIN_BANNED: 1}
    people = await _users(session)
    assert len(people) == 4
    admin = people[ADMIN]
    assert admin.id == existing.id and admin.is_admin and admin.first_name == "Модератор"
    assert admin.username == "admin_secret" and admin.created_at == utc("2024-06-01 08:00:00")
    merged = people[BROTHER]
    assert merged.id == member.id and merged.username == "new_name" and merged.consent_at == consent_at
    assert merged.profile.number == 500
    assert people[env_admin].is_admin  # listed in ADMIN_TELEGRAM_IDS
    assert people[700000088].is_admin and not people[700000088].is_banned  # an admin is never banned


# --- the command line: output, read-only source, errors -------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.import_legacy", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=os.environ.copy(),  # DATABASE_URL of the test database (tests/conftest.py)
        timeout=120,
        check=False,
    )


def _assert_no_personal_data(output: str) -> None:
    for value in PERSONAL:
        assert value not in output


async def test_cli_prints_only_counters_and_never_touches_the_source(session, legacy):
    ids = _basic(legacy)
    legacy.message(
        ids["chat"],
        ids["brother"],
        "Тайный текст переписки ещё раз",
        "2025-04-02 10:07:00",
        flagged=True,
        note=f"flagged by {ADMIN}",
    )
    legacy.path.chmod(0o444)
    digest = legacy.digest()

    dry = _cli("--import", str(legacy.path))
    assert dry.returncode == 0, dry.stderr
    assert "ПРОБНЫЙ ПРОГОН" in dry.stdout and "messages → messages" in dry.stdout
    assert await _count(session, Profile) == 0

    real = _cli("--import", str(legacy.path), "--commit")
    assert real.returncode == 0, real.stderr
    assert "Перенос записан в базу" in real.stdout
    again = _cli("--import", str(legacy.path), "--commit")
    assert again.returncode == 1 and "уже выполнен" in again.stderr

    for result in (dry, real, again):
        _assert_no_personal_data(result.stdout + result.stderr)
    assert legacy.digest() == digest  # opened read-only (mode=ro), never written
    assert await _count(session, Message) == 3


def test_cli_rejects_other_databases(tmp_path: Path, legacy):
    other = tmp_path / "other.db"
    import sqlite3

    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    result = _cli("--import", str(other))
    assert result.returncode == 1 and "это не база старого бота" in result.stderr

    legacy.user(BROTHER, created_at="вчера вечером")
    broken = _cli("--import", str(legacy.path))
    assert broken.returncode == 1 and "users.created_at: 1" in broken.stderr
    assert "вчера" not in broken.stderr

    postgres = _cli("--import", "postgresql://nikah:nikah@127.0.0.1:5432/nikah")
    assert postgres.returncode == 1 and "только файл SQLite" in postgres.stderr


async def test_bad_timestamp_is_refused_before_anything_is_written(session, legacy):
    _basic(legacy)
    legacy.execute("UPDATE messages SET sent_at = 'не дата' WHERE id = 1")
    with pytest.raises(SourceError, match=r"messages\.sent_at: 1"):
        await run_import(str(legacy.path), commit=True)
    assert await _count(session, User) == 0


def test_parse_time_reads_sqlite_text_as_utc():
    assert import_legacy.parse_time("2025-04-02 10:05:00") == utc("2025-04-02 10:05:00")
    assert import_legacy.parse_time("2025-04-02T13:05:00+03:00") == utc("2025-04-02 10:05:00")
    assert import_legacy.parse_time(0) == datetime(1970, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        import_legacy.parse_time("вчера")


def test_database_errors_are_reported_without_values(legacy, monkeypatch, capsys):
    from sqlalchemy.exc import IntegrityError

    class UniqueViolation(Exception):
        constraint_name = "uq_users_telegram_id"

    async def failing(path: str, *, commit: bool) -> str:
        raise IntegrityError("INSERT INTO users ...", None, UniqueViolation(f"DETAIL: Key (telegram_id)=({BROTHER})"))

    monkeypatch.setattr(import_legacy, "_import_cli", failing)
    assert import_legacy.main(["--import", str(legacy.path), "--commit"]) == 1
    captured = capsys.readouterr()
    assert "IntegrityError, ограничение uq_users_telegram_id" in captured.err
    _assert_no_personal_data(captured.out + captured.err)
