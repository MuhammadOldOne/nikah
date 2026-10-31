"""Small helpers shared by handlers: sending, editing and leaving multi-step dialogs."""

from __future__ import annotations

import contextlib

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot import keyboards
from app.models import User
from app.services import users

ReplyMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | ForceReply

# FSM data of dialogs in progress; dropped when a dialog ends. Other keys (catalog filters, the
# action waiting for consent) survive navigation.
FLOW_KEYS = ("wizard", "interest_to", "report", "filter_draft", "reject", "witness_say")


def menu(user: User | None) -> ReplyKeyboardMarkup:
    """The main menu for this person: with or without a profile, «/admin» for admins."""
    return keyboards.main_menu(
        has_profile=user is not None and user.profile is not None,
        is_admin=user is not None and users.is_admin(user),
    )


async def send(event: Message | CallbackQuery, text: str, reply_markup: ReplyMarkup | None = None) -> Message:
    """Send a new message to the user who triggered ``event`` (the bot works in private chats)."""
    if isinstance(event, Message):
        return await event.answer(text, reply_markup=reply_markup)
    assert event.bot is not None
    return await event.bot.send_message(event.from_user.id, text, reply_markup=reply_markup)


async def edit(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Replace the text of the callback's message, or send a new one if it cannot be edited."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message:
                return
    await send(callback, text, reply_markup)


async def set_buttons(callback: CallbackQuery, reply_markup: InlineKeyboardMarkup | None) -> None:
    """Replace (or with None remove) the inline keyboard of the callback's message."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=reply_markup)


def message_html(callback: CallbackQuery) -> str:
    """HTML of the callback's message, to append a status line when editing it."""
    return callback.message.html_text if isinstance(callback.message, Message) else ""


async def exit_flow(state: FSMContext) -> bool:
    """Leave any multi-step dialog. Returns True if one was in progress."""
    was_active = await state.get_state() is not None
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data({key: value for key, value in data.items() if key not in FLOW_KEYS})
    return was_active
