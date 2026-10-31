"""End-to-end check that the site and the Telegram bot are one system (spec: «Сайт и бот — одна
система с одной базой данных»), with real browsers on a running server.

Skipped unless ``E2E=1``. Needs Node with Playwright and a free port 8000::

    E2E=1 TEST_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/<test_db> \\
        .venv/bin/pytest -q tests/e2e

The runner migrates the dev database (``E2E_DATABASE_URL``, default: the dev DB ``nikah``), removes
the e2e accounts of a previous run, starts uvicorn with ENV=dev, DEV_LOGIN_ENABLED=true and
PREMODERATION=true, and runs ``cross_channel.js``. That script drives the browsers and calls
``telegram_side.py`` for the bot's side (real handlers and outbox worker, fake Bot API). Screenshots
are saved to docs/screenshots/e2e-*.png. The e2e accounts are kept afterwards for inspection.
"""

from __future__ import annotations

import json
import os
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
DATABASE_URL = os.environ.get("E2E_DATABASE_URL", "postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah")
HOST, PORT = "127.0.0.1", 8000
BASE_URL = f"http://{HOST}:{PORT}"
# Telegram ids reserved for this test; their accounts are deleted at the start of every run.
ACCOUNTS = {"brother": 9_200_000_001, "sister": 9_200_000_002, "wali": 9_200_000_003, "admin": 9_200_000_004}


def _app_env() -> dict[str, str]:
    """Environment of the server and of the Telegram side: the same database and settings."""
    env = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    env.update(
        DATABASE_URL=DATABASE_URL,
        ENV="dev",
        DEV_LOGIN_ENABLED="true",
        PREMODERATION="true",
        INTEREST_DAILY_LIMIT="10",
        SITE_URL=BASE_URL,
        SECRET_KEY="",  # the dev key
        TELEGRAM_TOKEN="",
        TELEGRAM_WIDGET_ENABLED="false",
        SESSION_COOKIE_SECURE="false",
        ADMIN_TELEGRAM_IDS=str(ACCOUNTS["admin"]),
        PYTHONUNBUFFERED="1",
    )
    return env


def _run(args: list[str], env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False)


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


@pytest.fixture
def server(tmp_path: Path) -> Iterator[dict[str, str]]:
    if shutil.which("node") is None:
        pytest.fail("node is required for the browser end-to-end test")
    if not _port_is_free():
        pytest.fail(f"port {PORT} is busy: stop the dev server first")
    env = _app_env()
    for args in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "tests.e2e.telegram_side", "reset", *map(str, ACCOUNTS.values())],
    ):
        done = _run(args, env)
        assert done.returncode == 0, done.stderr
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                *(sys.executable, "-m", "uvicorn", "app.web.main:app"),
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
            yield env | {"E2E_SERVER_LOG": str(log_path)}
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


def _to(drain: dict[str, Any], telegram_id: int) -> list[dict[str, Any]]:
    return [item for item in drain["deliveries"] if item["chat_id"] == telegram_id]


def _kinds(drain: dict[str, Any], *user_ids: int) -> list[str]:
    """Kinds of the processed outbox rows that concern these users (old rows may be drained too)."""
    return sorted(row["kind"] for row in drain["rows"] if set(user_ids) & set(row["payload"].get("user_ids", [])))


def test_site_and_bot_stay_in_sync(server: dict[str, str]) -> None:
    result = _browser_run(server)
    brother, sister = result["brotherId"], result["sisterId"]
    wali = result["wali"]["wali_user_id"]
    texts = result["texts"]
    tg_brother, tg_sister, tg_wali, tg_admin = (ACCOUNTS[key] for key in ("brother", "sister", "wali", "admin"))
    drains = result["drains"]
    assert all(drain["unsent_row_ids"] == [] for drain in drains.values())

    # New profiles go to the admin in Telegram with moderation buttons, the decision to the authors.
    cards = _to(drains["submitted"], tg_admin)
    for name, user_id in (("Ахмад", brother), ("Мариям", sister)):
        assert any(
            "Анкета на модерации" in card["text"] and name in card["text"] and f"#{user_id}" in card["text"]
            for card in cards
        ), cards
    assert all({"✅ Одобрить", "❌ Отклонить"} <= set(card["buttons"]) for card in cards), cards
    assert _kinds(drains["moderated"], brother, sister) == ["profile.moderated"] * 2
    assert len(_to(drains["moderated"], tg_brother)) == len(_to(drains["moderated"], tg_sister)) == 1

    # The interest sent on the site reaches her in Telegram with «Принять» / «Отклонить».
    [card] = _to(drains["interest"], tg_sister)
    assert texts["interestText"] in card["text"] and {"✅ Принять", "❌ Отклонить"} <= set(card["buttons"])

    # Acceptance, his site message and her Telegram reply: each side is notified about the other only.
    chat = drains["chat"]
    assert _kinds(chat, brother, sister) == ["interest.accepted", "message.created", "message.created"]
    to_brother, to_sister = _to(chat, tg_brother), _to(chat, tg_sister)
    assert any("Ваш отклик принят" in item["text"] for item in to_brother)
    [reply] = [item for item in to_brother if texts["telegramText"] in item["text"]]
    assert f"(#{sister})" in reply["text"]
    [site_message] = [item for item in to_sister if texts["siteText"] in item["text"]]
    assert f"(#{brother})" in site_message["text"]
    assert not any(texts["telegramText"] in item["text"] for item in to_sister)  # never echoed to the sender
    assert not any(texts["siteText"] in item["text"] for item in to_brother)

    assert any("Вали подключён" in item["text"] for item in _to(drains["wali"], tg_sister))

    # The message written on the site under the wali's eye: one event, delivered to the partner in
    # Telegram and copied to her wali, nothing to the author.
    watched = drains["watched"]
    [row] = watched["rows"]
    assert row["kind"] == "message.created" and row["payload"]["origin"] == "web"
    assert row["payload"]["sender_id"] == brother and row["payload"]["recipient_id"] == sister
    assert row["payload"]["guardians"] == [{"ward_user_id": sister, "guardian_user_id": wali}]
    assert sorted(item["chat_id"] for item in watched["deliveries"]) == sorted([tg_sister, tg_wali])
    [to_partner] = _to(watched, tg_sister)
    assert to_partner["text"].startswith("💬") and texts["watchedText"] in to_partner["text"]
    assert f"(#{brother})" in to_partner["text"]
    assert any("Ответить" in button for button in to_partner["buttons"])
    [copy] = _to(watched, tg_wali)
    assert "Копия переписки" in copy["text"] and texts["watchedText"] in copy["text"]
    assert copy["buttons"] == []  # the wali reads, he does not take part
    assert _to(watched, tg_brother) == []
