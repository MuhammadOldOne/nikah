"""Bot and dispatcher factories (used by ``python -m app.bot`` and by the tests)."""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, ErrorEvent

from app.bot import texts
from app.bot.handlers import create_router
from app.bot.middlewares import CommitBeforeTelegram, DbSessionMiddleware, UserMiddleware
from app.logutil import exc_summary

logger = logging.getLogger(__name__)


def create_bot(token: str, session: BaseSession | None = None) -> Bot:
    """HTML parse mode everywhere (user text is escaped in app.bot.render) and no link previews.
    Update handlers commit their database work before each Telegram call (CommitBeforeTelegram)."""
    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    bot.session.middleware(CommitBeforeTelegram())
    return bot


async def on_error(event: ErrorEvent) -> bool:
    logger.error("update %s failed: %s", event.update.update_id, exc_summary(event.exception))
    with contextlib.suppress(TelegramAPIError):
        if event.update.callback_query is not None:
            await event.update.callback_query.answer(texts.ERROR, show_alert=True)
        elif event.update.message is not None:
            await event.update.message.answer(texts.ERROR)
    return True


def create_dispatcher(storage: BaseStorage | None = None) -> Dispatcher:
    """Dispatcher with an in-memory FSM (dialog progress is short-lived; durable state such as
    the active chat lives in the database). Updates of one user are processed one at a time,
    so double taps cannot race inside a dialog."""
    dispatcher = Dispatcher(storage=storage or MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    user_middleware = UserMiddleware()
    dispatcher.message.outer_middleware(user_middleware)
    dispatcher.callback_query.outer_middleware(user_middleware)
    dispatcher.errors.register(on_error)
    dispatcher.include_router(create_router())
    return dispatcher


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command=command, description=description) for command, description in texts.COMMANDS],
        scope=BotCommandScopeAllPrivateChats(),
    )
