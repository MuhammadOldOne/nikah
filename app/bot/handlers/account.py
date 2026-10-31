"""«🌐 Открыть сайт» (one-time magic link) and account deletion (/delete)."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.callbacks import AccountAction, AccountCb
from app.bot.render import esc
from app.bot.ui import send, set_buttons
from app.models import User
from app.services import auth, users
from app.services.errors import DomainError


async def send_site_link(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    try:
        url = await auth.create_magic_link(session, user)
    except DomainError as exc:
        await send(event, esc(exc.message))
        return
    # A URL button, not a link in the text: a link preview could consume the single-use token.
    await send(event, texts.SITE_LINK, keyboards.open_site(url))


async def ask_delete(event: Message | CallbackQuery) -> None:
    await send(event, texts.DELETE_CONFIRM, keyboards.delete_confirm())


async def on_account_action(
    callback: CallbackQuery, callback_data: AccountCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    if callback_data.action == AccountAction.DELETE:
        await ask_delete(callback)
        return
    await set_buttons(callback, None)
    await users.delete_account(session, user)
    await state.clear()
    await send(callback, texts.DELETED, ReplyKeyboardRemove())


def create_router() -> Router:
    router = Router(name="account")
    router.callback_query.register(on_account_action, AccountCb.filter())
    return router
