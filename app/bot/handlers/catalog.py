"""«🔍 Смотреть анкеты»: one card at a time, age/city filter, interest with an optional message."""

from __future__ import annotations

import re
from typing import Any

from aiogram import F, Router
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
from app.enums import ProfileStatus
from app.models import User
from app.schemas import CatalogFilters, first_error
from app.services import guardians, interests, profiles
from app.services.errors import DomainError, InvalidInput, ProfileNotActive, ProfileRequired

PAGE_KEY = "catalog_page"
SHOWN_KEY = "catalog_shown"  # user id of the card shown last: «Дальше» continues after it
FILTERS_KEY = "catalog_filters"
FILTER_DRAFT_KEY = "filter_draft"
INTEREST_KEY = "interest_to"
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
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User, *, replace: bool = False
) -> None:
    data = await state.get_data()
    page = int(data.get(PAGE_KEY) or 1)
    filters = _filters(data)
    try:
        items, total = await profiles.list_catalog(session, user, filters, page=page, per_page=1)
    except ProfileRequired:
        await send(event, texts.PROFILE_REQUIRED, keyboards.create_profile())
        return
    except DomainError as exc:
        await send(event, esc(exc.message))
        return
    note = _filter_note(filters)
    if items:
        profile = items[0]
        has_guardian = await guardians.has_active_guardian(session, profile.user_id)
        footer = f"Анкета {page} из {total}" + (f" · фильтр: {note}" if note else "")
        text = f"{render.profile_card(profile, has_guardian=has_guardian)}\n\n<i>{footer}</i>"
        if user.profile is not None and user.profile.status != ProfileStatus.ACTIVE:
            text += f"\n\nℹ️ {ProfileNotActive().message}"
        markup = keyboards.catalog_card(profile.user_id)
        await state.update_data({SHOWN_KEY: profile.user_id})
    else:
        if total:
            text = "Вы посмотрели все анкеты 🌙 Новые появляются регулярно — загляните позже или начните сначала."
        else:
            text = f"По фильтру «{note}» анкет пока нет." if note else "Анкет пока нет — загляните позже."
        markup = keyboards.catalog_end(has_filter=bool(note))
    if replace and isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await send(event, text, markup)


async def _next_page(state: FSMContext, session: AsyncSession, user: User) -> int:
    """The page after the card shown last, wherever it is now. Profiles may have left the list
    meanwhile (one the user just reported, a hidden one): counting pages would skip a card."""
    data = await state.get_data()
    page = int(data.get(PAGE_KEY) or 1)
    shown = data.get(SHOWN_KEY)
    if shown is None:
        return page + 1
    try:
        position = await profiles.catalog_position(session, user, _filters(data), shown)
    except DomainError:
        return page + 1
    # Not in the list any more: the next card has moved up to the shown card's page.
    return position + 1 if position is not None else page


async def _ask_interest_message(callback: CallbackQuery, state: FSMContext, user: User, target_id: int) -> None:
    # Fail fast before asking for a message; send_interest re-checks every rule.
    if user.profile is None:
        await callback.answer(texts.PROFILE_REQUIRED, show_alert=True)
        return
    if user.profile.status != ProfileStatus.ACTIVE:
        await callback.answer(ProfileNotActive().message, show_alert=True)
        return
    await exit_flow(state)
    await state.set_state(CatalogForm.interest_message)
    await state.update_data({INTEREST_KEY: target_id})
    await callback.answer()
    await send(callback, texts.INTEREST_MESSAGE_PROMPT, keyboards.interest_message())


async def _send_interest(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User, text: str | None
) -> None:
    target_id = (await state.get_data()).get(INTEREST_KEY)
    if target_id is None:
        await exit_flow(state)
        await send(event, texts.STALE_BUTTON)
        return
    try:
        await interests.send_interest(session, user, target_id, text)
    except InvalidInput as exc:
        await send(
            event,
            f"⚠️ {esc(exc.message)}\nИсправьте сообщение или нажмите «Без сообщения».",
            keyboards.interest_message(),
        )
        return
    except DomainError as exc:
        await exit_flow(state)
        await send(event, esc(exc.message))
        return
    await exit_flow(state)
    target = await profiles.get_profile(session, target_id)
    name = render.profile_ref(target) if target else "собеседник"
    await send(event, f"💌 Отклик отправлен. Если {name} примет его, откроется чат — уведомление придёт сюда.")


async def on_interest_message(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _send_interest(message, state, session, user, message.text)


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
    await state.update_data({FILTERS_KEY: filters.model_dump(mode="json", exclude_none=True), PAGE_KEY: 1})
    await show_catalog(event, state, session, user)


async def on_filter_city(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await _apply_filter(message, state, session, user, message.text)


async def on_catalog_action(
    callback: CallbackQuery, callback_data: CatalogCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    action = callback_data.action
    current_state = await state.get_state()
    if action in (CatalogAction.INTEREST, CatalogAction.REPORT) and callback_data.user_id <= 0:
        await callback.answer(texts.STALE_BUTTON)
        return
    match action:
        case CatalogAction.INTEREST:
            await _ask_interest_message(callback, state, user, callback_data.user_id)
            return
        case CatalogAction.NO_MESSAGE if current_state == CatalogForm.interest_message.state:
            await callback.answer()
            await set_buttons(callback, None)
            await _send_interest(callback, state, session, user, None)
            return
        case CatalogAction.REPORT:
            await callback.answer()
            await report.ask_reason(callback, callback_data.user_id)
            return
        case CatalogAction.FILTER:
            await callback.answer()
            await exit_flow(state)
            await state.set_state(CatalogForm.age)
            await send(callback, texts.FILTER_AGE_PROMPT, keyboards.filter_age())
            return
        case CatalogAction.ANY_AGE if current_state == CatalogForm.age.state:
            await callback.answer()
            await set_buttons(callback, None)
            await _ask_city(callback, state, {})
            return
        case CatalogAction.ANY_CITY if current_state == CatalogForm.city.state:
            await callback.answer()
            await set_buttons(callback, None)
            await _apply_filter(callback, state, session, user, None)
            return
        case CatalogAction.NEXT:
            await state.update_data({PAGE_KEY: await _next_page(state, session, user)})
        case CatalogAction.RESTART:
            await state.update_data({PAGE_KEY: 1})
        case CatalogAction.RESET_FILTER:
            await state.update_data({PAGE_KEY: 1, FILTERS_KEY: {}})
        case _:
            await callback.answer(texts.STALE_BUTTON)
            return
    await callback.answer()
    await show_catalog(callback, state, session, user, replace=True)


def create_router() -> Router:
    router = Router(name="catalog")
    router.message.register(on_interest_message, CatalogForm.interest_message, F.text)
    router.message.register(on_filter_age, CatalogForm.age, F.text)
    router.message.register(on_filter_city, CatalogForm.city, F.text)
    router.callback_query.register(on_catalog_action, CatalogCb.filter())
    return router
