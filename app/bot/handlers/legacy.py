"""Buttons of the legacy bot that are still on people's screens (docs/LEGACY_BOT.md §3).

Most legacy formats are the new bot's own (int:acc / int:dec in handlers.interests, rep:<id> in
handlers.chats, w:* in handlers.witness, adm:appr / adm:rej in handlers.admin). This router takes
the rest; every action goes through the services with their usual checks:

- ``br:p:<i>`` / ``br:n:<i>`` (⬅️ / ➡️) reopen browsing at that card, ``br:int:<number>`` sends an
  interest to that profile at once, without a message, as the legacy button did; ``br:close`` closes
  the card, ``br:noop`` does nothing;
- ``reg:*``: the registration dialog of the old bot is gone — «Анкету заполняем заново»;
- ``chat:close``: «Завершить общение» of the only open chat (with a confirmation).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.callbacks import LEGACY_BROWSE_PREFIX, LEGACY_CHAT_CLOSE, LEGACY_REGISTRATION_PREFIX
from app.bot.handlers import catalog, chats
from app.bot.ui import send, set_buttons
from app.models import User
from app.services import chat
from app.services.errors import DomainError

MAX_INDEX = 1_000_000


def _number(part: str) -> int | None:
    return int(part) if part.isdigit() and len(part) <= 9 else None


async def on_browse(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    _, action, value_text, *_ = [*(callback.data or "").split(":"), "", ""]
    value = _number(value_text)
    match action:
        case "p" | "n" if value is not None and value < MAX_INDEX:
            await callback.answer()
            await catalog.show_catalog(callback, state, session, user, page=value + 1, replace=True)
        case "int" if value is not None:
            await catalog.start_interest(callback, state, session, user, value, one_tap=True)
        case "close":
            await callback.answer()
            await catalog.close_card(callback)
        case "noop":
            await callback.answer()
        case _:
            await callback.answer()
            await catalog.show_catalog(callback, state, session, user, replace=True)


async def on_registration(callback: CallbackQuery) -> None:
    await callback.answer(texts.LEGACY_REGISTRATION, show_alert=True)
    await set_buttons(callback, None)
    await send(callback, texts.LEGACY_REGISTRATION, keyboards.create_profile())


async def on_chat_close(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    open_chats = await chat.open_conversations(session, user)
    if not open_chats:
        await callback.answer("Открытого чата нет.", show_alert=True)
        return
    if len(open_chats) > 1:
        await callback.answer()
        await chats.show_my_chat(callback, session, user)
        return
    try:
        await chats.ask_close(callback, session, open_chats[0], user)
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)


def create_router() -> Router:
    router = Router(name="legacy")
    router.callback_query.register(on_browse, F.data.startswith(LEGACY_BROWSE_PREFIX))
    router.callback_query.register(on_registration, F.data.startswith(LEGACY_REGISTRATION_PREFIX))
    router.callback_query.register(on_chat_close, F.data == LEGACY_CHAT_CLOSE)
    return router
