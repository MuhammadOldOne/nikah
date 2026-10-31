"""Bot middlewares: one DB unit of work per update (committed before every Telegram call), the
current account, admin-only routers. The consent gate lives in handlers.consent."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import AccountCb
from app.config import get_settings
from app.db import session_scope
from app.models import User
from app.services import meta, users
from app.services.errors import PermissionDenied, WitnessOnly

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# The unit of work of the update being handled in this task (None outside updates, e.g. in the
# outbox worker, which shares the Bot).
_update_session: ContextVar[AsyncSession | None] = ContextVar("bot_update_session", default=None)


def current_update_session() -> AsyncSession | None:
    """The unit of work of the update being handled in this task, if any."""
    return _update_session.get()


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
    """Outer update middleware: one unit of work per update (commit on success, rollback on any
    exception). Handlers receive it as ``session``. CommitBeforeTelegram commits it earlier,
    before each Bot API call."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with session_scope() as session:
            data["session"] = session
            token = _update_session.set(session)
            try:
                return await handler(event, data)
            finally:
                _update_session.reset(token)


class CommitBeforeTelegram(BaseRequestMiddleware):
    """Bot API request middleware. Telegram calls are side effects a rollback cannot undo, so the
    database work of the current update is committed before each of them: a Telegram call that
    fails afterwards (network error, flood control) can no longer roll back an action the user was
    already told about. Answering a callback query is a courtesy: when Telegram rejects the answer
    (e.g. "query is too old" after a restart), the handler carries on with the action."""

    async def __call__(
        self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot, method: TelegramMethod[TelegramType]
    ) -> Response[TelegramType]:
        session = _update_session.get()
        if session is not None and session.in_transaction() and not session.in_nested_transaction():
            await session.commit()
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if not isinstance(method, AnswerCallbackQuery):
                raise
            logger.info("callback query was not answered: %.200s", exc.message)
            return True  # type: ignore[return-value]  # the result of answerCallbackQuery


async def _witness_chat_member(session: AsyncSession, sender: TelegramUser) -> User | None:
    """The account of someone acting in the witness group. Accounts are created only for the
    admins of ADMIN_TELEGRAM_IDS (who may never have opened the bot privately), never for other
    members of the group."""
    user = await users.get_by_telegram_id(session, sender.id)
    if user is None and sender.id in get_settings().ADMIN_TELEGRAM_IDS:
        user = await users.get_or_create_by_telegram(session, sender.id, sender.username, sender.first_name)
    return user


class UserMiddleware(BaseMiddleware):
    """Outer message/callback middleware: private chats, plus the witness group (WITNESS_CHAT_ID)
    where only the witness handlers run; upserts the account by Telegram id (handlers receive it
    as ``user``, with ``profile`` loaded) and stops banned users, who may only delete their account."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat: Chat | None = data.get("event_chat")
        sender: TelegramUser | None = data.get("event_from_user")
        if chat is None or sender is None or sender.is_bot:
            return None
        session = data["session"]
        if chat.type != ChatType.PRIVATE:
            # Never post profiles into groups: only the witness buttons and the witness's text work there.
            # The group's new id counts too after Telegram upgraded it to a supergroup.
            configured = get_settings().WITNESS_CHAT_ID
            if configured is None or chat.id not in (configured, await meta.witness_chat_id(session)):
                return None
            member = await _witness_chat_member(session, sender)
            if member is None:
                if isinstance(event, CallbackQuery):
                    await event.answer(WitnessOnly().message, show_alert=True)
                return None
            data["user"] = member
            return await handler(event, data)
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
