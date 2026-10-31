"""``python -m app.bot`` startup: Telegram is reached, a leftover webhook removed, then polling."""

from __future__ import annotations

from typing import Any

import pytest

from app.bot.__main__ import serve
from app.bot.dispatcher import create_bot, create_dispatcher
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
