"""«Мой чат»: the open chat, the plain-text relay, closing and reporting.

As in the legacy bot, a person with exactly one open chat simply writes to the bot: any plain
text (not a menu label or command) goes to that chat. With MAX_OPEN_CONVERSATIONS > 1 and several
open chats, the person picks the active chat («Написать» / «Ответить» / the list in «Мой чат»), and
a Telegram «Reply» to a message of a chat goes to that chat. Relayed messages carry the legacy
one-tap «Пожаловаться» (a report on that message).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import LEGACY_CHAT_CLOSE, ChatAction, ChatCb, MessageReportCb
from app.bot.handlers import report
from app.bot.render import esc
from app.bot.ui import edit, exit_flow, menu, send
from app.enums import MessageOrigin, ReportReason
from app.models import Conversation, User
from app.models import Message as ChatMessage
from app.services import chat, guardians, moderation, profiles
from app.services.errors import (
    ConversationClosed,
    ConversationNotFound,
    DomainError,
    MessageNotFound,
    PartnerDeleted,
    ProfileUnderReview,
)

HISTORY_SIZE = 10
CHATS_SHOWN = 30


def _partner(conversation: Conversation, user: User) -> User | None:
    """The other participant (loaded by the chat services), None if deleted."""
    partner_id = conversation.partner_id(user.id)
    if partner_id is None:
        return None
    return conversation.user_a if conversation.user_a_id == partner_id else conversation.user_b


def _partner_card(partner: User | None, *, has_guardian: bool) -> str:
    """The partner's card; content a moderator has not approved yet is not shown (as on the site)."""
    if partner is None or partner.profile is None:
        return "<i>Собеседник удалил аккаунт.</i>"
    if not profiles.has_approved_content(partner.profile):
        return f"{render.profile_ref(partner.profile)}\n<i>{ProfileUnderReview().message}</i>"
    return render.profile_card(partner.profile, has_guardian=has_guardian, about_limit=render.ABOUT_SHORT_LIMIT)


async def show_my_chat(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    """The only open chat: the partner's card, the last messages and «Завершить чат». Several open
    chats: choose one. None: the legacy hint."""
    open_chats = await chat.open_conversations(session, user)
    if not open_chats:
        await send(event, texts.NO_OPEN_CHATS, menu(user))
        return
    if len(open_chats) > 1:
        await _send_chat_list(event, session, user, "<b>Открытые чаты</b>\nВыберите, кому пишете:")
        return
    conversation = open_chats[0]
    partner = _partner(conversation, user)
    has_guardian = partner is not None and await guardians.has_active_guardian(session, partner.id)
    history = await chat.list_messages(session, conversation, limit=HISTORY_SIZE)
    await chat.mark_read(session, conversation, user)
    blocks = render.chat_history(history, user, partner) or ["<i>Сообщений пока нет.</i>"]
    for text in render.split_blocks([_partner_card(partner, has_guardian=has_guardian), *blocks]):
        await send(event, text)
    await send(
        event,
        render.my_chat_footer(partner, partner_has_guardian=has_guardian),
        keyboards.chat_actions(conversation.id, is_open=True),
    )


async def _send_chat_list(event: Message | CallbackQuery, session: AsyncSession, user: User, title: str) -> None:
    summaries = [item for item in await chat.list_conversations(session, user) if item.conversation.is_open]
    items = [(item.conversation.id, render.chat_button(item, user.bot_active_conversation_id)) for item in summaries]
    await send(event, title, keyboards.chats_list(items[:CHATS_SHOWN]))


async def _active_chat_header(session: AsyncSession, partner: User | None, *, flow_cancelled: bool = False) -> str:
    has_guardian = partner is not None and await guardians.has_active_guardian(session, partner.id)
    header = render.chat_header(partner, partner_has_guardian=has_guardian)
    # The bot now says «Вы пишете: …»: a dialog left open would otherwise capture the next text.
    return f"{texts.CANCELLED}\n{header}" if flow_cancelled else header


async def _open(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, conversation_id: int
) -> None:
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    await callback.answer()
    partner = _partner(conversation, user)
    history = await chat.list_messages(session, conversation, limit=HISTORY_SIZE)
    await chat.mark_read(session, conversation, user)
    blocks = render.chat_history(history, user, partner) or ["<i>Сообщений пока нет.</i>"]
    for text in render.split_blocks([f"<b>Чат</b> · {render.person_ref(partner)}", *blocks]):
        await send(callback, text)
    if conversation.is_open and partner is not None:
        await chat.set_bot_active_conversation(session, user, conversation.id)
        footer = await _active_chat_header(session, partner, flow_cancelled=await exit_flow(state))
    else:
        footer = "Общение завершено." if partner is not None else "Собеседник удалил аккаунт."
    await send(callback, footer, keyboards.chat_actions(conversation.id, is_open=conversation.is_open))


async def _reply(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, conversation_id: int
) -> None:
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    await chat.set_bot_active_conversation(session, user, conversation.id)
    await callback.answer()
    partner = _partner(conversation, user)
    header = await _active_chat_header(session, partner, flow_cancelled=await exit_flow(state))
    # The chat's buttons also tie the header to the chat: a «Reply» to it goes there.
    await send(callback, header, keyboards.chat_actions(conversation.id, is_open=True))


async def ask_close(callback: CallbackQuery, session: AsyncSession, conversation: Conversation, user: User) -> None:
    if not conversation.is_open:
        raise ConversationClosed()
    note = await chat.close_note(session, conversation, user)
    await callback.answer()
    await send(
        callback,
        f"Завершить общение? Собеседник: {render.person_ref(_partner(conversation, user))}.\n{note}",
        keyboards.close_confirm(conversation.id),
    )


async def on_chat_action(
    callback: CallbackQuery, callback_data: ChatCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    conversation_id = callback_data.conversation_id
    try:
        match callback_data.action:
            case ChatAction.OPEN:
                await _open(callback, state, session, user, conversation_id)
            case ChatAction.REPLY:
                await _reply(callback, state, session, user, conversation_id)
            case ChatAction.CLOSE:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                await ask_close(callback, session, conversation, user)
            case ChatAction.CLOSE_CONFIRM:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                await chat.close_conversation(session, conversation, user)
                await callback.answer()
                await edit(callback, "Вы завершили общение.")
            case ChatAction.REPORT:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                partner = _partner(conversation, user)
                if partner is None or partner.profile is None:
                    raise PartnerDeleted()
                await callback.answer()
                await report.ask_reason(callback, partner.profile.number)
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)


async def on_message_report(
    callback: CallbackQuery, callback_data: MessageReportCb, session: AsyncSession, user: User
) -> None:
    """Legacy «Пожаловаться» under a relayed message (rep:<message_id>): one tap, no dialog."""
    message = await session.get(ChatMessage, callback_data.message_id)
    try:
        if message is None or message.sender_id is None:
            raise MessageNotFound()
        await moderation.create_report(
            session, user, message.sender_id, ReportReason.OTHER, message_id=callback_data.message_id
        )
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer(texts.MESSAGE_REPORTED, show_alert=True)


# --- The plain-text relay ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """What a Telegram «Reply» points at: a chat (its buttons) or a relayed message («Пожаловаться»)."""

    conversation_id: int | None = None
    message_id: int | None = None


def _reply_target(message: Message) -> ReplyTarget | None:
    """The chat a «Reply» is addressed to: the replied message carries the buttons of exactly one
    conversation (a relayed message, «Написать», the chat header…)."""
    replied = message.reply_to_message
    if replied is None or replied.reply_markup is None:
        return None
    conversation_ids: set[int] = set()
    message_ids: set[int] = set()
    for row in replied.reply_markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            with contextlib.suppress(TypeError, ValueError):
                chat_data = ChatCb.unpack(data)
                if chat_data.action != ChatAction.OPEN:  # the chat list names many chats
                    conversation_ids.add(chat_data.conversation_id)
            with contextlib.suppress(TypeError, ValueError):
                message_ids.add(MessageReportCb.unpack(data).message_id)
    if len(conversation_ids) == 1:
        return ReplyTarget(conversation_id=conversation_ids.pop())
    if not conversation_ids and len(message_ids) == 1:
        return ReplyTarget(message_id=message_ids.pop())
    return None


async def _resolve(session: AsyncSession, target: ReplyTarget) -> int | None:
    if target.conversation_id is not None:
        return target.conversation_id
    relayed = await session.get(ChatMessage, target.message_id)
    return relayed.conversation_id if relayed is not None else None


def _only_legacy_chat_buttons(replied: Message) -> bool:
    """The legacy bot's «Чат открыт…» and «Мой чат» messages carry only «Завершить общение»
    (chat:close, no chat id): they belong to the only open chat, and say «пишите сюда»."""
    data = [button.callback_data for row in replied.reply_markup.inline_keyboard for button in row]
    return bool(data) and all(item == LEGACY_CHAT_CLOSE for item in data)


def _misdirected_reply(message: Message) -> str | None:
    """A hint when the text is a «Reply» to a bot message that belongs to no chat (a wali copy, an
    interest or profile card, a menu): it must not silently go to some chat instead."""
    replied = message.reply_to_message
    if replied is None or replied.from_user is None or not replied.from_user.is_bot:
        return None
    if (replied.text or "").startswith(render.GUARDIAN_COPY_TITLE):
        return texts.REPLY_TO_GUARDIAN_COPY
    if replied.reply_markup is not None and not _only_legacy_chat_buttons(replied):
        return texts.REPLY_NOT_TO_CHAT
    return None


@dataclass(frozen=True, slots=True)
class Destination:
    """Where a plain text goes: ``conversation_id`` (None: the only open chat, or the active one),
    or nowhere, with ``refusal`` as the hint (a «Reply» to a bot message of no chat)."""

    conversation_id: int | None = None
    refusal: str | None = None


async def destination(message: Message, session: AsyncSession) -> Destination:
    target = _reply_target(message)
    if target is None:
        return Destination(refusal=_misdirected_reply(message))
    return Destination(conversation_id=await _resolve(session, target))


def chat_reply_filter(message: Message) -> bool:
    """Filter: a text «Reply» (not a command) to a message of one chat."""
    text = message.text or ""
    return bool(text) and not text.startswith("/") and _reply_target(message) is not None


async def on_chat_reply(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """A «Reply» to a chat's message is meant for that chat even while a dialog is open (checked
    before dialog states): the dialog ends instead of taking the text as its answer."""
    cancelled = await exit_flow(state)
    await relay_text(message, session, user, flow_cancelled=cancelled)


async def relay_text(message: Message, session: AsyncSession, user: User, *, flow_cancelled: bool = False) -> None:
    """Plain text outside dialogs: to the chat of a replied message, else to the only open chat
    (or the active one when there are several). Otherwise a hint."""
    text = message.text or ""
    if text.startswith("/"):
        await message.answer(texts.UNKNOWN_COMMAND)
        return
    where = await destination(message, session)
    if where.refusal:
        await message.answer(where.refusal)
        return
    await deliver(message, session, user, text, conversation_id=where.conversation_id, flow_cancelled=flow_cancelled)


async def deliver(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    text: str,
    *,
    conversation_id: int | None = None,
    flow_cancelled: bool = False,
) -> None:
    """Send ``text`` to ``conversation_id`` or, if None, to the only open chat (the active one
    when there are several). Silent on success, as a chat should be."""
    open_chats = await chat.open_conversations(session, user)
    active_id = user.bot_active_conversation_id
    if conversation_id is None:
        if len(open_chats) == 1:
            conversation_id = open_chats[0].id
        elif any(item.id == active_id for item in open_chats):
            conversation_id = active_id
        elif not open_chats:
            await send(event, texts.NO_OPEN_CHAT, menu(user))
            return
        else:
            await _send_chat_list(event, session, user, texts.CHOOSE_CHAT)
            return
    try:
        sent = await chat.send_message(session, conversation_id, user, text, MessageOrigin.TELEGRAM)
    except (ConversationClosed, ConversationNotFound) as exc:
        if conversation_id == active_id:
            await chat.set_bot_active_conversation(session, user, None)
        await send(event, f"{esc(exc.message)} Откройте «Мой чат».")
        return
    except DomainError as exc:
        await send(event, f"⚠️ {esc(exc.message)}")
        return
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    # Replying means the partner's messages were read.
    await chat.mark_read(session, conversation, user)
    if sent.contacts_hidden:
        await send(event, chat.CONTACTS_HIDDEN_NOTICE)
    notice = f"{texts.CANCELLED}\n" if flow_cancelled else ""
    if len(open_chats) > 1 and conversation_id != active_id:
        await chat.set_bot_active_conversation(session, user, conversation_id)
        header = await _active_chat_header(session, _partner(conversation, user))
        await send(event, f"{notice}Ответ отправлен.\n{header}", keyboards.chat_actions(conversation_id, is_open=True))
    elif flow_cancelled:
        await send(event, f"{notice}Ответ отправлен.")


async def stop_chat(message: Message, session: AsyncSession, user: User) -> None:
    open_chats = await chat.open_conversations(session, user)
    if len(open_chats) == 1:
        await message.answer(texts.STOP_SINGLE_CHAT, reply_markup=menu(user))
        return
    if user.bot_active_conversation_id is None:
        await message.answer(texts.CHAT_NOT_ACTIVE, reply_markup=menu(user))
        return
    await chat.set_bot_active_conversation(session, user, None)
    await message.answer(texts.CHAT_STOPPED, reply_markup=menu(user))


def create_router() -> Router:
    router = Router(name="chats")
    router.callback_query.register(on_chat_action, ChatCb.filter())
    router.callback_query.register(on_message_report, MessageReportCb.filter())
    return router
