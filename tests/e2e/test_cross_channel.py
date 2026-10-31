"""End-to-end check that the site and the Telegram bot are one system (spec: «Сайт и бот — одна
система с одной базой данных») and that the legacy bot's users arrive intact, with real browsers
on a running server.

Skipped unless ``E2E=1``. Needs Node with Playwright and a free port (8000, or ``E2E_PORT``)::

    E2E=1 E2E_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah_e2e_test \\
        TEST_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/<test_db> \\
        .venv/bin/pytest -q tests/e2e

The runner **empties** the e2e database (``E2E_DATABASE_URL``, its name must contain "test"),
migrates it, imports a synthetic legacy ``data.db`` with ``scripts/import_legacy.py --commit``
(``tests/e2e/prepare.py``), starts uvicorn with ENV=dev, DEV_LOGIN_ENABLED=true, PREMODERATION=true
and WITNESS_CHAT_ID set, and runs ``cross_channel.js``. That script drives the browsers and calls
``telegram_side.py`` for the bot's side (real handlers and outbox worker, fake Bot API). Screenshots
are saved to docs/screenshots/e2e-*.png. The data stays in the database afterwards for inspection.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("E2E") != "1", reason="browser end-to-end test: set E2E=1")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("cross_channel.js")
SCREENSHOTS = ROOT / "docs" / "screenshots"
DATABASE_URL = os.environ.get("E2E_DATABASE_URL", "postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah_e2e_test")
HOST, PORT = "127.0.0.1", int(os.environ.get("E2E_PORT", "8000"))
BASE_URL = f"http://{HOST}:{PORT}"
# Telegram ids of the people created during the run (the legacy ones come from prepare.py).
ACCOUNTS = {"brother": 9_200_000_001, "sister": 9_200_000_002, "wali": 9_200_000_003, "admin": 9_200_000_004}
WITNESS_CHAT_ID = -1_009_200_000_000  # the witness group
WITNESS_BUTTONS = ["⚠️ Пометить", "✍️ Написать в чат", "🚫 Закрыть чат"]
PLACEHOLDER = "[скрыто свидетелем]"
OPEN_CHAT_LIMIT_TEXT = "У одного из участников уже есть открытый чат. Завершите его сначала."


def _app_env() -> dict[str, str]:
    """Environment of the server and of the Telegram side: the same database and settings."""
    env = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    env.update(
        DATABASE_URL=DATABASE_URL,
        ENV="dev",
        DEV_LOGIN_ENABLED="true",
        PREMODERATION="true",
        INTEREST_DAILY_LIMIT="10",
        MAX_OPEN_CONVERSATIONS="1",
        CHAT_RATE_LIMIT_PER_MIN="20",
        SITE_URL=BASE_URL,
        SECRET_KEY="",  # the dev key
        TELEGRAM_TOKEN="",
        TELEGRAM_WIDGET_ENABLED="false",
        SESSION_COOKIE_SECURE="false",
        ADMIN_TELEGRAM_IDS=str(ACCOUNTS["admin"]),
        WITNESS_CHAT_ID=str(WITNESS_CHAT_ID),
        PYTHONUNBUFFERED="1",
    )
    return env


def _run(args: list[str], env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def _run_ok(args: list[str], env: dict[str, str]) -> str:
    done = _run(args, env)
    assert done.returncode == 0, f"{' '.join(args[1:])} failed:\n{done.stdout}\n{done.stderr}"
    return done.stdout


def _port_is_free() -> bool:
    with socket.socket() as sock:
        return sock.connect_ex((HOST, PORT)) != 0


def _wait_healthy(server: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("uvicorn exited during startup")
        try:
            with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError("uvicorn did not become healthy in time")


def _import_counts(report: str) -> dict[str, tuple[int, int, int]]:
    """Rows «old → new  in-legacy  imported  skipped» of the importer's report."""
    return {
        match[1]: (int(match[2]), int(match[3]), int(match[4]))
        for match in re.finditer(r"^(\w+) → \w+\s+(\d+)\s+(\d+)\s+(\d+)$", report, re.MULTILINE)
    }


@pytest.fixture
def server(tmp_path: Path) -> Iterator[dict[str, str]]:
    if shutil.which("node") is None:
        pytest.fail("node is required for the browser end-to-end test")
    if not _port_is_free():
        pytest.fail(f"port {PORT} is busy: stop the dev server first (or set E2E_PORT)")
    env = _app_env()
    python = sys.executable
    _run_ok([python, "-m", "tests.e2e.prepare", "wipe"], env)
    _run_ok([python, "-m", "alembic", "upgrade", "head"], env)
    legacy = json.loads(_run_ok([python, "-m", "tests.e2e.prepare", "legacy", str(tmp_path / "data.db")], env))
    report = _run_ok([python, "-m", "scripts.import_legacy", "--import", legacy["path"], "--commit"], env)
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                *(python, "-m", "uvicorn", "app.web.main:app"),
                *("--host", HOST, "--port", str(PORT), "--timeout-graceful-shutdown", "3"),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_healthy(process)
            yield env | {"E2E_SERVER_LOG": str(log_path), "E2E_LEGACY": json.dumps(legacy), "E2E_IMPORT": report}
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _browser_run(env: dict[str, str]) -> dict[str, Any]:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    done = _run(
        ["node", str(SCRIPT)],
        env
        | {
            "E2E_BASE_URL": BASE_URL,
            "E2E_PYTHON": sys.executable,
            "E2E_ROOT": str(ROOT),
            "E2E_SCREENSHOTS": str(SCREENSHOTS),
            "E2E_ACCOUNTS": json.dumps(ACCOUNTS),
        },
        timeout=300,
    )
    if done.returncode != 0:
        server_log = Path(env["E2E_SERVER_LOG"]).read_text()[-4000:]
        pytest.fail(f"browser run failed:\n{done.stderr[-6000:]}\n--- uvicorn log ---\n{server_log}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _to(drain: dict[str, Any], chat_id: int) -> list[dict[str, Any]]:
    return [item for item in drain["deliveries"] if item["chat_id"] == chat_id]


def _kinds(drain: dict[str, Any], *user_ids: int) -> list[str]:
    """Kinds of the processed outbox rows that concern these users (old rows may be drained too)."""
    return sorted(row["kind"] for row in drain["rows"] if set(user_ids) & set(row["payload"].get("user_ids", [])))


def _rows(drain: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [row for row in drain["rows"] if row["kind"] == kind]


def _copies(drain: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Witness copies in the witness group that carry ``text``."""
    return [item for item in _to(drain, WITNESS_CHAT_ID) if item["text"].startswith("Чат #") and text in item["text"]]


def test_site_and_bot_stay_in_sync(server: dict[str, str]) -> None:
    legacy = json.loads(server["E2E_LEGACY"])

    # The legacy import: every row moved, nothing skipped (the report holds counters only).
    counts = _import_counts(server["E2E_IMPORT"])
    assert counts == {
        "users": (2, 2, 0),
        "profiles": (2, 2, 0),
        "interests": (1, 1, 0),
        "chats": (1, 1, 0),
        "messages": (3, 3, 0),
    }, server["E2E_IMPORT"]
    assert "ПРОБНЫЙ ПРОГОН" not in server["E2E_IMPORT"]
    for telegram_id in legacy["telegramIds"].values():
        assert str(telegram_id) not in server["E2E_IMPORT"]

    result = _browser_run(server)
    brother, sister = result["brother"]["number"], result["sister"]["number"]
    conversation = result["conversationId"]
    texts = result["texts"]
    tg_brother, tg_sister, tg_wali, tg_admin = (ACCOUNTS[key] for key in ("brother", "sister", "wali", "admin"))
    drains = result["drains"]
    assert all(drain["unsent_row_ids"] == [] for drain in drains.values())

    # New profiles continue the legacy numbering past the old sequence: no old number is reused.
    assert min(brother, sister) > legacy["sequence"]

    # New profiles go to the admin in Telegram with moderation buttons, the decision to the authors.
    cards = _to(drains["submitted"], tg_admin)
    for number in (brother, sister):
        assert any(
            "Новая анкета на модерацию" in card["text"] and f"Анкета №{number}</b>" in card["text"] for card in cards
        ), cards
    assert all({"Одобрить", "Отклонить"} <= set(card["buttons"]) for card in cards), cards
    moderated = drains["moderated"]
    assert [row["kind"] for row in moderated["rows"]] == ["profile.moderated"] * 2
    assert [item["text"] for item in _to(moderated, tg_brother)] == [
        f"Ваша анкета №{brother} одобрена. Теперь вы можете смотреть анкеты и отправлять отклики."
    ]
    assert len(_to(moderated, tg_sister)) == 1

    # The interest sent on the site reaches her in Telegram as the legacy card with «Принять» / «Отклонить».
    [card] = _to(drains["interest"], tg_sister)
    assert f"Вами заинтересовалась анкета №{brother}" in card["text"] and texts["interestText"] in card["text"]
    assert card["buttons"] == ["Принять", "Отклонить"]

    # Acceptance, his site message and her Telegram reply: each side is notified about the other only,
    # in the legacy relay format; the witness group gets the chat opening and exactly one copy of each
    # message, from both channels, with the witness buttons.
    chat = drains["chat"]
    assert _kinds(chat, result["brother"]["userId"], result["sister"]["userId"]) == [
        "interest.accepted",
        "message.created",
        "message.created",
    ]
    to_brother, to_sister = _to(chat, tg_brother), _to(chat, tg_sister)
    [opened] = [item for item in to_brother if "Чат открыт между анкетами" in item["text"]]
    assert f"№{sister} и №{brother}." in opened["text"]  # the legacy wording, the acceptor first
    [reply] = [item for item in to_brother if texts["telegramText"] in item["text"]]
    assert reply["text"] == f"<b>Анкета №{sister} пишет:</b>\n{texts['telegramText']}"
    assert reply["buttons"] == ["Пожаловаться"]  # one open chat: no «Ответить» needed
    [site_message] = [item for item in to_sister if texts["siteText"] in item["text"]]
    assert site_message["text"] == f"<b>Анкета №{brother} пишет:</b>\n{texts['siteText']}"
    assert not any(texts["telegramText"] in item["text"] for item in to_sister)  # never echoed to the sender
    assert not any(texts["siteText"] in item["text"] for item in to_brother)
    group = _to(chat, WITNESS_CHAT_ID)
    assert f"Открыт чат #{conversation}: №{brother} ↔ №{sister}." in [item["text"] for item in group]
    [site_copy] = _copies(chat, texts["siteText"])
    assert site_copy["text"] == f"Чат #{conversation} | №{brother} → №{sister} (сайт)\n{texts['siteText']}"
    [telegram_copy] = _copies(chat, texts["telegramText"])
    assert telegram_copy["text"] == f"Чат #{conversation} | №{sister} → №{brother} (Telegram)\n{texts['telegramText']}"
    assert site_copy["buttons"] == telegram_copy["buttons"] == WITNESS_BUTTONS
    assert len(_rows(chat, "witness.copy")) == 2
    assert _to(chat, tg_admin) == []  # with WITNESS_CHAT_ID set, admins get no private copies

    # «✍️ Написать в чат» in the witness group: a service message both participants receive in Telegram.
    say = result["witnessSay"]
    assert say["message"]["kind"] == "witness" and say["message"]["sender_id"] is None
    assert say["message"]["text"] == texts["witnessText"]
    witness_drain = drains["witness"]
    assert [row["kind"] for row in witness_drain["rows"]] == ["message.witness"]
    for telegram_id in (tg_brother, tg_sister):
        assert [item["text"] for item in _to(witness_drain, telegram_id)] == [
            f"<b>Сообщение от свидетеля:</b>\n{texts['witnessText']}"
        ]
    assert _to(witness_drain, WITNESS_CHAT_ID) == []  # the witness is not copied his own words

    assert any("Вали подключён" in item["text"] for item in _to(drains["wali"], tg_sister))
    # Her chat partner is told in Telegram too: his messages are copied to the wali from now on.
    [notice] = _to(drains["wali"], tg_brother)
    assert "подключён вали" in notice["text"] and f"Анкета №{sister}" in notice["text"]

    # The message written on the site under the wali's eye: delivered to the partner in Telegram,
    # copied to her wali and to the witness, nothing to the author.
    watched = drains["watched"]
    [row] = _rows(watched, "message.created")
    assert row["payload"]["origin"] == "web"
    assert row["payload"]["recipient_id"] == result["sister"]["userId"]
    assert row["payload"]["guardians"] == [
        {"ward_user_id": result["sister"]["userId"], "guardian_user_id": result["wali"]["wali_user_id"]}
    ]
    assert sorted(item["chat_id"] for item in watched["deliveries"]) == sorted([tg_sister, tg_wali, WITNESS_CHAT_ID])
    [to_partner] = _to(watched, tg_sister)
    assert to_partner["text"] == f"<b>Анкета №{brother} пишет:</b>\n{texts['watchedText']}"  # he has no wali
    [copy] = _to(watched, tg_wali)
    assert "Копия переписки" in copy["text"] and texts["watchedText"] in copy["text"]
    assert copy["buttons"] == []  # the wali reads, he does not take part
    assert len(_copies(watched, texts["watchedText"])) == 1

    # A phone number written on the site: hidden before anyone else sees it, in every channel.
    filtered = drains["filtered"]
    [row] = _rows(filtered, "message.created")
    phone_digits = re.sub(r"\D", "", texts["phone"])
    for item in filtered["deliveries"]:
        assert phone_digits not in re.sub(r"\D", "", item["text"]), item
    [to_partner] = _to(filtered, tg_sister)
    assert PLACEHOLDER in to_partner["text"] and texts["phoneVisible"] in to_partner["text"]
    [witness_copy] = _copies(filtered, PLACEHOLDER)
    assert witness_copy["text"].startswith(f"Чат #{conversation} | №{brother} → №{sister} (сайт)\n")
    assert [item["chat_id"] for item in _to(filtered, tg_wali)] == [tg_wali]

    # The legacy brother: consent first, then his old profile, number and chat history are there,
    # and the one-open-chat rule of the old bot holds across channels.
    migrated = result["legacy"]
    assert migrated["landing"] == "/welcome"
    assert f"Анкета №{legacy['numbers']['brother']}" in migrated["welcomeText"]
    assert migrated["openChatError"] == OPEN_CHAT_LIMIT_TEXT
    # His interest reached her in Telegram as the legacy card; the fields the old bot never asked
    # show «не указано».
    [card] = _to(drains["final"], tg_sister)
    assert f"Вами заинтересовалась анкета №{legacy['numbers']['brother']}" in card["text"]
    assert "Семейное положение: не указано" in card["text"] and "Национальность: татарин" in card["text"]
    assert card["buttons"] == ["Принять", "Отклонить"]
