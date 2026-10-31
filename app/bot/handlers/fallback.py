"""Last-resort handlers: cancel buttons, non-text content, chat relay and unknown buttons."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import CANCEL
from app.bot.handlers import chats
from app.bot.ui import edit, exit_flow


async def on_cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await exit_flow(state)
    await callback.answer()
    await edit(callback, texts.CANCELLED)


async def on_non_text(message: Message) -> None:
    # Photos, voice, video, stickers, documents, contacts, locations…: the legacy rule, text only.
    await message.answer(texts.MEDIA_REJECTED)


async def on_text_in_dialog(message: Message) -> None:
    await message.answer(texts.USE_BUTTONS)


async def on_stale_callback(callback: CallbackQuery) -> None:
    """Any button nobody handles (an old format, a dialog that ended): a polite answer."""
    await callback.answer(texts.STALE_BUTTON, show_alert=True)


def create_router() -> Router:
    router = Router(name="fallback")
    router.callback_query.register(on_cancel_button, F.data == CANCEL)
    router.callback_query.register(on_stale_callback)
    router.message.register(on_non_text, ~F.text)
    router.message.register(chats.relay_text, StateFilter(None))
    router.message.register(on_text_in_dialog)
    return router
