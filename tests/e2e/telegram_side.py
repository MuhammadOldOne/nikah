"""The Telegram side of the end-to-end test: the bot's real dispatcher, handlers and outbox worker,
with a fake Bot API session (nothing is sent to api.telegram.org).

Run from the repository root by the e2e runner and by cross_channel.js; uses ``DATABASE_URL`` and
``WITNESS_CHAT_ID`` like the application and prints one JSON object::

    python -m tests.e2e.telegram_side relay TELEGRAM_ID TEXT
    python -m tests.e2e.telegram_side witness-say TELEGRAM_ID CONVERSATION_ID TEXT
    python -m tests.e2e.telegram_side wali-accept TELEGRAM_ID FIRST_NAME TOKEN
    python -m tests.e2e.telegram_side drain
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from app.bot import keyboards
from app.bot.dispatcher import create_bot, create_dispatcher
from app.bot.outbox import OutboxWorker
from app.config import get_settings
from app.db import dispose_engine, session_scope
from app.models import Message, Outbox, User
from app.services import users
from tests.bot.conftest import BotHarness, RecordingSession, button_data, buttons

FAKE_TOKEN = "42:E2E-FAKE-TOKEN"


def _harness() -> BotHarness:
    api = RecordingSession()
    return BotHarness(create_bot(FAKE_TOKEN, session=api), create_dispatcher(), api)


def _sent(method: SendMessage) -> dict[str, Any]:
    markup = method.reply_markup
    return {
        "chat_id": int(method.chat_id),
        "text": method.text,
        "buttons": [button.text for button in buttons(markup)] if isinstance(markup, InlineKeyboardMarkup) else [],
    }


async def _user(telegram_id: int) -> User:
    async with session_scope() as session:
        user = await users.get_by_telegram_id(session, telegram_id)
    if user is None:
        raise SystemExit(f"no account with telegram_id={telegram_id}")
    return user


async def _last_message(**where: Any) -> Message:
    async with session_scope() as session:
        query = select(Message).filter_by(**where).order_by(Message.id.desc()).limit(1)
        message = await session.scalar(query)
    assert message is not None, f"no message with {where}"
    return message


async def relay(telegram_id: int, text: str) -> dict[str, Any]:
    """The person types ``text`` in the bot's private chat. With exactly one open chat any plain
    text goes there (the legacy bot's behaviour): stored with origin=telegram, no «Ответить»."""
    user = await _user(telegram_id)
    harness = _harness()
    await harness.text(user, text)
    stored = await _last_message(sender_id=user.id)
    return {
        "replies_after_text": harness.texts(user),
        "message": {"id": stored.id, "conversation_id": stored.conversation_id, "origin": stored.origin.value},
    }


async def witness_say(telegram_id: int, conversation_id: int, text: str) -> dict[str, Any]:
    """In the witness group (WITNESS_CHAT_ID) an admin presses «✍️ Написать в чат» under the copy of
    the chat's latest message, then answers the bot's prompt there with ``text``."""
    group = get_settings().WITNESS_CHAT_ID
    assert group is not None, "WITNESS_CHAT_ID must be set"
    witness = await _user(telegram_id)
    last = await _last_message(conversation_id=conversation_id)
    copy_buttons = keyboards.witness_copy(last.id, conversation_id)
    harness = _harness()
    await harness.press(witness, button_data(copy_buttons, "Написать в чат"), "copy", group_id=group)
    prompt = harness.last_text(group)
    harness.clear()
    await harness.text(witness, text, group_id=group)
    stored = await _last_message(conversation_id=conversation_id)
    return {
        "prompt": prompt,
        "replies": harness.texts(group),
        "message": {"id": stored.id, "kind": stored.kind.value, "sender_id": stored.sender_id, "text": stored.text},
    }


async def wali_accept(telegram_id: int, first_name: str, token: str) -> dict[str, Any]:
    """The wali opens the invite link (/start wali_<token>) and presses «Подтвердить»."""
    async with session_scope() as session:
        wali = await users.get_or_create_by_telegram(session, telegram_id, None, first_name)
    harness = _harness()
    await harness.text(wali, f"/start wali_{token}")
    prompt = harness.last_text(wali)
    await harness.press(wali, button_data(harness.last_markup(wali), "Подтвердить"), prompt)
    return {"wali_user_id": wali.id, "prompt": prompt, "confirmation": harness.edited_texts()}


async def drain() -> dict[str, Any]:
    """One run of the bot's outbox worker over every due row, with a recording Bot."""
    async with session_scope() as session:
        pending = (
            await session.scalars(
                select(Outbox).where(Outbox.sent_at.is_(None), Outbox.failed_at.is_(None)).order_by(Outbox.id)
            )
        ).all()
        pending_ids = [row.id for row in pending]
    harness = _harness()
    worker = OutboxWorker(harness.bot, send_delay=0)
    while await worker.process_batch():
        pass
    async with session_scope() as session:
        rows = (await session.scalars(select(Outbox).where(Outbox.id.in_(pending_ids)).order_by(Outbox.id))).all()
        processed = [
            {"id": row.id, "kind": row.kind, "payload": row.payload} for row in rows if row.sent_at is not None
        ]
        unsent = [row.id for row in rows if row.sent_at is None]
    return {"rows": processed, "unsent_row_ids": unsent, "deliveries": [_sent(method) for method in harness.sent()]}


async def main(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.telegram_side")
    commands = parser.add_subparsers(dest="command", required=True)
    relay_cmd = commands.add_parser("relay")
    relay_cmd.add_argument("telegram_id", type=int)
    relay_cmd.add_argument("text")
    say_cmd = commands.add_parser("witness-say")
    say_cmd.add_argument("telegram_id", type=int)
    say_cmd.add_argument("conversation_id", type=int)
    say_cmd.add_argument("text")
    wali_cmd = commands.add_parser("wali-accept")
    wali_cmd.add_argument("telegram_id", type=int)
    wali_cmd.add_argument("first_name")
    wali_cmd.add_argument("token")
    commands.add_parser("drain")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "relay":
                return await relay(args.telegram_id, args.text)
            case "witness-say":
                return await witness_say(args.telegram_id, args.conversation_id, args.text)
            case "wali-accept":
                return await wali_accept(args.telegram_id, args.first_name, args.token)
            case _:
                return await drain()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1:])), ensure_ascii=False))
