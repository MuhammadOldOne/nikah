"""Commands, main-menu buttons and replies to chat messages. This router is checked first, so
they work in any dialog state (they leave the current dialog instead of being read as an answer).

The legacy commands (/start /create /myprofile /browse /cancel /help /rules; /admin /ban /unban
in handlers.admin) and the legacy menu labels work as before; the labels of the first new-bot
keyboard (with emoji) are accepted as synonyms."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers import account, catalog, chats, interests, profile, settings, start
from app.bot.ui import exit_flow, menu
from app.models import User

SECTION_COMMANDS: dict[str, str] = {
    "browse": texts.MENU_BROWSE,
    "myprofile": texts.MENU_MY_PROFILE,
    "create": texts.MENU_CREATE,
}


def section_for_text(text: str) -> str | None:
    """The menu section a text opens: a label, a synonym or a section command, else None."""
    if text in texts.MENU_SECTIONS:
        return text
    if text in texts.MENU_SYNONYMS:
        return texts.MENU_SYNONYMS[text]
    if text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].partition("@")[0]
        return SECTION_COMMANDS.get(command)
    return None


async def open_section(
    event: Message | CallbackQuery, section: str, state: FSMContext, session: AsyncSession, user: User
) -> None:
    match section:
        case texts.MENU_BROWSE:
            await catalog.show_catalog(event, state, session, user)
        case texts.MENU_MY_PROFILE:
            await profile.show_my_profile(event, session, user)
        case texts.MENU_CREATE:
            await profile.start_registration(event, state, session, user)
        case texts.MENU_MY_CHAT:
            await chats.show_my_chat(event, session, user)
        case texts.MENU_INTERESTS:
            await interests.show_interests(event, session, user)
        case texts.MENU_SETTINGS:
            await settings.show_settings(event, session, user)
        case texts.MENU_SITE:
            await account.send_site_link(event, session, user)


async def on_section(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await exit_flow(state)
    section = section_for_text(message.text or "")
    if section is not None:
        await open_section(message, section, state, session, user)


async def on_help(message: Message, user: User) -> None:
    await message.answer(texts.HELP, reply_markup=menu(user))


async def on_rules(message: Message, user: User) -> None:
    await message.answer(texts.RULES, reply_markup=menu(user))


async def on_cancel(message: Message, state: FSMContext, user: User) -> None:
    text = texts.CANCELLED if await exit_flow(state) else texts.NOTHING_TO_CANCEL
    await message.answer(text, reply_markup=menu(user))


async def on_stop(message: Message, session: AsyncSession, user: User) -> None:
    await chats.stop_chat(message, session, user)


async def on_delete(message: Message, state: FSMContext) -> None:
    await exit_flow(state)
    await account.ask_delete(message)


def create_router() -> Router:
    router = Router(name="navigation")
    router.message.register(start.on_start, CommandStart())
    router.message.register(on_help, Command("help"))
    router.message.register(on_help, F.text == texts.MENU_HELP)
    router.message.register(on_rules, Command("rules"))
    router.message.register(on_rules, F.text == texts.MENU_RULES)
    router.message.register(on_cancel, Command("cancel"))
    router.message.register(on_stop, Command("stop"))
    router.message.register(on_delete, Command("delete"))
    router.message.register(on_section, Command(*SECTION_COMMANDS))
    router.message.register(on_section, F.text.in_(texts.MENU_LABELS))
    router.message.register(chats.on_chat_reply, chats.chat_reply_filter)
    return router
