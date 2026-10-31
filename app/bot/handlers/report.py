"""Reports (жалобы) on a person from a profile card or a chat: reason → optional comment →
moderation. People are named by profile number. (A report on one chat message is the one-tap
«Пожаловаться» under it: handlers.chats.)"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.callbacks import REPORT_SEND, ReportCb
from app.bot.render import esc
from app.bot.states import ReportForm
from app.bot.ui import edit, exit_flow, send, set_buttons
from app.enums import ReportReason, label
from app.models import User
from app.services import moderation, profiles
from app.services.errors import DomainError, InvalidInput, UserNotFound

REPORT_KEY = "report"


async def ask_reason(event: Message | CallbackQuery, number: int) -> None:
    await send(event, f"<b>Жалоба на анкету №{number}</b>\nВыберите причину:", keyboards.report_reasons(number))


async def on_reason(callback: CallbackQuery, callback_data: ReportCb, state: FSMContext) -> None:
    await exit_flow(state)
    await state.set_state(ReportForm.comment)
    await state.update_data({REPORT_KEY: {"number": callback_data.number, "reason": callback_data.reason.value}})
    await callback.answer()
    await edit(
        callback,
        f"<b>Жалоба на анкету №{callback_data.number}</b>\nПричина: {label(callback_data.reason)}\n\n"
        f"{texts.REPORT_COMMENT_PROMPT}",
        keyboards.report_comment(),
    )


async def on_comment(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _submit(message, state, session, user, message.text)


async def on_send_without_comment(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    if await state.get_state() != ReportForm.comment.state:
        await callback.answer(texts.STALE_BUTTON)
        return
    await callback.answer()
    await set_buttons(callback, None)
    await _submit(callback, state, session, user, None)


async def _submit(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User, comment: str | None
) -> None:
    report = (await state.get_data()).get(REPORT_KEY)
    if report is None:
        await exit_flow(state)
        await send(event, texts.STALE_BUTTON)
        return
    target = await profiles.get_profile_by_number(session, report["number"])
    try:
        if target is None:
            raise UserNotFound()
        await moderation.create_report(session, user, target.user_id, ReportReason(report["reason"]), comment)
    except InvalidInput as exc:
        await send(event, f"⚠️ {esc(exc.message)}", keyboards.report_comment())
        return
    except DomainError as exc:
        await exit_flow(state)
        await send(event, esc(exc.message))
        return
    await exit_flow(state)
    await send(event, texts.REPORT_SENT)


def create_router() -> Router:
    router = Router(name="report")
    router.message.register(on_comment, ReportForm.comment, F.text)
    router.callback_query.register(on_reason, ReportCb.filter())
    router.callback_query.register(on_send_without_comment, F.data == REPORT_SEND)
    return router
