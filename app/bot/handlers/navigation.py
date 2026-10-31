"""Commands and main-menu buttons. This router is checked first, so they work in any dialog
state (a menu button leaves the current dialog instead of being read as an answer)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.handlers import account, catalog, chats, interests, profile, settings, start
from app.bot.ui import exit_flow
from app.models import User


async def on_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.main_menu())


async def on_cancel(message: Message, state: FSMContext) -> None:
    text = texts.CANCELLED if await exit_flow(state) else texts.NOTHING_TO_CANCEL
    await message.answer(text, reply_markup=keyboards.main_menu())


async def on_stop(message: Message, session: AsyncSession, user: User) -> None:
    await chats.stop_chat(message, session, user)


async def on_delete(message: Message, state: FSMContext) -> None:
    await exit_flow(state)
    await account.ask_delete(message)


async def on_menu(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await exit_flow(state)
    match message.text:
        case texts.MENU_PROFILE:
            await profile.show_my_profile(message, session, user)
        case texts.MENU_CATALOG:
            await catalog.show_catalog(message, state, session, user)
        case texts.MENU_INTERESTS:
            await interests.show_interests(message, session, user)
        case texts.MENU_CHATS:
            await chats.show_chats(message, session, user)
        case texts.MENU_SETTINGS:
            await settings.show_settings(message, session, user)
        case texts.MENU_SITE:
            await account.send_site_link(message, session, user)


def create_router() -> Router:
    router = Router(name="navigation")
    router.message.register(start.on_start, CommandStart())
    router.message.register(on_help, Command("help"))
    router.message.register(on_cancel, Command("cancel"))
    router.message.register(on_stop, Command("stop"))
    router.message.register(on_delete, Command("delete"))
    router.message.register(on_menu, F.text.in_(texts.MENU_BUTTONS))
    return router
