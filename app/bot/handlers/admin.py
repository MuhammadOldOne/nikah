"""Moderation from Telegram: the legacy /admin queue, /ban and /unban (Telegram id, «№512» or
«#512»), approve/reject profiles (also the legacy adm:appr / adm:rej buttons) and resolve reports.

Every handler of this router runs behind AdminMiddleware, which checks ``users.is_admin`` on each
update; callback data only says *what* to do, never *who may* do it.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import (
    AdminProfileAction,
    AdminProfileCb,
    AdminReportAction,
    AdminReportCb,
    LegacyReviewAction,
    LegacyReviewCb,
)
from app.bot.middlewares import AdminMiddleware
from app.bot.render import esc
from app.bot.states import AdminForm
from app.bot.ui import edit, exit_flow, message_html, send, set_buttons
from app.enums import ProfileStatus, ReportAction, label
from app.models import Profile, User
from app.services import moderation, profiles, users
from app.services.errors import AdminBanDenied, DomainError, InvalidInput, UserNotFound

REJECT_KEY = "reject"
STALE_PROFILE = "Анкета уже рассмотрена или изменена после отправки — смотрите актуальную карточку."
QUEUE_SHOWN = 20
# A legacy card was sent right after the profile was created; seconds are truncated on both sides.
LEGACY_CARD_SLACK = timedelta(seconds=1)


async def on_queue(message: Message, session: AsyncSession) -> None:
    """Legacy /admin: the moderation queue, one card per pending profile."""
    pending = await profiles.list_pending(session, limit=QUEUE_SHOWN + 1)
    if not pending:
        await message.answer(texts.ADMIN_QUEUE_EMPTY)
        return
    count = f"{QUEUE_SHOWN}+" if len(pending) > QUEUE_SHOWN else str(len(pending))
    await message.answer(f"Анкет в очереди: {count}. Высылаю...")
    for profile in pending[:QUEUE_SHOWN]:
        card = render.moderation_card(profile)
        await message.answer(card.text, reply_markup=card.reply_markup)
    if len(pending) > QUEUE_SHOWN:
        await message.answer(
            f"Показаны первые {QUEUE_SHOWN}. Остальные — после них (/admin) или на сайте: "
            f"{esc(keyboards.site_url('/admin/moderation'))}"
        )


async def on_ban(message: Message, command: CommandObject, session: AsyncSession) -> None:
    await _set_ban(message, command, session, banned=True)


async def on_unban(message: Message, command: CommandObject, session: AsyncSession) -> None:
    await _set_ban(message, command, session, banned=False)


async def _set_ban(message: Message, command: CommandObject, session: AsyncSession, *, banned: bool) -> None:
    """/ban and /unban take a Telegram id (as in the legacy bot) or a profile number «№512» / «#512»."""
    ref = (command.args or "").strip()
    if not ref:
        await message.answer(f"Использование: /{command.command} <Telegram ID или №анкеты>")
        return
    try:
        target = await users.resolve_user_ref(session, ref)
        if target is None:
            raise UserNotFound()
        if banned:
            await users.ban(session, target)
        else:
            await users.unban(session, target)
    except DomainError as exc:
        await message.answer(esc(exc.message))
        return
    who = f"{esc(ref)} ({render.person_name(target)})" if target.profile is not None else esc(ref)
    await message.answer(f"Пользователь {who} {'заблокирован' if banned else 'разблокирован'}.")


async def _current_pending(session: AsyncSession, user_id: int, version: int) -> Profile | None:
    """The profile if it is still pending with the content the admin card showed."""
    profile = await profiles.get_profile(session, user_id)
    if profile is None or profile.status != ProfileStatus.PENDING or render.profile_version(profile) != version:
        return None
    return profile


async def on_profile_action(
    callback: CallbackQuery, callback_data: AdminProfileCb, state: FSMContext, session: AsyncSession
) -> None:
    user_id, version = callback_data.user_id, callback_data.version
    if await _current_pending(session, user_id, version) is None:
        await callback.answer(STALE_PROFILE, show_alert=True)
        await set_buttons(callback, None)
        return
    try:
        await _apply_profile_action(callback, callback_data, state, session)
    except DomainError as exc:  # e.g. another admin acted at the same moment
        await callback.answer(exc.message, show_alert=True)


async def _apply_profile_action(
    callback: CallbackQuery, callback_data: AdminProfileCb, state: FSMContext, session: AsyncSession
) -> None:
    user_id, version = callback_data.user_id, callback_data.version
    match callback_data.action:
        case AdminProfileAction.APPROVE:
            await profiles.approve(session, user_id)
            await callback.answer("Одобрено")
            await edit(callback, f"{message_html(callback)}\n\n✅ <b>Одобрено</b>")
        case AdminProfileAction.REJECT:
            await callback.answer()
            await set_buttons(callback, keyboards.admin_reject_reasons(user_id, version))
        case AdminProfileAction.BACK:
            await callback.answer()
            await set_buttons(callback, keyboards.admin_profile(user_id, version))
        case AdminProfileAction.PRESET if 0 <= callback_data.preset < len(texts.ADMIN_REJECT_PRESETS):
            title, note = texts.ADMIN_REJECT_PRESETS[callback_data.preset]
            await profiles.reject(session, user_id, note)
            await callback.answer("Отклонено")
            await edit(callback, f"{message_html(callback)}\n\n❌ <b>Отклонено:</b> {esc(title)}")
        case AdminProfileAction.OTHER:
            await exit_flow(state)
            await state.set_state(AdminForm.reject_reason)
            await state.update_data({REJECT_KEY: {"user_id": user_id, "version": version}})
            await callback.answer()
            await set_buttons(callback, None)
            profile = await profiles.get_profile(session, user_id)
            title = render.profile_ref(profile) if profile is not None else "Анкета"
            await send(callback, f"{title}. {texts.ADMIN_REJECT_REASON_PROMPT}", keyboards.cancel_only())
        case _:
            await callback.answer(texts.STALE_BUTTON)


async def on_reject_reason(message: Message, state: FSMContext, session: AsyncSession) -> None:
    target = (await state.get_data()).get(REJECT_KEY)
    if target is None:
        await exit_flow(state)
        return
    if await _current_pending(session, target["user_id"], target["version"]) is None:
        await exit_flow(state)
        await message.answer(STALE_PROFILE)
        return
    try:
        await profiles.reject(session, target["user_id"], message.text or "")
    except InvalidInput as exc:
        await message.answer(f"⚠️ {esc(exc.message)}")
        return
    await exit_flow(state)
    profile = await profiles.get_profile(session, target["user_id"])
    title = render.profile_ref(profile) if profile is not None else "Анкета"
    await message.answer(f"{title} отклонена. Автор получит причину.")


async def on_legacy_review(callback: CallbackQuery, callback_data: LegacyReviewCb, session: AsyncSession) -> None:
    """Legacy «Одобрить» / «Отклонить» (adm:appr / adm:rej:<number>) under a card of the old bot. It
    acts only on the content that card showed: if the profile changed since, the current card is
    sent instead. «Отклонить» asks for the reason, as new cards do."""
    profile = await profiles.get_profile_by_number(session, callback_data.number)
    if profile is None:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return
    if profile.status != ProfileStatus.PENDING:
        await callback.answer(f"Уже обработана: {label(profile.status)}.", show_alert=True)
        await set_buttons(callback, None)
        return
    sent_at = callback.message.date if isinstance(callback.message, Message) else None
    if sent_at is None or profile.submitted_at is None or profile.submitted_at > sent_at + LEGACY_CARD_SLACK:
        await callback.answer(STALE_PROFILE, show_alert=True)
        await set_buttons(callback, None)
        card = render.moderation_card(profile)
        await send(callback, card.text, card.reply_markup)
        return
    version = render.profile_version(profile)
    if callback_data.action == LegacyReviewAction.REJECT:
        await callback.answer()
        await set_buttons(callback, keyboards.admin_reject_reasons(profile.user_id, version))
        return
    try:
        await profiles.approve(session, profile.user_id)
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer("Одобрено")
    await set_buttons(callback, None)
    await send(callback, f"Анкета №{profile.number} одобрена.")


async def on_report_action(
    callback: CallbackQuery, callback_data: AdminReportCb, session: AsyncSession, user: User
) -> None:
    report_id = callback_data.report_id
    match callback_data.action:
        case AdminReportAction.BAN:
            await callback.answer()
            await set_buttons(callback, keyboards.admin_ban_confirm(report_id))
            return
        case AdminReportAction.BACK:
            await callback.answer()
            await set_buttons(callback, keyboards.admin_report(report_id))
            return
    action = ReportAction(callback_data.action.value)
    try:
        await moderation.resolve_report(session, report_id, user, action)
    except AdminBanDenied as exc:  # the report stays open: other decisions remain available
        await callback.answer(exc.message, show_alert=True)
        await set_buttons(callback, keyboards.admin_report(report_id))
        return
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        await set_buttons(callback, None)
        return
    await callback.answer("Готово")
    await edit(callback, f"{message_html(callback)}\n\n✅ <b>Решение:</b> {label(action)}")


def create_router() -> Router:
    router = Router(name="admin")
    router.message.middleware(AdminMiddleware())
    router.callback_query.middleware(AdminMiddleware())
    router.message.register(on_queue, Command("admin"))
    router.message.register(on_ban, Command("ban"))
    router.message.register(on_unban, Command("unban"))
    router.callback_query.register(on_profile_action, AdminProfileCb.filter())
    router.callback_query.register(on_legacy_review, LegacyReviewCb.filter())
    router.callback_query.register(on_report_action, AdminReportCb.filter())
    router.message.register(on_reject_reason, AdminForm.reject_reason, F.text)
    return router
