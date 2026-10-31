"""«Отклики»: incoming («Принять» / «Отклонить», legacy callback data int:acc / int:dec, so the
buttons of the legacy bot still work) and outgoing (status, «Отозвать»)."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render
from app.bot.callbacks import InterestAction, InterestCb
from app.bot.render import esc
from app.bot.ui import edit, message_html, send
from app.config import get_settings
from app.enums import InterestStatus
from app.models import User
from app.services import chat, guardians, interests
from app.services.errors import DomainError

INCOMING_SHOWN = 5
OUTGOING_SHOWN = 20


async def _outgoing_view(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    outgoing = await interests.list_outgoing(session, user, limit=OUTGOING_SHOWN)
    pending = [
        (item.id, render.person_label(item.to_user)) for item in outgoing if item.status == InterestStatus.PENDING
    ]
    return render.outgoing_interests(outgoing), keyboards.outgoing_interests(pending)


async def show_interests(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    incoming = [
        (item, profile)
        for item in await interests.list_incoming(session, user)
        if (profile := item.from_user.profile) is not None
    ]
    if not incoming:
        await send(event, "Входящих откликов пока нет.")
    else:
        await send(event, f"<b>Входящие отклики: {len(incoming)}</b>")
        shown = incoming[:INCOMING_SHOWN]
        with_guardian = await guardians.users_with_active_guardian(session, [item.from_user_id for item, _ in shown])
        for item, profile in shown:
            await send(
                event,
                render.interest_card(item, profile, has_guardian=item.from_user_id in with_guardian),
                keyboards.interest_decision(item.id),
            )
        if len(incoming) > INCOMING_SHOWN:
            await send(
                event,
                f"Показаны {INCOMING_SHOWN} последних. Остальные — на сайте: {esc(keyboards.site_url('/interests'))}",
            )
    text, markup = await _outgoing_view(session, user)
    await send(event, text, markup)


async def _accept(callback: CallbackQuery, session: AsyncSession, user: User, interest_id: int) -> None:
    conversation = await interests.accept(session, interest_id, user)
    # Plain text goes to the new chat (it is the only one unless MAX_OPEN_CONVERSATIONS > 1).
    await chat.set_bot_active_conversation(session, user, conversation.id)
    conversation = await chat.get_conversation_for_participant(session, conversation.id, user)
    sender = conversation.user_a if conversation.user_a_id != user.id else conversation.user_b
    await callback.answer("Принято")
    await edit(callback, f"{message_html(callback)}\n\n✅ Отклик принят.")
    if sender is None or sender.profile is None or user.profile is None:
        return
    opened = render.chat_opened(
        user.profile,
        sender.profile,
        conversation.id,
        several_allowed=get_settings().MAX_OPEN_CONVERSATIONS > 1,
        badge=await guardians.has_active_guardian(session, sender.id),
    )
    await send(callback, opened.text, opened.reply_markup)


async def on_interest_action(
    callback: CallbackQuery, callback_data: InterestCb, session: AsyncSession, user: User
) -> None:
    try:
        match callback_data.action:
            case InterestAction.ACCEPT:
                await _accept(callback, session, user, callback_data.interest_id)
            case InterestAction.DECLINE:
                await interests.decline(session, callback_data.interest_id, user)
                await callback.answer()
                await edit(callback, f"{message_html(callback)}\n\nВы отклонили отклик.")
            case InterestAction.CANCEL:
                await interests.cancel(session, callback_data.interest_id, user)
                await callback.answer("Отклик отозван")
                text, markup = await _outgoing_view(session, user)
                await edit(callback, text, markup)
    except DomainError as exc:  # e.g. OpenConversationLimit: shown verbatim
        await callback.answer(exc.message, show_alert=True)


def create_router() -> Router:
    router = Router(name="interests")
    router.callback_query.register(on_interest_action, InterestCb.filter())
    return router
