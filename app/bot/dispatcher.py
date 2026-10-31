"""Bot and dispatcher factories (used by ``python -m app.bot`` and by the tests)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, ErrorEvent

from app.bot import texts
from app.bot.handlers import create_router
from app.bot.middlewares import CommitBeforeTelegram, DbSessionMiddleware, UserMiddleware
from app.config import get_settings
from app.logutil import exc_summary

logger = logging.getLogger(__name__)


PROXY_UNSUPPORTED = (
    "TELEGRAM_PROXY: use http://host:port or socks5://host:port (with user:password@ if needed); "
    "https:// proxies are not supported by the Telegram client library."
)


def telegram_session() -> AiohttpSession | None:
    """An HTTP session through TELEGRAM_PROXY (http:// CONNECT, socks5:// or socks4://, via
    aiohttp-socks), or None for a direct connection. The proxy URL may hold credentials: it is
    never logged, not even in the error for an unsupported scheme (ValueError)."""
    proxy = get_settings().TELEGRAM_PROXY
    if proxy is None or not proxy.get_secret_value():
        return None
    try:
        session = AiohttpSession(proxy=proxy.get_secret_value())
    except ValueError:
        raise ValueError(PROXY_UNSUPPORTED) from None
    logger.info("Telegram requests go through TELEGRAM_PROXY")
    return session


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


def _commands(items: Iterable[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=command, description=description) for command, description in items]


async def set_commands(bot: Bot, admin_ids: Iterable[int] = ()) -> None:
    """The legacy commands plus /stop and /delete for everyone; /admin, /ban, /unban for admins."""
    await bot.set_my_commands(_commands(texts.COMMANDS), scope=BotCommandScopeAllPrivateChats())
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(_commands(texts.ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as exc:  # e.g. an admin who never opened the bot
            logger.info("admin commands not set for one admin: %s", type(exc).__name__)
