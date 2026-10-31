"""The witness's buttons under message copies («⚠️ Пометить», «✍️ Написать в чат», «🚫 Закрыть чат»,
legacy callback data w:flag / w:say / w:close) — in the witness group (WITNESS_CHAT_ID) or in an
admin's private chat when no group is set. ``services.witness`` checks admin rights on every action.

«Написать в чат» works as in the legacy bot: the bot asks for the text and the witness's next
message in the same chat is sent to both sides (the FSM is keyed by chat and user, so in a group
it is that admin's next message there). The question is a ForceReply, so the answer reaches the
bot also when the group's privacy mode is on.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.callbacks import WitnessAction, WitnessCb
from app.bot.render import esc
from app.bot.states import WitnessForm
from app.bot.ui import exit_flow
from app.models import User
from app.services import users, witness
from app.services.errors import DomainError, WitnessOnly

SAY_KEY = "witness_say"


async def on_witness_action(
    callback: CallbackQuery, callback_data: WitnessCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    try:
        match callback_data.action:
            case WitnessAction.FLAG:
                await witness.witness_flag(session, user, callback_data.target_id)
                await callback.answer(texts.WITNESS_FLAGGED)
            case WitnessAction.CLOSE:
                await witness.witness_close(session, user, callback_data.target_id)
                await callback.answer(texts.WITNESS_CLOSED)
            case WitnessAction.SAY:
                if not users.is_admin(user):  # never leave a dialog open for someone else
                    raise WitnessOnly()
                await exit_flow(state)
                await state.set_state(WitnessForm.text)
                await state.update_data({SAY_KEY: callback_data.target_id})
                await callback.answer()
                if isinstance(callback.message, Message):
                    await callback.message.answer(
                        texts.WITNESS_SAY_PROMPT.format(conversation_id=callback_data.target_id),
                        reply_markup=keyboards.witness_text_prompt(),
                    )
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)


async def on_witness_text(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    conversation_id = (await state.get_data()).get(SAY_KEY)
    await exit_flow(state)
    if conversation_id is None:
        return
    try:
        await witness.witness_say(session, user, conversation_id, message.text or "")
    except DomainError as exc:
        await message.answer(esc(exc.message))
        return
    await message.answer(texts.WITNESS_SAID)


async def on_witness_cancel(message: Message, state: FSMContext) -> None:
    await exit_flow(state)
    await message.answer(texts.CANCELLED)


def _witness_text(message: Message) -> bool:
    """The witness's answer: any text but a command or a menu button (those leave the dialog)."""
    text = message.text or ""
    return (
        bool(text) and not text.startswith("/") and text not in (*texts.MENU_LABELS, texts.MENU_RULES, texts.MENU_HELP)
    )


def create_router() -> Router:
    router = Router(name="witness")
    router.callback_query.register(on_witness_action, WitnessCb.filter())
    router.message.register(on_witness_cancel, WitnessForm.text, Command("cancel"))
    router.message.register(on_witness_text, WitnessForm.text, F.func(_witness_text))
    return router
