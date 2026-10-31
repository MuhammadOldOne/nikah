"""Bot middlewares: one DB unit of work per update, the current account, admin-only routers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject
from aiogram.types import User as TelegramUser

from app.bot import texts
from app.bot.callbacks import AccountCb
from app.db import session_scope
from app.services import users
from app.services.errors import PermissionDenied

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


async def _reply(event: TelegramObject, text: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    elif isinstance(event, Message):
        await event.answer(text)


def _is_account_deletion(event: TelegramObject) -> bool:
    """/delete and its buttons: banned users keep the right to erase their data (152-FZ)."""
    if isinstance(event, Message):
        command = (event.text or "").split(maxsplit=1)[0] if event.text else ""
        return command.partition("@")[0] == "/delete"
    if isinstance(event, CallbackQuery):
        try:
            AccountCb.unpack(event.data or "")
        except (TypeError, ValueError):
            return False
        return True
    return False


class DbSessionMiddleware(BaseMiddleware):
    """Outer update middleware: the whole update is one transaction (commit on success,
    rollback on any exception). Handlers receive it as ``session``."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with session_scope() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Outer message/callback middleware: private chats only; upserts the account by Telegram id
    (handlers receive it as ``user``, with ``profile`` loaded) and stops banned users, who may
    only delete their account."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat: Chat | None = data.get("event_chat")
        sender: TelegramUser | None = data.get("event_from_user")
        if chat is None or chat.type != ChatType.PRIVATE or sender is None or sender.is_bot:
            return None  # the bot works only in private chats: never post profiles into groups
        session = data["session"]
        user = await users.get_or_create_by_telegram(session, sender.id, sender.username, sender.first_name)
        await users.touch_last_seen(session, user)
        if user.is_banned and not _is_account_deletion(event):
            await _reply(event, texts.BANNED)
            return None
        data["user"] = user
        return await handler(event, data)


class AdminMiddleware(BaseMiddleware):
    """Inner middleware of the admin router: runs for every admin handler after its filters
    matched, so admin rights are checked on each action, never inferred from callback data."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("user")
        if user is None or not users.is_admin(user):
            await _reply(event, PermissionDenied().message)
            return None
        return await handler(event, data)
