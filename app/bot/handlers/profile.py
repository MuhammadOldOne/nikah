"""«Моя анкета» and «Создать анкету»: the legacy registration order, editing, hide/show and
«Дополнить анкету (1 минута)» for profiles migrated without the new required fields.

Registration asks, as the legacy bot did: rules → Брат/Сестра → «Сколько вам лет?» → город →
национальность → (the new required fields as buttons) → о себе → пожелания; then the optional
extras (each skippable, or «Готово, остальное позже»), a preview, the 152-FZ consent and 18+.
Validation is the site's (app.schemas); the age becomes a birth year (``AgeIn``).
"""

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
from app.bot.callbacks import FieldAction, FieldCb, FillCb, ProfileAction, ProfileCb
from app.bot.render import by_gender, esc
from app.bot.states import ProfileForm
from app.bot.ui import edit, exit_flow, menu, message_html, send, set_buttons
from app.clock import age_from_birth_year
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
    ABOUT_MAX,
    AGE_MAX,
    AGE_MIN,
    FIELD_LABELS,
    PROFILE_OPTIONAL_FIELDS,
    PROFILE_REQUIRED_FIELDS,
    WISHES_MAX,
    WISHES_MIN,
    AgeIn,
    ProfileCreate,
    ProfileUpdate,
    first_error,
    humanize_errors,
)
from app.services import profiles, users
from app.services.errors import DomainError, InvalidInput
from app.text import gender_word

WIZARD_KEY = "wizard"
PICKER_LABEL_LIMIT = 48  # «Поле: значение» on a button
# Steps in the legacy order; "age" stands for the birth year (the bot asks the age).
CREATE_STEPS: tuple[str, ...] = (
    "gender",
    "age",
    "city",
    "nationality",
    "marital_status",
    "children",
    "education",
    "employment",
    "about",
    "wishes",
    "height",
    "weight",
    "occupation",
    "madhab",
    "prayer",
    "nikah_timeline",
    "relocation",
)
EDIT_STEPS = CREATE_STEPS[1:]  # gender cannot be changed after the profile is created
LAST_REQUIRED_STEP = "wishes"  # «Готово, остальное позже» is offered after it
STEP_FIELDS: dict[str, str] = {"age": "birth_year"}
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
QUESTIONS: dict[str, str] = {
    "gender": "Кто вы?",
    "age": f"Сколько вам лет? (от {AGE_MIN} до {AGE_MAX})",
    "city": "Из какого вы города?",
    "nationality": "Ваша национальность?",
    "marital_status": "Ваше семейное положение?",
    "children": "Сколько у вас детей? Выберите или напишите число.",
    "education": "Ваше образование?",
    "employment": "Чем вы заняты?",
    "about": f"Расскажите о себе кратко (до {ABOUT_MAX} симв.): практика, образование, работа, характер.",
    "height": "Ваш рост, см? Например, 175.",
    "weight": "Ваш вес, кг? Например, 70.",
    "occupation": "Род занятий: профессия или сфера деятельности?",
    "madhab": "Ваш мазхаб?",
    "prayer": "Намаз?",
    "nikah_timeline": "Когда вы готовы к никаху?",
    "relocation": "Готовность к переезду?",
}
TEXT_STEPS_WITH_HINT = frozenset({"about", "wishes", "occupation"})


class Mode(StrEnum):
    CREATE = "create"
    EDIT = "edit"


def _field(step: str) -> str:
    return STEP_FIELDS.get(step, step)


def _optional(step: str) -> bool:
    return _field(step) in PROFILE_OPTIONAL_FIELDS


@dataclass
class Wizard:
    """Wizard progress kept in FSM data (JSON-friendly values only, keyed by model field)."""

    mode: Mode
    step: str
    draft: dict[str, Any] = field(default_factory=dict)
    question_id: int | None = None  # message whose buttons belong to the current step
    number: int | None = None  # editing: the profile's number (for the preview)

    @property
    def steps(self) -> tuple[str, ...]:
        return CREATE_STEPS if self.mode == Mode.CREATE else EDIT_STEPS

    @property
    def gender(self) -> Gender | None:
        value = self.draft.get("gender")
        return Gender(value) if value else None


async def _load(state: FSMContext) -> Wizard | None:
    raw = (await state.get_data()).get(WIZARD_KEY)
    if not raw:
        return None
    return Wizard(
        mode=Mode(raw["mode"]),
        step=raw["step"],
        draft=raw["draft"],
        question_id=raw.get("question_id"),
        number=raw.get("number"),
    )


async def _store(state: FSMContext, wizard: Wizard) -> None:
    await state.update_data({WIZARD_KEY: asdict(wizard)})


def _title(step: str, gender: Gender | None) -> str:
    if step == "wishes":
        return by_gender(gender, "Пожелания к супруге", "Пожелания к супругу", FIELD_LABELS["wishes"])
    if step == "nikah_timeline":
        return by_gender(gender, "Когда готов к никаху", "Когда готова к никаху", FIELD_LABELS[step])
    return FIELD_LABELS[step]  # "age" has its own label («Возраст»)


def _question(step: str, gender: Gender | None) -> str:
    if step == "wishes":
        spouse = by_gender(gender, "будущей супруге", "будущему супругу", "будущему супругу/супруге")
        return f"Опишите пожелания к {spouse} (от {WISHES_MIN} до {WISHES_MAX} симв.)."
    return QUESTIONS[step]


def _choices(step: str, gender: Gender | None) -> list[tuple[str, str]]:
    if step == "gender":
        return [(value.value, gender_word(value)) for value in Gender]
    if step in CHOICE_FIELDS:
        return [(value, text[:1].upper() + text[1:]) for value, text in choices(CHOICE_FIELDS[step], gender)]
    return [(value, value) for value in SHORTCUTS.get(step, ())]


def _display(step: str, draft: dict[str, Any], gender: Gender | None) -> str:
    value = draft.get(_field(step))
    if value is None:
        return "—"
    if step == "age":
        return str(age_from_birth_year(value))
    if step == "gender":
        return gender_word(value)
    if step in CHOICE_FIELDS:
        text = label(CHOICE_FIELDS[step](value), gender)
        return text[:1].upper() + text[1:]
    return str(value)


def _validate(step: str, raw: str) -> dict[str, Any]:
    """Validate one answer with the shared rules; returns {model field: JSON value}."""
    if step == "gender":
        try:
            return {"gender": Gender(raw).value}
        except ValueError as exc:
            raise InvalidInput("Выберите вариант из списка.") from exc
    if step == "age":
        try:
            return {"birth_year": AgeIn.model_validate({"age": raw.strip()}).birth_year()}
        except ValidationError as exc:
            raise InvalidInput(humanize_errors(exc).get("age", first_error(exc))) from exc
    name = _field(step)
    try:
        data = ProfileUpdate.model_validate({name: raw})
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    return {name: data.model_dump(mode="json")[name]}


def _draft_from(profile: Profile) -> dict[str, Any]:
    draft: dict[str, Any] = {}
    for name in PROFILE_REQUIRED_FIELDS + PROFILE_OPTIONAL_FIELDS:
        value = getattr(profile, name)
        draft[name] = value.value if isinstance(value, StrEnum) else value
    return draft


def _preview(draft: dict[str, Any], number: int | None) -> Profile:
    """A transient Profile with the draft's values, for the preview card."""
    values = {
        name: CHOICE_FIELDS[name](value) if name in CHOICE_FIELDS and value is not None else value
        for name, value in draft.items()
    }
    return Profile(number=number, **values)


# --- «Моя анкета» --------------------------------------------------------------------------------


def _my_profile_buttons(profile: Profile) -> InlineKeyboardMarkup:
    return keyboards.my_profile(
        can_hide=profile.status in (ProfileStatus.ACTIVE, ProfileStatus.PENDING),
        can_show=profile.status == ProfileStatus.HIDDEN,
        incomplete=bool(_fillable(profile)),
    )


async def show_my_profile(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    profile = await profiles.get_profile(session, user.id)
    if profile is None:
        await send(event, texts.NO_PROFILE, keyboards.create_profile())
        return
    await send(event, render.my_profile(profile, profiles.missing_fields(profile)), _my_profile_buttons(profile))


# --- «Дополнить анкету (1 минута)» ------------------------------------------------------------------


def _fillable(profile: Profile) -> list[str]:
    """Missing required fields that one button press fills (the four fields of migrated profiles)."""
    return [name for name in profiles.missing_fields(profile) if name in CHOICE_FIELDS or name in SHORTCUTS]


async def offer_completion(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    """Invite the owner of an incomplete profile to complete it (on /start and after consent)."""
    profile = await profiles.get_profile(session, user.id)
    if profile is None or users.needs_consent(user):
        return
    missing = profiles.missing_fields(profile)
    if missing:
        await send(event, render.missing_note(missing), keyboards.complete_profile())


async def ask_missing(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    """The next missing field as buttons, or the thanks when none is left."""
    profile = await profiles.get_profile(session, user.id)
    if profile is None:
        await send(event, texts.NO_PROFILE, keyboards.create_profile())
        return
    fillable = _fillable(profile)
    if not fillable:
        missing = profiles.missing_fields(profile)
        if missing:  # a text field (never empty in migrated profiles): the edit wizard handles it
            await send(event, render.missing_note(missing), _my_profile_buttons(profile))
        else:
            await send(event, texts.COMPLETED, menu(user))
        return
    name = fillable[0]
    left = f"Осталось вопросов: {len(fillable)}"
    await send(
        event,
        f"<i>{left}</i>\n<b>{_question(name, profile.gender)}</b>",
        keyboards.fill_field(name, _choices(name, profile.gender), columns=len(SHORTCUTS.get(name, ())) or 2),
    )


async def on_fill(callback: CallbackQuery, callback_data: FillCb, session: AsyncSession, user: User) -> None:
    """One press saves one missing field: a choice/number edit, so no re-moderation."""
    profile = await profiles.get_profile(session, user.id)
    name = callback_data.field
    if profile is None or name not in _fillable(profile):
        await callback.answer("Уже заполнено.")
        await set_buttons(callback, None)
        await ask_missing(callback, session, user)
        return
    try:
        await profiles.update_profile(session, user, ProfileUpdate.model_validate({name: callback_data.value}))
    except ValidationError:
        await callback.answer("Выберите вариант из списка.", show_alert=True)
        return
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer()
    shown = _display(name, {name: callback_data.value}, profile.gender)
    await edit(callback, f"{message_html(callback)}\n\n✅ {esc(shown)}")
    await ask_missing(callback, session, user)


# --- Registration and editing --------------------------------------------------------------------


async def start_registration(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    """«Создать анкету»: the legacy rules with «Согласен(на) с правилами» first."""
    await exit_flow(state)
    if user.is_banned:
        await send(event, texts.BANNED)
        return
    if await profiles.get_profile(session, user.id) is not None:
        await send(event, texts.PROFILE_EXISTS, menu(user))
        return
    await state.set_state(ProfileForm.rules)
    await send(event, texts.RULES, keyboards.rules_agree())


async def start_editing(event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    await exit_flow(state)
    profile = await profiles.get_profile(session, user.id)
    if profile is None:
        await send(event, texts.NO_PROFILE, keyboards.create_profile())
        return
    wizard = Wizard(mode=Mode.EDIT, step=EDIT_STEPS[0], draft=_draft_from(profile), number=profile.number)
    await _pick_field(event, state, wizard, texts.WIZARD_EDIT_INTRO)


async def _pick_field(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard, text: str) -> None:
    """Editing: one button per field with its current value; the chosen one is asked next."""
    gender = wizard.gender
    fields = []
    for step in wizard.steps:
        caption = f"{_title(step, gender)}: {_display(step, wizard.draft, gender)}"
        fields.append(
            (step, caption if len(caption) <= PICKER_LABEL_LIMIT else caption[: PICKER_LABEL_LIMIT - 1] + "…")
        )
    wizard.question_id = None
    await _store(state, wizard)
    await state.set_state(ProfileForm.picking)
    await send(event, text, keyboards.field_picker(fields))


def _position(wizard: Wizard) -> str:
    if wizard.mode == Mode.EDIT:
        return "Изменение анкеты"
    index = wizard.steps.index(wizard.step)
    required_count = wizard.steps.index(LAST_REQUIRED_STEP) + 1
    if index < required_count:
        position = f"Шаг {index + 1} из {required_count}"
        return f"{position} · можно пропустить" if _optional(wizard.step) else position
    return "Дополнительно · необязательно"


async def _ask(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    step, gender = wizard.step, wizard.gender
    editing = wizard.mode == Mode.EDIT
    text = f"<i>{_position(wizard)}</i>\n<b>{_question(step, gender)}</b>"
    if step in TEXT_STEPS_WITH_HINT:
        text += f"\n{texts.WIZARD_TEXT_HINT}"
    has_value = wizard.draft.get(_field(step)) is not None
    if editing and has_value:
        text += f"\n\nСейчас: <i>{esc(_display(step, wizard.draft, gender))}</i>"
    can_finish = not editing and wizard.steps.index(step) > wizard.steps.index(LAST_REQUIRED_STEP)
    markup = keyboards.field_step(
        step,
        _choices(step, gender),
        optional=_optional(step),
        editing=editing,
        has_value=has_value,
        can_finish=can_finish,
        columns=len(SHORTCUTS[step]) if step in SHORTCUTS else 2,
    )
    question = await send(event, text, markup)
    wizard.question_id = question.message_id
    await _store(state, wizard)


async def _advance(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    """Creating: the next step (the preview after the last one). Editing: back to the preview."""
    index = wizard.steps.index(wizard.step) + 1
    if wizard.mode == Mode.CREATE and index < len(wizard.steps):
        wizard.step = wizard.steps[index]
        await _ask(event, state, wizard)
        return
    await _summary(event, state, wizard)


async def _summary(event: Message | CallbackQuery, state: FSMContext, wizard: Wizard) -> None:
    if wizard.mode == Mode.CREATE:
        data = await _validated_or_reask(event, state, wizard)
        if data is None:
            return
        card = render.profile_card(Profile(**data.model_dump()))
        intro = (
            "Проверьте анкету. После отправки её увидит модератор."
            if get_settings().PREMODERATION
            else "Проверьте анкету."
        )
    else:
        card = render.profile_card(_preview(wizard.draft, wizard.number))
        intro = "Проверьте изменения."
    wizard.question_id = None
    await _store(state, wizard)
    await state.set_state(ProfileForm.confirm)
    markup = keyboards.wizard_summary(editing=wizard.mode == Mode.EDIT, premoderation=get_settings().PREMODERATION)
    await send(event, f"{intro}\n\n{card}", markup)


def _step_of(field_name: str) -> str:
    return next((step for step, name in STEP_FIELDS.items() if name == field_name), field_name)


async def _validated_or_reask(
    event: Message | CallbackQuery, state: FSMContext, wizard: Wizard
) -> ProfileCreate | None:
    """Validate the whole new profile; on an error return to the first invalid step."""
    try:
        return ProfileCreate.model_validate(wizard.draft)
    except ValidationError as exc:
        invalid = _step_of(next(iter(humanize_errors(exc))))
        await send(event, f"⚠️ {esc(first_error(exc))}")
        wizard.step = invalid if invalid in wizard.steps else wizard.steps[0]
        await state.set_state(ProfileForm.filling)
        await _ask(event, state, wizard)
        return None


async def on_text_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    wizard = await _load(state)
    if wizard is None:
        await exit_flow(state)
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    if wizard.step in CHOICE_FIELDS:
        await message.answer(texts.WIZARD_PICK_BUTTON)
        return
    try:
        values = _validate(wizard.step, message.text or "")
    except InvalidInput as exc:
        await message.answer(f"⚠️ {esc(exc.message)}\nПопробуйте ещё раз.")
        return
    if wizard.question_id is not None:
        with contextlib.suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=wizard.question_id)
    wizard.draft.update(values)
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
    step = wizard.step
    match callback_data.action:
        case FieldAction.SET if step in CHOICE_FIELDS or step in SHORTCUTS:
            try:
                wizard.draft.update(_validate(step, callback_data.value))
            except InvalidInput as exc:
                await callback.answer(exc.message, show_alert=True)
                return
        case FieldAction.KEEP if wizard.mode == Mode.EDIT:
            pass
        case FieldAction.CLEAR if _optional(step):
            wizard.draft[_field(step)] = None
        case FieldAction.FINISH if wizard.mode == Mode.CREATE and _optional(step):
            await callback.answer()
            await edit(callback, f"{message_html(callback)}\n\n✅ Остальное — позже")
            await _summary(callback, state, wizard)
            return
        case _:
            await callback.answer(texts.STALE_BUTTON)
            return
    await callback.answer()
    await edit(callback, f"{message_html(callback)}\n\n✅ {esc(_display(step, wizard.draft, wizard.gender))}")
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


def consent_text() -> str:
    url = f"{get_settings().SITE_URL}/consent"
    return (
        "<b>Согласие на обработку персональных данных</b>\n\n"
        "Анкета содержит сведения о ваших религиозных убеждениях — это особая категория персональных "
        "данных (152-ФЗ). Данные хранятся на сервере в России и используются только для работы "
        "платформы: показа анкеты участникам, откликов и переписки.\n\n"
        f'Прочитайте <a href="{esc(url)}">текст согласия</a> и нажмите «{texts.CONSENT_BUTTON}». '
        "Отозвать согласие и удалить все данные можно в любой момент командой /delete."
    )


def adult_text(gender: Gender | None) -> str:
    status = by_gender(gender, "не женаты", "не замужем", "не состоите в браке")
    spouse = by_gender(gender, "супругу", "супруга", "супруга(у)")
    return f"<b>Последний шаг</b>\nПодтвердите, что вам исполнилось 18 лет, вы {status} и ищете {spouse} для никаха."


async def _rules_ok(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != ProfileForm.rules.state:
        await callback.answer(texts.STALE_BUTTON)
        return
    await callback.answer()
    await set_buttons(callback, None)
    wizard = Wizard(mode=Mode.CREATE, step=CREATE_STEPS[0])
    await state.set_state(ProfileForm.filling)
    await _ask(callback, state, wizard)


async def _save(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.confirm)
    if wizard is None:
        return
    if wizard.mode == Mode.EDIT:
        await _save_changes(callback, state, session, user, wizard)
    elif users.needs_consent(user):
        await state.set_state(ProfileForm.consent)
        await send(callback, consent_text(), keyboards.consent())
    else:
        await state.set_state(ProfileForm.adult)
        await send(callback, adult_text(wizard.gender), keyboards.adult())


async def _restart(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    wizard = await _wizard_at(callback, state, ProfileForm.confirm)
    if wizard is None:
        return
    if wizard.mode == Mode.CREATE:
        await exit_flow(state)
        await state.set_state(ProfileForm.filling)
        await _ask(callback, state, Wizard(mode=Mode.CREATE, step=CREATE_STEPS[0]))
    else:
        await start_editing(callback, state, session, user)


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
    await send(callback, adult_text(wizard.gender), keyboards.adult())


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
        await send(callback, esc(exc.message), menu(user))
        return
    await exit_flow(state)
    if profile.status == ProfileStatus.PENDING:
        text = f"Ваша анкета №{profile.number} отправлена на модерацию. Мы уведомим вас, когда её одобрят."
    else:
        text = f"Ваша анкета №{profile.number} опубликована. Теперь вы можете смотреть анкеты и отправлять отклики."
    await send(callback, text, menu(user))


async def _save_changes(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, wizard: Wizard
) -> None:
    profile = await profiles.get_profile(session, user.id)
    if profile is None:
        await exit_flow(state)
        await send(callback, texts.NO_PROFILE, keyboards.create_profile())
        return
    submitted_before = profile.submitted_at
    try:
        # Only changed fields are validated: legacy texts that predate today's rules may stay.
        data = ProfileUpdate.from_form({k: v for k, v in wizard.draft.items() if k != "gender"}, profile)
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
    resubmitted = profile.submitted_at != submitted_before
    if resubmitted and profile.status == ProfileStatus.PENDING:
        text = (
            "Изменения сохранены и отправлены на модерацию. "
            "Пока анкета на проверке, смотреть анкеты и отправлять отклики нельзя."
        )
    elif profile.status == ProfileStatus.HIDDEN:
        text = "Изменения сохранены. Анкета скрыта — покажите её, когда будете готовы."
    else:
        text = "Изменения сохранены."
    await send(callback, text, menu(user))


async def on_profile_action(
    callback: CallbackQuery, callback_data: ProfileCb, state: FSMContext, session: AsyncSession, user: User
) -> None:
    match callback_data.action:
        case ProfileAction.CREATE:
            await callback.answer()
            await set_buttons(callback, None)
            await start_registration(callback, state, session, user)
        case ProfileAction.EDIT:
            await callback.answer()
            await start_editing(callback, state, session, user)
        case ProfileAction.RULES_OK:
            await _rules_ok(callback, state)
        case ProfileAction.COMPLETE:
            await callback.answer()
            await exit_flow(state)
            await ask_missing(callback, session, user)
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
            await edit(
                callback, render.my_profile(profile, profiles.missing_fields(profile)), _my_profile_buttons(profile)
            )
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
    router.callback_query.register(on_fill, FillCb.filter())
    router.callback_query.register(on_profile_action, ProfileCb.filter())
    return router
