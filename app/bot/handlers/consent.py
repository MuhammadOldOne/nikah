"""Consent to personal data processing (152-FZ) for accounts migrated from the legacy bot.

Migrated people keep their profile but have no recorded consent (``users.needs_consent``). Before
anything else they are shown the consent prompt (link {SITE_URL}/consent, «Даю согласие»); what
they started continues after consent: the last menu section or command, and every chat message
(in order, each to the chat it was addressed to; a «Reply» the relay would refuse is refused now).
Without consent they may still read the rules and help, start the bot, cancel, delete the account
and withdraw (hide the profile, cancel an interest, close a chat, report a message in their chat),
as services allow (docs/ARCHITECTURE.md §7).

People without a profile give consent at the end of the profile form (handlers.profile).
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.callbacks import (
    CANCEL,
    GIVE_CONSENT,
    LEGACY_CHAT_CLOSE,
    AccountCb,
    ChatAction,
    ChatCb,
    InterestAction,
    InterestCb,
    LoginCb,
    MessageReportCb,
    ProfileAction,
    ProfileCb,
    WaliCb,
)
from app.bot.handlers import chats, navigation, profile
from app.bot.middlewares import Handler
from app.bot.render import esc
from app.bot.ui import menu, send, set_buttons
from app.config import get_settings
from app.models import User
from app.services import users

# What waits for consent: {"section": the last menu label or command, "messages": [{"text",
# "conversation_id"}]} — chat texts in order, each with the chat it was addressed to (None: the only
# open chat). Kept in the dialog storage, so a restart before consent loses nothing.
PENDING_KEY = "after_consent"
MAX_PENDING_MESSAGES = 20
FREE_COMMANDS = frozenset({"start", "help", "rules", "cancel", "delete"})
FREE_LABELS = frozenset({texts.MENU_RULES, texts.MENU_HELP})


def prompt_text() -> str:
    url = f"{get_settings().SITE_URL}/consent"
    return (
        "<b>Согласие на обработку персональных данных</b>\n\n"
        "Бот обновился: теперь у него есть сайт «Никах Истина», а ваша анкета, отклики и переписка "
        "сохранены. По закону (152-ФЗ) для дальнейшей работы нужно ваше отдельное согласие: анкета "
        "содержит сведения о религиозных убеждениях — это особая категория персональных данных. "
        "Данные хранятся на сервере в России и используются только для работы платформы.\n\n"
        f'Прочитайте <a href="{esc(url)}">текст согласия</a> и нажмите «{texts.CONSENT_BUTTON}». '
        "Если не согласны — /delete удалит аккаунт и все данные."
    )


async def ask(event: Message | CallbackQuery) -> None:
    """Show the consent prompt."""
    await send(event, prompt_text(), keyboards.give_consent())


async def _hold(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Keep a gated text for after consent: a menu section or command (the last one wins), or a
    chat message (all of them, in order, each to the chat it was addressed to)."""
    text = message.text or ""
    pending: dict[str, Any] = dict((await state.get_data()).get(PENDING_KEY) or {})
    messages: list[dict[str, Any]] = list(pending.get("messages", []))
    if text.startswith("/") or navigation.section_for_text(text) is not None:
        pending["section"] = text
        await state.update_data({PENDING_KEY: pending})
        await ask(message)
        return
    where = await chats.destination(message, session)
    if where.refusal:  # a «Reply» to a card or a wali copy: refused as it would be with consent
        await message.answer(where.refusal)
        return
    if len(messages) >= MAX_PENDING_MESSAGES:
        await message.answer(texts.CONSENT_QUEUE_FULL, reply_markup=keyboards.give_consent())
        return
    messages.append({"text": text, "conversation_id": where.conversation_id})
    await state.update_data({PENDING_KEY: {**pending, "messages": messages}})
    if len(messages) == 1:
        await ask(message)
    else:
        await message.answer(texts.CONSENT_MESSAGE_WAITS, reply_markup=keyboards.give_consent())


def gated(user: User | None) -> bool:
    """Whether the consent gate applies: an account with a profile and no recorded consent."""
    return user is not None and user.profile is not None and users.needs_consent(user)


def _free_message(message: Message) -> bool:
    text = message.text or ""
    if not text:
        return True  # media: the «text only» reply reveals nothing
    if text in FREE_LABELS:
        return True
    command = text.split(maxsplit=1)[0].partition("@")[0]
    return command.startswith("/") and command[1:] in FREE_COMMANDS


def _free_callback(data: str) -> bool:
    if data in (GIVE_CONSENT, CANCEL, LEGACY_CHAT_CLOSE):
        return True
    for allowed in (AccountCb, LoginCb, WaliCb, MessageReportCb):
        with contextlib.suppress(TypeError, ValueError):
            allowed.unpack(data)
            return True
    withdrawals: tuple[tuple[Any, Any], ...] = (
        (ProfileCb, lambda cb: cb.action == ProfileAction.HIDE),
        (InterestCb, lambda cb: cb.action == InterestAction.CANCEL),
        (ChatCb, lambda cb: cb.action in (ChatAction.CLOSE, ChatAction.CLOSE_CONFIRM)),
    )
    for cls, check in withdrawals:
        with contextlib.suppress(TypeError, ValueError):
            if check(cls.unpack(data)):
                return True
    return False


class ConsentGate(BaseMiddleware):
    """Inner middleware of the private-chat router: a migrated account without consent sees the
    consent prompt instead of the action (see the module docstring)."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("user")
        if not gated(user):
            return await handler(event, data)
        state: FSMContext = data["state"]
        if isinstance(event, Message):
            if _free_message(event):
                return await handler(event, data)
            await _hold(event, state, data["session"])
            return None
        if isinstance(event, CallbackQuery):
            if _free_callback(event.data or ""):
                return await handler(event, data)
            await event.answer()
            await ask(event)
            return None
        return await handler(event, data)


async def on_give_consent(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await users.set_consent(session, user)
    await set_buttons(callback, None)
    # The new menu replaces the legacy keyboard still on the screen.
    await send(callback, texts.CONSENT_GIVEN, menu(user))
    pending = (await state.get_data()).get(PENDING_KEY) or {}
    await state.update_data({PENDING_KEY: None})
    for item in pending.get("messages", []):
        await chats.deliver(callback, session, user, item["text"], conversation_id=item.get("conversation_id"))
    if (wanted := pending.get("section")) and (section := navigation.section_for_text(wanted)) is not None:
        await navigation.open_section(callback, section, state, session, user)
    await profile.offer_completion(callback, session, user)


def create_router() -> Router:
    router = Router(name="consent")
    router.callback_query.register(on_give_consent, F.data == GIVE_CONSENT)
    return router
