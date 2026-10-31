"""«Смотреть анкеты»: the legacy browsing card (⬅️ N/M ➡️, «Написать (заинтересоваться)»,
«Закрыть»), an interest with an optional message, reports and an age/city filter.

Navigation buttons carry the card's profile number and position, so they keep working after a
restart; the position is re-computed from the current catalog (profiles come and go)."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import CatalogAction, CatalogCb
from app.bot.handlers import report
from app.bot.render import esc
from app.bot.states import CatalogForm
from app.bot.ui import edit, exit_flow, send, set_buttons
from app.models import Profile, User
from app.schemas import CatalogFilters, first_error
from app.services import chat, guardians, interests, profiles
from app.services.errors import DomainError, InvalidInput, ProfileNotFound, ProfilePending, ProfileRequired

FILTERS_KEY = "catalog_filters"
FILTER_DRAFT_KEY = "filter_draft"
INTEREST_KEY = "interest_to"  # profile number the interest goes to
_NUMBER = re.compile(r"\d+")


def _filters(data: dict[str, Any]) -> CatalogFilters:
    return CatalogFilters.model_validate(data.get(FILTERS_KEY) or {})


def _filter_note(filters: CatalogFilters) -> str:
    parts = []
    if filters.age_min is not None and filters.age_max is not None:
        parts.append(f"{filters.age_min}–{filters.age_max} лет")
    elif filters.age_min is not None:
        parts.append(f"от {filters.age_min} лет")
    elif filters.age_max is not None:
        parts.append(f"до {filters.age_max} лет")
    if filters.city:
        parts.append(esc(filters.city))
    return ", ".join(parts)


async def show_catalog(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    *,
    page: int = 1,
    replace: bool = False,
) -> None:
    """One card at page ``page`` (1-based); ``replace``: edit the callback's card in place."""
    filters = _filters(await state.get_data())
    try:
        items, total = await profiles.list_catalog(session, user, filters, page=max(page, 1), per_page=1)
        if not items and total:  # the list got shorter since the button was shown
            page = total
            items, total = await profiles.list_catalog(session, user, filters, page=page, per_page=1)
    except ProfileRequired:
        await send(event, texts.PROFILE_REQUIRED, keyboards.create_profile())
        return
    except ProfilePending:
        await send(event, texts.NOT_APPROVED)
        return
    except DomainError as exc:
        await send(event, esc(exc.message))
        return
    note = _filter_note(filters)
    if items:
        profile = items[0]
        has_guardian = await guardians.has_active_guardian(session, profile.user_id)
        text = render.profile_card(profile, has_guardian=has_guardian)
        if note:
            text += f"\n\n<i>Фильтр: {note}</i>"
        markup = keyboards.browse(max(page, 1), total, profile.number, has_filter=bool(note))
    else:
        text = f"По фильтру «{note}» анкет пока нет." if note else texts.CATALOG_EMPTY
        markup = keyboards.browse_end(has_filter=True) if note else None
    if replace and isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await send(event, text, markup)


async def _target_page(state: FSMContext, session: AsyncSession, user: User, callback_data: CatalogCb) -> int:
    """The page ⬅️/➡️ leads to, from where the shown card is now. If it left the catalog
    (reported, hidden…), the next card has moved up to the shown card's page."""
    position = None
    shown = await profiles.get_profile_by_number(session, callback_data.number) if callback_data.number else None
    if shown is not None:
        with contextlib.suppress(DomainError):  # the gate is reported by show_catalog
            filters = _filters(await state.get_data())
            position = await profiles.catalog_position(session, user, filters, shown.user_id)
    if callback_data.action == CatalogAction.NEXT:
        return position + 1 if position is not None else callback_data.page
    return position - 1 if position is not None else callback_data.page - 1


async def _interest_target(callback: CallbackQuery, session: AsyncSession, user: User, number: int) -> Profile | None:
    """The profile an interest button points at, once the read-only checks of send_interest pass;
    otherwise the callback is answered with the reason (before any message is asked for)."""
    try:
        profiles.require_active(user.profile)
        target = await profiles.get_profile_by_number(session, number)
        if target is None:
            raise ProfileNotFound()
        await interests.check_can_send(session, user, target.user_id)
    except ProfilePending:
        await callback.answer(texts.NOT_APPROVED, show_alert=True)
        return None
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        return None
    return target


async def start_interest(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    number: int,
    *,
    one_tap: bool = False,
) -> None:
    """«Написать (заинтересоваться)»: refused at once if the interest cannot be sent; otherwise an
    optional message is asked for. While the person has an open chat, any text goes to that chat
    (LEGACY_BOT.md §2.7), so the interest is sent at once without a message, like the legacy
    one-tap button (``one_tap``: an old br:int button)."""
    target = await _interest_target(callback, session, user, number)
    if target is None:
        return
    with_chat = bool(await chat.open_conversations(session, user))
    if one_tap or with_chat:
        try:
            await interests.send_interest(session, user, target.user_id)
        except DomainError as exc:
            await callback.answer(exc.message, show_alert=True)
            return
        sent = texts.INTEREST_SENT.format(number=target.number)
        await callback.answer(f"{sent} {texts.INTEREST_SENT_WHILE_CHATTING}" if with_chat else sent, show_alert=True)
        return
    await exit_flow(state)
    await state.set_state(CatalogForm.interest_message)
    await state.update_data({INTEREST_KEY: number})
    await callback.answer()
    await send(callback, texts.INTEREST_MESSAGE_PROMPT, keyboards.interest_message(number))


async def _send_interest(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    number: int | None,
    text: str | None,
) -> None:
    target: Profile | None = await profiles.get_profile_by_number(session, number) if number else None
    if target is None:
        await exit_flow(state)
        await send(event, esc(ProfileNotFound().message) if number else texts.STALE_BUTTON)
        return
    try:
        await interests.send_interest(session, user, target.user_id, text)
    except InvalidInput as exc:
        await send(
            event,
            f"⚠️ {esc(exc.message)}\nИсправьте сообщение или нажмите «Без сообщения».",
            keyboards.interest_message(target.number),
        )
        return
    except DomainError as exc:
        await exit_flow(state)
        await send(event, esc(exc.message))
        return
    await exit_flow(state)
    await send(event, texts.INTEREST_SENT.format(number=target.number))


async def on_interest_message(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    number = (await state.get_data()).get(INTEREST_KEY)
    if number and await chat.open_conversations(session, user):
        # A chat opened after the question was asked: the text may be meant for it, so it goes nowhere.
        await exit_flow(state)
        await message.answer(
            texts.INTEREST_MESSAGE_NOT_SENT.format(number=number), reply_markup=keyboards.interest_message(number)
        )
        return
    await _send_interest(message, state, session, user, number, message.text)


def _parse_age(text: str) -> dict[str, Any]:
    """«25-35» / «25» → age filter fields; raises InvalidInput with an HTML message."""
    numbers = _NUMBER.findall(text)
    if not 1 <= len(numbers) <= 2:
        raise InvalidInput(texts.FILTER_AGE_INVALID)
    try:
        filters = CatalogFilters(age_min=numbers[0], age_max=numbers[1] if len(numbers) == 2 else None)
    except ValidationError as exc:
        raise InvalidInput(esc(first_error(exc))) from exc
    return filters.model_dump(mode="json", include={"age_min", "age_max"}, exclude_none=True)


async def on_filter_age(message: Message, state: FSMContext) -> None:
    try:
        ages = _parse_age(message.text or "")
    except InvalidInput as exc:
        await message.answer(f"⚠️ {exc.message}", reply_markup=keyboards.filter_age())
        return
    await _ask_city(message, state, ages)


async def _ask_city(event: Message | CallbackQuery, state: FSMContext, ages: dict[str, Any]) -> None:
    await state.update_data({FILTER_DRAFT_KEY: ages})
    await state.set_state(CatalogForm.city)
    await send(event, texts.FILTER_CITY_PROMPT, keyboards.filter_city())


async def _apply_filter(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User, city: str | None
) -> None:
    ages = (await state.get_data()).get(FILTER_DRAFT_KEY) or {}
    try:
        filters = CatalogFilters.model_validate({**ages, "city": city})
    except ValidationError as exc:
        await send(event, f"⚠️ {esc(first_error(exc))}", keyboards.filter_city())
        return
    await exit_flow(state)
    await state.update_data({FILTERS_KEY: filters.model_dump(mode="json", exclude_none=True)})
    await show_catalog(event, state, session, user)


async def on_filter_city(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _apply_filter(message, state, session, user, message.text)


async def close_card(callback: CallbackQuery) -> None:
    """Legacy «Закрыть»: delete the card (or at least its buttons)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            await set_buttons(callback, None)


async def on_catalog_action(
    callback: CallbackQuery, callback_data: CatalogCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    action = callback_data.action
    current_state = await state.get_state()
    if action in (CatalogAction.INTEREST, CatalogAction.REPORT) and callback_data.number <= 0:
        await callback.answer(texts.STALE_BUTTON)
        return
    match action:
        case CatalogAction.INTEREST:
            await start_interest(callback, state, session, user, callback_data.number)
        case CatalogAction.NO_MESSAGE:
            # The button names the profile; buttons sent before it did rely on the dialog.
            number = callback_data.number or (await state.get_data()).get(INTEREST_KEY)
            await callback.answer()
            await set_buttons(callback, None)
            await _send_interest(callback, state, session, user, number, None)
        case CatalogAction.REPORT:
            await callback.answer()
            await report.ask_reason(callback, callback_data.number)
        case CatalogAction.FILTER:
            await callback.answer()
            await exit_flow(state)
            await state.set_state(CatalogForm.age)
            await send(callback, texts.FILTER_AGE_PROMPT, keyboards.filter_age())
        case CatalogAction.ANY_AGE if current_state == CatalogForm.age.state:
            await callback.answer()
            await set_buttons(callback, None)
            await _ask_city(callback, state, {})
        case CatalogAction.ANY_CITY if current_state == CatalogForm.city.state:
            await callback.answer()
            await set_buttons(callback, None)
            await _apply_filter(callback, state, session, user, None)
        case CatalogAction.PREV | CatalogAction.NEXT:
            await callback.answer()
            page = await _target_page(state, session, user, callback_data)
            await show_catalog(callback, state, session, user, page=page, replace=True)
        case CatalogAction.RESTART:
            await callback.answer()
            await show_catalog(callback, state, session, user, replace=True)
        case CatalogAction.RESET_FILTER:
            await callback.answer()
            await state.update_data({FILTERS_KEY: {}})
            await show_catalog(callback, state, session, user, replace=True)
        case CatalogAction.NOOP:
            await callback.answer()
        case CatalogAction.CLOSE:
            await callback.answer()
            await close_card(callback)
        case _:
            await callback.answer(texts.STALE_BUTTON)


def create_router() -> Router:
    router = Router(name="catalog")
    router.message.register(on_interest_message, CatalogForm.interest_message, F.text)
    router.message.register(on_filter_age, CatalogForm.age, F.text)
    router.message.register(on_filter_city, CatalogForm.city, F.text)
    router.callback_query.register(on_catalog_action, CatalogCb.filter())
    return router
