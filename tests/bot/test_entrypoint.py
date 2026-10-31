"""``python -m app.bot`` startup: Telegram is reached (through TELEGRAM_PROXY if set), a leftover
webhook removed without dropping pending updates, the command menu set, then polling."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import DeleteMyCommands, SetMyCommands
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiohttp import TCPConnector
from pydantic import SecretStr

from app.bot.__main__ import clear, serve
from app.bot.dispatcher import PROXY_UNSUPPORTED, create_bot, create_dispatcher, set_commands, telegram_session
from tests.bot.conftest import RecordingSession


async def test_serve_removes_webhook_before_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RecordingSession()
    bot = create_bot("42:TEST-TOKEN", session=api)
    dispatcher = create_dispatcher()
    calls_before_polling: list[list[str]] = []

    async def fake_polling(*args: Any, **kwargs: Any) -> None:
        calls_before_polling.append([type(method).__name__ for method in api.requests])

    monkeypatch.setattr(dispatcher, "start_polling", fake_polling)
    await serve(bot, dispatcher)

    assert calls_before_polling == [["GetMe", "DeleteWebhook", "SetMyCommands"]]
    [delete_webhook] = [method for method in api.requests if type(method).__name__ == "DeleteWebhook"]
    assert not delete_webhook.drop_pending_updates  # users' messages sent during the switch are kept


async def test_commands_menu_has_the_legacy_commands_and_admin_ones_for_admins(make_user) -> None:
    api = RecordingSession()
    bot = create_bot("42:TEST-TOKEN", session=api)
    await set_commands(bot, [777])
    everyone, admins = [method for method in api.requests if isinstance(method, SetMyCommands)]
    names = [command.command for command in everyone.commands]
    assert names[:4] == ["start", "browse", "myprofile", "create"]
    assert {"help", "rules", "cancel", "stop", "delete"} <= set(names) and "admin" not in names
    assert isinstance(everyone.scope, BotCommandScopeAllPrivateChats)
    assert [c.command for c in admins.commands][-3:] == ["admin", "ban", "unban"]
    assert isinstance(admins.scope, BotCommandScopeChat) and admins.scope.chat_id == 777


async def test_clear_commands_removes_the_menu_for_everyone_and_every_admin(make_user, override_settings) -> None:
    """Rollback to the legacy bot: its users must not keep /stop or /delete in the menu."""
    override_settings(ADMIN_TELEGRAM_IDS=[777])
    flagged = await make_user(is_admin=True)
    api = RecordingSession()
    await clear(create_bot("42:TEST-TOKEN", session=api))
    deleted = [method for method in api.requests if isinstance(method, DeleteMyCommands)]
    assert isinstance(deleted[0].scope, BotCommandScopeAllPrivateChats)
    assert sorted(method.scope.chat_id for method in deleted[1:]) == sorted([777, flagged.telegram_id])


@pytest.mark.parametrize(
    "proxy", ["http://127.0.0.1:3128", "http://user:secret@proxy.example:8080", "socks5://user:secret@10.0.0.1:1080"]
)
def test_telegram_proxy_goes_to_the_aiohttp_session(override_settings, caplog, proxy) -> None:
    override_settings(TELEGRAM_PROXY=SecretStr(proxy))
    with caplog.at_level(logging.DEBUG):
        session = telegram_session()
    assert isinstance(session, AiohttpSession) and session.proxy == proxy
    assert session._connector_type is not TCPConnector  # aiohttp-socks connector (http and socks alike)
    assert "secret" not in caplog.text and proxy not in caplog.text  # never logged
    bot = create_bot("42:TEST-TOKEN", session=session)
    assert bot.session is session


def test_unsupported_proxy_scheme_fails_without_revealing_the_url(override_settings) -> None:
    override_settings(TELEGRAM_PROXY=SecretStr("https://user:secret@proxy.example:8443"))
    with pytest.raises(ValueError) as error:
        telegram_session()
    assert str(error.value) == PROXY_UNSUPPORTED and "secret" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_no_proxy_means_a_direct_connection(override_settings) -> None:
    override_settings(TELEGRAM_PROXY=None)
    assert telegram_session() is None
    override_settings(TELEGRAM_PROXY=SecretStr(""))
    assert telegram_session() is None


def test_pending_updates_are_never_dropped() -> None:
    """Messages sent while the old bot is replaced must reach the new one (LEGACY_BOT.md §3)."""
    sources = [path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py")]
    assert not any(re.search(r"drop_pending_updates\s*=\s*True", source) for source in sources)
    assert any("drop_pending_updates=False" in source for source in sources)
