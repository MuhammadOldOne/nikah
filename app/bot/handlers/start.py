"""/start with deep links: login_<token> (site login), wali_<token> (become a wali), p_<id> (profile)."""

from __future__ import annotations

import re

from aiogram import Router
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import LoginCb, WaliCb
from app.bot.handlers.profile import show_my_profile
from app.bot.render import esc
from app.bot.ui import edit, exit_flow
from app.config import get_settings
from app.enums import InterestStatus, LoginTokenState
from app.models import User
from app.services import auth, guardians, interests, profiles, users
from app.services.errors import (
    DomainError,
    GuardianInviteInvalid,
    GuardianSelf,
    ProfileRequired,
    TokenInvalid,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
_USER_ID = re.compile(r"[0-9]{1,18}")


def greeting() -> str:
    """The first greeting; it promises premoderation only when it is switched on."""
    premoderation = get_settings().PREMODERATION
    moderation = texts.MODERATION_WITH_PREMODERATION if premoderation else texts.MODERATION_WITHOUT_PREMODERATION
    return texts.GREETING.format(moderation=moderation)


async def greet(message: Message, user: User) -> None:
    text = greeting() if user.profile is None else texts.GREETING_BACK
    await message.answer(text, reply_markup=keyboards.main_menu())


async def on_start(
    message: Message, command: CommandObject, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await exit_flow(state)
    kind, _, value = (command.args or "").strip().partition("_")
    if kind == "login" and _TOKEN.fullmatch(value):
        await _login_prompt(message, session, value)
    elif kind == "wali" and _TOKEN.fullmatch(value):
        await _wali_prompt(message, session, user, value)
    elif kind == "p" and _USER_ID.fullmatch(value):
        await _show_profile(message, session, user, int(value))
    else:
        await greet(message, user)


async def _login_prompt(message: Message, session: AsyncSession, token: str) -> None:
    # Confirmation is an explicit button: a login link sent by someone else must not log a
    # stranger's browser into this account just because the link was opened.
    if await auth.login_token_state(session, token) not in (LoginTokenState.PENDING, LoginTokenState.CONFIRMED):
        await message.answer(esc(TokenInvalid().message), reply_markup=keyboards.main_menu())
        return
    site = esc(get_settings().SITE_URL)
    await message.answer(
        f"🔐 <b>Вход на сайт {site}</b>\n\n"
        "Подтвердите, только если вы сами только что нажали «Войти через Telegram» на сайте. "
        "Если ссылку прислал кто-то другой — нажмите «Отмена».",
        reply_markup=keyboards.login_confirm(token),
    )


async def on_login_confirm(callback: CallbackQuery, callback_data: LoginCb, session: AsyncSession, user: User) -> None:
    await callback.answer()
    try:
        await auth.confirm_login_token(session, callback_data.token, user)
    except DomainError as exc:
        await edit(callback, esc(exc.message))
        return
    await edit(callback, "✅ Вход на сайт подтверждён. Вернитесь во вкладку браузера.")


def _ward_ref(ward: User) -> str:
    # The wali knows the ward personally; without a profile name, the Telegram name identifies them.
    return render.person_ref(ward) if ward.profile is not None else f"{render.telegram_name(ward)} (#{ward.id})"


async def _wali_prompt(message: Message, session: AsyncSession, user: User, token: str) -> None:
    invite = await guardians.get_invite(session, token)
    if invite is None:
        await message.answer(esc(GuardianInviteInvalid().message), reply_markup=keyboards.main_menu())
        return
    if invite.ward_user_id == user.id:
        await message.answer(esc(GuardianSelf().message), reply_markup=keyboards.main_menu())
        return
    await message.answer(
        f"👁 <b>Приглашение стать вали</b>\n\n{_ward_ref(invite.ward)} просит вас стать вали.\n\n"
        "Вали получает в этом боте копии всей переписки подопечного на платформе — в обе стороны. "
        "Писать в чаты вали не может. Подопечный может отключить вали в любой момент.\n\n"
        "Подтвердите, если вы знаете этого человека и согласны.",
        reply_markup=keyboards.wali_confirm(token),
    )


async def on_wali_confirm(callback: CallbackQuery, callback_data: WaliCb, session: AsyncSession, user: User) -> None:
    await callback.answer()
    try:
        link = await guardians.accept_invite(session, callback_data.token, user)
    except DomainError as exc:
        await edit(callback, esc(exc.message))
        return
    ward = await users.get_by_id(session, link.ward_user_id)
    ward_ref = _ward_ref(ward) if ward is not None else "подопечного"
    await edit(
        callback,
        f"✅ Вы стали вали для {ward_ref}.\nКопии переписки будут приходить в этот чат с пометкой «👁 Копия переписки».",
    )


async def _show_profile(message: Message, session: AsyncSession, user: User, user_id: int) -> None:
    try:
        profile = await profiles.get_viewable_profile(session, user, user_id)
    except ProfileRequired:
        await message.answer(texts.PROFILE_REQUIRED, reply_markup=keyboards.create_profile())
        return
    except DomainError as exc:
        await message.answer(esc(exc.message), reply_markup=keyboards.main_menu())
        return
    if profile.user_id == user.id:
        await show_my_profile(message, session, user)
        return
    relation = await interests.get_relation(session, user, user_id)
    interest, conversation = relation.interest, relation.conversation
    pending = interest is not None and interest.status == InterestStatus.PENDING
    incoming_id = interest.id if interest is not None and pending and interest.to_user_id == user.id else None
    blocked = conversation is not None or (interest is not None and interest.status == InterestStatus.ACCEPTED)
    text = render.profile_card(profile, has_guardian=await guardians.has_active_guardian(session, user_id))
    if pending and incoming_id is None:
        text += "\n\n<i>Ваш отклик ждёт ответа.</i>"
    elif conversation is not None and not conversation.is_open:
        text += "\n\n<i>Общение завершено.</i>"
    await message.answer(
        text,
        reply_markup=keyboards.profile_view(
            user_id,
            can_send_interest=not pending and not blocked,
            incoming_interest_id=incoming_id,
            open_conversation_id=conversation.id if conversation is not None and conversation.is_open else None,
        ),
    )


def create_router() -> Router:
    """Confirmation callbacks of deep links (/start itself is registered in navigation)."""
    router = Router(name="start")
    router.callback_query.register(on_login_confirm, LoginCb.filter())
    router.callback_query.register(on_wali_confirm, WaliCb.filter())
    return router
