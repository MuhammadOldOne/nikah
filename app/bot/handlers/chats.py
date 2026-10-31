"""«💬 Чаты»: chat list, the active chat (relay of plain text), /stop, closing and reporting."""

from __future__ import annotations

import contextlib

from aiogram import Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import ChatAction, ChatCb
from app.bot.handlers import report
from app.bot.render import esc
from app.bot.ui import edit, send
from app.enums import MessageOrigin
from app.models import Conversation, User
from app.services import chat, guardians
from app.services.errors import ConversationClosed, ConversationNotFound, DomainError, PartnerDeleted

HISTORY_SIZE = 10
CHATS_SHOWN = 30


def _partner(conversation: Conversation, user: User) -> User | None:
    """The other participant (loaded by get_conversation_for_participant), None if deleted."""
    partner_id = conversation.partner_id(user.id)
    if partner_id is None:
        return None
    return conversation.user_a if conversation.user_a_id == partner_id else conversation.user_b


async def show_chats(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    summaries = await chat.list_conversations(session, user)
    if not summaries:
        await send(event, "Чатов пока нет. Чат открывается, когда вторая сторона принимает отклик.")
        return
    items = [(item.conversation.id, render.chat_button(item, user.bot_active_conversation_id)) for item in summaries]
    await send(
        event,
        "<b>Ваши чаты</b>\nВыберите чат, чтобы прочитать последние сообщения и ответить.",
        keyboards.chats_list(items[:CHATS_SHOWN]),
    )


async def _active_chat_header(session: AsyncSession, partner: User | None) -> str:
    has_guardian = partner is not None and await guardians.has_active_guardian(session, partner.id)
    return render.chat_header(partner, partner_has_guardian=has_guardian)


async def _open(callback: CallbackQuery, session: AsyncSession, user: User, conversation_id: int) -> None:
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    await callback.answer()
    partner = _partner(conversation, user)
    history = await chat.list_messages(session, conversation, limit=HISTORY_SIZE)
    await chat.mark_read(session, conversation, user)
    blocks = render.chat_history(history, user, partner) or ["<i>Сообщений пока нет.</i>"]
    for text in render.split_blocks([f"💬 <b>Чат</b> · {render.person_ref(partner)}", *blocks]):
        await send(callback, text)
    if conversation.is_open and partner is not None:
        await chat.set_bot_active_conversation(session, user, conversation.id)
        footer = await _active_chat_header(session, partner)
    else:
        footer = "🔒 Общение завершено." if partner is not None else "🔒 Собеседник удалил аккаунт."
    await send(callback, footer, keyboards.chat_actions(conversation.id, is_open=conversation.is_open))


async def _reply(callback: CallbackQuery, session: AsyncSession, user: User, conversation_id: int) -> None:
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    await chat.set_bot_active_conversation(session, user, conversation.id)
    await callback.answer()
    await send(callback, await _active_chat_header(session, _partner(conversation, user)))


async def on_chat_action(callback: CallbackQuery, callback_data: ChatCb, session: AsyncSession, user: User) -> None:
    conversation_id = callback_data.conversation_id
    try:
        match callback_data.action:
            case ChatAction.OPEN:
                await _open(callback, session, user, conversation_id)
            case ChatAction.REPLY:
                await _reply(callback, session, user, conversation_id)
            case ChatAction.CLOSE:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                if not conversation.is_open:
                    raise ConversationClosed()
                await callback.answer()
                await send(
                    callback,
                    f"Завершить общение? Собеседник: {render.person_ref(_partner(conversation, user))}.\n"
                    "Писать друг другу будет нельзя, и новый отклик этой паре отправить не получится.",
                    keyboards.close_confirm(conversation_id),
                )
            case ChatAction.CLOSE_CONFIRM:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                await chat.close_conversation(session, conversation, user)
                await callback.answer()
                await edit(callback, "🔒 Общение завершено.")
            case ChatAction.REPORT:
                conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
                partner_id = conversation.partner_id(user.id)
                if partner_id is None:
                    raise PartnerDeleted()
                await callback.answer()
                await report.ask_reason(callback, partner_id)
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)


def _replied_conversation(message: Message) -> int | None:
    """The conversation of a message notification the user answered with Telegram's «Reply»."""
    replied = message.reply_to_message
    if replied is None or replied.reply_markup is None:
        return None
    for row in replied.reply_markup.inline_keyboard:
        for button in row:
            with contextlib.suppress(TypeError, ValueError):
                data = ChatCb.unpack(button.callback_data or "")
                if data.action == ChatAction.REPLY:
                    return data.conversation_id
    return None


async def relay_text(message: Message, session: AsyncSession, user: User) -> None:
    """Plain text outside dialogs goes to the active chat — or, when it is a «Reply» to a message
    notification, to that notification's chat (which becomes active). Otherwise a hint."""
    text = message.text or ""
    if text.startswith("/"):
        await message.answer(texts.UNKNOWN_COMMAND)
        return
    active_id = user.bot_active_conversation_id
    conversation_id = _replied_conversation(message) or active_id
    if conversation_id is None:
        await message.answer(texts.NO_ACTIVE_CHAT, reply_markup=keyboards.main_menu())
        return
    try:
        await chat.send_message(session, conversation_id, user, text, MessageOrigin.TELEGRAM)
    except (ConversationClosed, ConversationNotFound) as exc:
        if conversation_id == active_id:
            await chat.set_bot_active_conversation(session, user, None)
        await message.answer(f"{esc(exc.message)} Выберите чат в «💬 Чаты».")
        return
    except DomainError as exc:
        await message.answer(f"⚠️ {esc(exc.message)}")
        return
    conversation = await chat.get_conversation_for_participant(session, conversation_id, user)
    # Replying means the partner's messages were read. No confirmation otherwise: it is a chat.
    await chat.mark_read(session, conversation, user)
    if conversation_id != active_id:
        await chat.set_bot_active_conversation(session, user, conversation_id)
        header = await _active_chat_header(session, _partner(conversation, user))
        await message.answer(f"Ответ отправлен.\n{header}")


async def stop_chat(message: Message, session: AsyncSession, user: User) -> None:
    if user.bot_active_conversation_id is None:
        await message.answer(texts.CHAT_NOT_ACTIVE, reply_markup=keyboards.main_menu())
        return
    await chat.set_bot_active_conversation(session, user, None)
    await message.answer(texts.CHAT_STOPPED, reply_markup=keyboards.main_menu())


def create_router() -> Router:
    router = Router(name="chats")
    router.callback_query.register(on_chat_action, ChatCb.filter())
    return router
