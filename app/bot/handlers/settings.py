"""«⚙️ Настройки»: Telegram notifications and the wali (guardian) link."""

from __future__ import annotations

from urllib.parse import urlencode

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import SettingsAction, SettingsCb
from app.bot.render import esc
from app.bot.ui import edit, send
from app.models import User
from app.services import guardians, users
from app.services.errors import DomainError


async def _view(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    active = await guardians.get_active_guardian(session, user.id)
    pending = await guardians.get_pending_invite(session, user)
    wards = await guardians.list_wards(session, user)
    notify = "включены" if user.notify_telegram else "выключены"
    lines = [
        "<b>⚙️ Настройки</b>",
        "",
        f"🔔 Уведомления в Telegram: <b>{notify}</b>",
        "Новые сообщения, отклики и решения модерации приходят в этот чат.",
        "",
    ]
    if active is not None and active.guardian is not None:
        lines.append(f"👁 Вали: <b>подключён</b> — {render.telegram_name(active.guardian)}")
    else:
        lines.append("👁 Вали: <b>не подключён</b>")
    if pending is not None:
        lines.append(f"Приглашение ждёт подтверждения до {render.msk(pending.expires_at)}.")
    lines.append(texts.WALI_ABOUT)
    if wards:
        lines += ["", "Вы — вали для: " + ", ".join(render.person_ref(link.ward) for link in wards)]
    markup = keyboards.settings(
        notify=user.notify_telegram, wali_connected=active is not None, wali_pending=pending is not None
    )
    return "\n".join(lines), markup


async def show_settings(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    text, markup = await _view(session, user)
    await send(event, text, markup)


def _share_markup(link: str) -> InlineKeyboardMarkup:
    query = urlencode({"url": link, "text": "Приглашение стать моим вали на платформе «Истина»"})
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 Переслать вали", url=f"https://t.me/share/url?{query}")]]
    )


async def on_settings_action(
    callback: CallbackQuery, callback_data: SettingsCb, session: AsyncSession, user: User
) -> None:
    match callback_data.action:
        case SettingsAction.NOTIFY_ON | SettingsAction.NOTIFY_OFF:
            await users.set_notify(session, user, callback_data.action == SettingsAction.NOTIFY_ON)
            await callback.answer("Уведомления включены" if user.notify_telegram else "Уведомления выключены")
        case SettingsAction.WALI_INVITE:
            try:
                link = await guardians.create_invite(session, user)
            except DomainError as exc:
                await callback.answer(exc.message, show_alert=True)
                return
            await callback.answer()
            await send(callback, texts.WALI_INVITE_INSTRUCTIONS.format(link=esc(link)), _share_markup(link))
        case SettingsAction.WALI_REVOKE:
            changed = await guardians.revoke(session, user)
            await callback.answer("Вали отключён" if changed else "Вали не был подключён")
    text, markup = await _view(session, user)
    await edit(callback, text, markup)


def create_router() -> Router:
    router = Router(name="settings")
    router.callback_query.register(on_settings_action, SettingsCb.filter())
    return router
