"""«📝 Анкета»: view, create/edit wizard (same fields and validation as the site), hide/show."""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, render, texts
from app.bot.callbacks import FieldAction, FieldCb, ProfileAction, ProfileCb
from app.bot.render import by_gender, esc
from app.bot.states import ProfileForm
from app.bot.ui import edit, exit_flow, message_html, send, set_buttons
from app.config import get_settings
from app.enums import (
    Education,
    Employment,
    Gender,
    Madhab,
    MaritalStatus,
    NikahTimeline,
    Prayer,
    ProfileStatus,
    Relocation,
    choices,
    label,
)
from app.models import Profile, User
from app.schemas import (
    FIELD_LABELS,
    PROFILE_OPTIONAL_FIELDS,
    PROFILE_REQUIRED_FIELDS,
    ProfileCreate,
    ProfileUpdate,
    first_error,
    humanize_errors,
)
from app.services import profiles, users
from app.services.errors import DomainError, InvalidInput

WIZARD_KEY = "wizard"
PICKER_LABEL_LIMIT = 48  # «Поле: значение» on a button
STEPS: tuple[str, ...] = PROFILE_REQUIRED_FIELDS + PROFILE_OPTIONAL_FIELDS
CHOICE_FIELDS: dict[str, type[StrEnum]] = {
    "gender": Gender,
    "marital_status": MaritalStatus,
    "education": Education,
    "employment": Employment,
    "madhab": Madhab,
    "prayer": Prayer,
    "nikah_timeline": NikahTimeline,
    "relocation": Relocation,
}
SHORTCUTS: dict[str, tuple[str, ...]] = {"children": ("0", "1", "2", "3", "4", "5")}
HINTS: dict[str, str] = {
    "name": "Только буквы, от 2 до 40. Можно псевдоним.",
    "birth_year": "Например, <code>1996</code>.",
    "city": "Город, в котором вы живёте.",
    "children": "Выберите или напишите число.",
    "wishes": "Какой вы видите семью, что для вас важно — от 10 до 1000 символов.",
    "height": "Число, например <code>175</code>.",
    "weight": "Число, например <code>70</code>.",
    "occupation": "Профессия или сфера деятельности.",
    "about": "Характер, увлечения, образ жизни — до 2000 символов.",
}


class Mode(StrEnum):
    CREATE = "create"
    EDIT = "edit"


def _steps(mode: Mode) -> tuple[str, ...]:
    # Gender cannot be changed after the profile is created.
    return STEPS if mode == Mode.CREATE else STEPS[1:]


@dataclass
class Wizard:
    """Wizard progress kept in FSM data (JSON-friendly values only)."""

    mode: Mode
    step: str
    draft: dict[str, Any] = field(default_factory=dict)
    question_id: int | None = None  # message whose buttons belong to the current step

    @property
    def steps(self) -> tuple[str, ...]:
        return _steps(self.mode)

    @property
    def gender(self) -> Gender | None:
        value = self.draft.get("gender")
        return Gender(value) if value else None


async def _load(state: FSMContext) -> Wizard | None:
    raw = (await state.get_data()).get(WIZARD_KEY)
    if not raw:
        return None
    return Wizard(mode=Mode(raw["mode"]), step=raw["step"], draft=raw["draft"], question_id=raw.get("question_id"))


async def _store(state: FSMContext, wizard: Wizard) -> None:
    await state.update_data({WIZARD_KEY: asdict(wizard)})


def _title(name: str, gender: Gender | None) -> str:
    if name == "wishes":
        return by_gender(gender, "Пожелания к супруге", "Пожелания к супругу", FIELD_LABELS[name])
    if name == "nikah_timeline":
        return by_gender(gender, "Когда готов к никаху", "Когда готова к никаху", FIELD_LABELS[name])
    return FIELD_LABELS[name]


def _choices(name: str, gender: Gender | None) -> list[tuple[str, str]]:
    if name in CHOICE_FIELDS:
        return [(value, text[:1].upper() + text[1:]) for value, text in choices(CHOICE_FIELDS[name], gender)]
    return [(value, value) for value in SHORTCUTS.get(name, ())]


def _display(name: str, value: Any, gender: Gender | None) -> str:
    if value is None:
        return "—"
    if name in CHOICE_FIELDS:
        text = label(CHOICE_FIELDS[name](value), gender)
        return text[:1].upper() + text[1:]
    return str(value)


def _validate(name: str, raw: str) -> Any:
    """Validate one answer with the shared schema rules; returns the normalized JSON value."""
    if name == "gender":
        try:
            return Gender(raw).value
        except ValueError as exc:
            raise InvalidInput("Выберите вариант из списка.") from exc
    try:
        data = ProfileUpdate.model_validate({name: raw})
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    return data.model_dump(mode="json")[name]


def _draft_from(profile: Profile) -> dict[str, Any]:
    draft: dict[str, Any] = {}
    for name in STEPS:
        value = getattr(profile, name)
        draft[name] = value.value if isinstance(value, StrEnum) else value
    return draft


def _my_profile_buttons(profile: Profile) -> InlineKeyboardMarkup:
    return keyboards.my_profile(
        can_hide=profile.status in (ProfileStatus.ACTIVE, ProfileStatus.PENDING),
        can_show=profile.status == ProfileStatus.HIDDEN,
    )


async def show_my_profile(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    profile = await profiles.get_profile(session, user.id)
    if profile is None:
        await send(event, texts.NO_PROFILE, keyboards.create_profile())
        return
    await send(event, render.my_profile(profile), _my_profile_buttons(profile))


async def start_wizard(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User, mode: Mode
) -> None:
    """Creating walks through every step; editing asks which field to change."""
    await exit_flow(state)
    profile = await profiles.get_profile(session, user.id)
    if mode == Mode.CREATE and profile is not None:
        await show_my_profile(event, session, user)
        return
    if profile is None:
        mode = Mode.CREATE
    if mode == Mode.EDIT and profile is not None:
        wizard = Wizard(mode=mode, step=_steps(mode)[0], draft=_draft_from(profile))
        await _pick_field(event, state, wizard, texts.WIZARD_EDIT_INTRO)
        return
    wizard = Wizard(mode=mode, step=_steps(mode)[0])
    await state.set_state(ProfileForm.filling)
    await send(event, texts.WIZARD_INTRO)
    await _ask(event, state, wizard)


async def _pick_field(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard, text: str) -> None:
    """Editing: one button per field with its current value; the chosen one is asked next."""
    gender = wizard.gender
    fields = []
    for name in wizard.steps:
        label = f"{_title(name, gender)}: {_display(name, wizard.draft.get(name), gender)}"
        fields.append((name, label if len(label) <= PICKER_LABEL_LIMIT else label[: PICKER_LABEL_LIMIT - 1] + "…"))
    wizard.question_id = None
    await _store(state, wizard)
    await state.set_state(ProfileForm.picking)
    await send(event, text, keyboards.field_picker(fields))


async def _ask(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    name, gender = wizard.step, wizard.gender
    optional = name in PROFILE_OPTIONAL_FIELDS
    if wizard.mode == Mode.CREATE:
        position = f"Шаг {wizard.steps.index(name) + 1} из {len(wizard.steps)}"
    else:
        position = "Изменение анкеты"
    position += " · необязательно" if optional else ""
    text = f"<i>{position}</i>\n<b>{_title(name, gender)}</b>"
    if hint := HINTS.get(name):
        text += f"\n{hint}"
    current = wizard.draft.get(name)
    editing = wizard.mode == Mode.EDIT
    if editing and current is not None:
        text += f"\n\nСейчас: <i>{esc(_display(name, current, gender))}</i>"
    markup = keyboards.field_step(
        name,
        _choices(name, gender),
        optional=optional,
        editing=editing,
        has_value=current is not None,
        columns=len(SHORTCUTS[name]) if name in SHORTCUTS else 2,
    )
    question = await send(event, text, markup)
    wizard.question_id = question.message_id
    await _store(state, wizard)


async def _advance(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    """Creating: the next step (the summary after the last one). Editing: back to the summary."""
    index = wizard.steps.index(wizard.step) + 1
    if wizard.mode == Mode.CREATE and index < len(wizard.steps):
        wizard.step = wizard.steps[index]
        await _ask(event, state, wizard)
        return
    await _summary(event, state, wizard)


async def _summary(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    data = await _validated_or_reask(event, state, wizard)
    if data is None:
        return
    wizard.question_id = None
    await _store(state, wizard)
    await state.set_state(ProfileForm.confirm)
    card = render.profile_card(Profile(**data.model_dump()))
    markup = keyboards.wizard_summary(editing=wizard.mode == Mode.EDIT)
    await send(event, f"<b>Проверьте анкету</b>\n\n{card}", markup)


async def _validated_or_reask(
    event: Message | CallbackQuery, state: FSMContext, wizard: Wizard
) -> ProfileCreate | None:
    """Validate the whole draft; on an error return to the first invalid step."""
    try:
        return ProfileCreate.model_validate(wizard.draft)
    except ValidationError as exc:
        invalid = next(iter(humanize_errors(exc)))
        await send(event, f"⚠️ {esc(first_error(exc))}")
        wizard.step = invalid if invalid in wizard.steps else wizard.steps[0]
        await state.set_state(ProfileForm.filling)
        await _ask(event, state, wizard)
        return None


async def on_text_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    wizard = await _load(state)
    if wizard is None:
        await exit_flow(state)
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=keyboards.main_menu())
        return
    if wizard.step in CHOICE_FIELDS:
        await message.answer(texts.WIZARD_PICK_BUTTON)
        return
    try:
        value = _validate(wizard.step, message.text or "")
    except InvalidInput as exc:
        await message.answer(f"⚠️ {esc(exc.message)}\nПопробуйте ещё раз.")
        return
    if wizard.question_id is not None:
        with contextlib.suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=wizard.question_id)
    wizard.draft[wizard.step] = value
    await _advance(message, state, wizard)


async def on_field_button(callback: CallbackQuery, callback_data: FieldCb, state: FSMContext) -> None:
    wizard = await _load(state)
    current = await state.get_state()
    if wizard is not None and callback_data.action == FieldAction.PICK:
        if current != ProfileForm.picking.state or callback_data.field not in wizard.steps:
            await callback.answer(texts.STALE_BUTTON)
            return
        await callback.answer()
        await set_buttons(callback, None)
        wizard.step = callback_data.field
        await state.set_state(ProfileForm.filling)
        await _ask(callback, state, wizard)
        return
    if wizard is None or current != ProfileForm.filling.state or callback_data.field != wizard.step:
        await callback.answer(texts.STALE_BUTTON)
        return
    name = wizard.step
    match callback_data.action:
        case FieldAction.SET if name in CHOICE_FIELDS or name in SHORTCUTS:
            try:
                wizard.draft[name] = _validate(name, callback_data.value)
            except InvalidInput as exc:
                await callback.answer(exc.message, show_alert=True)
                return
        case FieldAction.KEEP if wizard.mode == Mode.EDIT:
            pass
        case FieldAction.CLEAR if name in PROFILE_OPTIONAL_FIELDS:
            wizard.draft[name] = None
        case FieldAction.FINISH if wizard.mode == Mode.CREATE and name in PROFILE_OPTIONAL_FIELDS:
            await callback.answer()
            await edit(callback, f"{message_html(callback)}\n\n✅ Остальное — позже")
            await _summary(callback, state, wizard)
            return
        case _:
            await callback.answer(texts.STALE_BUTTON)
            return
    await callback.answer()
    await edit(callback, f"{message_html(callback)}\n\n✅ {esc(_display(name, wizard.draft.get(name), wizard.gender))}")
    await _advance(callback, state, wizard)


async def _wizard_at(callback: CallbackQuery, state: FSMContext, expected: State) -> Wizard | None:
    """The wizard if the dialog is at ``expected`` state, else answers that the button is stale."""
    wizard = await _load(state)
    if wizard is None or await state.get_state() != expected.state:
        await callback.answer(texts.STALE_BUTTON)
        return None
    await callback.answer()
    await set_buttons(callback, None)
    return wizard


def _consent_text() -> str:
    url = f"{get_settings().SITE_URL}/consent"
    return (
        "<b>Согласие на обработку персональных данных</b>\n\n"
        "Анкета содержит сведения о ваших религиозных убеждениях — это особая категория персональных "
        "данных (152-ФЗ). Данные хранятся на сервере в России и используются только для работы "
        "платформы: показа анкеты участникам, откликов и переписки.\n\n"
        f'Прочитайте <a href="{esc(url)}">текст согласия</a>. Отозвать согласие и удалить все данные '
        "можно в любой момент командой /delete."
    )


def _adult_text(gender: Gender | None) -> str:
    status = by_gender(gender, "не женаты", "не замужем", "не состоите в браке")
    spouse = by_gender(gender, "супругу", "супруга", "супруга(у)")
    return (
        f"<b>Последний шаг</b>\nПодтвердите, что вам исполнилось 18 лет, вы {status} "
        f"и ищете {spouse} для никаха. «Истина» — только для никаха."
    )


async def _save(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.confirm)
    if wizard is None:
        return
    if wizard.mode == Mode.EDIT:
        await _save_changes(callback, state, session, user, wizard)
    elif user.consent_at is None:
        await state.set_state(ProfileForm.consent)
        await send(callback, _consent_text(), keyboards.consent())
    else:
        await state.set_state(ProfileForm.adult)
        await send(callback, _adult_text(wizard.gender), keyboards.adult())


async def _restart(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.confirm)
    if wizard is not None:
        await start_wizard(callback, state, session, user, wizard.mode)


async def _edit_more(callback: CallbackQuery, state: FSMContext) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.confirm)
    if wizard is not None and wizard.mode == Mode.EDIT:
        await _pick_field(callback, state, wizard, texts.WIZARD_PICK_FIELD)


async def _consent(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.consent)
    if wizard is None:
        return
    await users.set_consent(session, user)
    await state.set_state(ProfileForm.adult)
    await send(callback, _adult_text(wizard.gender), keyboards.adult())


async def _adult(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.adult)
    if wizard is None:
        return
    data = await _validated_or_reask(callback, state, wizard)
    if data is None:
        return
    try:
        profile = await profiles.create_profile(session, user, data)
    except DomainError as exc:
        await exit_flow(state)
        await send(callback, esc(exc.message), keyboards.main_menu())
        return
    await exit_flow(state)
    if profile.status == ProfileStatus.PENDING:
        text = (
            "✅ Анкета сохранена и отправлена на модерацию. Когда её проверят, придёт уведомление.\n"
            "А пока можно смотреть анкеты: «🔍 Смотреть анкеты»."
        )
    else:
        text = "✅ Анкета опубликована! Смотрите анкеты и отправляйте отклики: «🔍 Смотреть анкеты»."
    await send(callback, text, keyboards.main_menu())


async def _save_changes(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, wizard: Wizard
) -> None:
    try:
        data = ProfileUpdate.model_validate({name: value for name, value in wizard.draft.items() if name != "gender"})
        profile = await profiles.update_profile(session, user, data)
    except ValidationError as exc:
        await exit_flow(state)
        await send(callback, f"⚠️ {esc(first_error(exc))}")
        return
    except DomainError as exc:
        await exit_flow(state)
        await send(callback, esc(exc.message))
        return
    await exit_flow(state)
    match profile.status:
        case ProfileStatus.PENDING:
            text = "✅ Изменения сохранены и отправлены на модерацию. Пока анкета на проверке, её не видно в каталоге."
        case ProfileStatus.HIDDEN:
            text = "✅ Изменения сохранены. Анкета скрыта — покажите её, когда будете готовы."
        case _:
            text = "✅ Изменения сохранены."
    await send(callback, text, keyboards.main_menu())


async def on_profile_action(
    callback: CallbackQuery, callback_data: ProfileCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    match callback_data.action:
        case ProfileAction.CREATE | ProfileAction.EDIT:
            await callback.answer()
            await set_buttons(callback, None)
            mode = Mode.CREATE if callback_data.action == ProfileAction.CREATE else Mode.EDIT
            await start_wizard(callback, state, session, user, mode)
        case ProfileAction.HIDE | ProfileAction.SHOW:
            try:
                if callback_data.action == ProfileAction.HIDE:
                    profile = await profiles.hide(session, user)
                else:
                    profile = await profiles.show(session, user)
            except DomainError as exc:
                await callback.answer(exc.message, show_alert=True)
                return
            await callback.answer()
            await edit(callback, render.my_profile(profile), _my_profile_buttons(profile))
        case ProfileAction.SAVE:
            await _save(callback, state, session, user)
        case ProfileAction.RESTART:
            await _restart(callback, state, session, user)
        case ProfileAction.MORE:
            await _edit_more(callback, state)
        case ProfileAction.CONSENT:
            await _consent(callback, state, session, user)
        case ProfileAction.ADULT:
            await _adult(callback, state, session, user)


def create_router() -> Router:
    router = Router(name="profile")
    router.message.register(on_text_answer, ProfileForm.filling, F.text)
    router.callback_query.register(on_field_button, FieldCb.filter())
    router.callback_query.register(on_profile_action, ProfileCb.filter())
    return router
