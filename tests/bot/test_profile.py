"""«Создать анкету» in the legacy order (rules → Брат/Сестра → age → …), consent, 18+, editing,
hide/show and «Дополнить анкету (1 минута)» for migrated profiles."""

from __future__ import annotations

from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import CANCEL, FieldAction, FieldCb, FillCb, ProfileAction, ProfileCb
from app.clock import current_year
from app.enums import Education, Employment, Gender, MaritalStatus, ProfileStatus
from app.models import Outbox, Profile
from app.services import profiles
from tests.bot.conftest import button_data, buttons, menu_labels


def _set(field: str, value: str) -> str:
    return FieldCb(field=field, action=FieldAction.SET, value=value).pack()


def _skip(field: str) -> str:
    return FieldCb(field=field, action=FieldAction.CLEAR).pack()


async def _answer_required(harness, user, gender: str = "female") -> None:
    """Rules → Брат/Сестра → age → city → nationality → the four new fields → about → wishes."""
    await harness.press(user, ProfileCb(action=ProfileAction.RULES_OK).pack())
    await harness.press(user, _set("gender", gender))
    await harness.text(user, "28")
    await harness.text(user, "Казань")
    await harness.text(user, "татарка")
    for field, value in (
        ("marital_status", "never_married"),
        ("children", "0"),
        ("education", "higher"),
        ("employment", "employed"),
    ):
        await harness.press(user, _set(field, value))
    await harness.press(user, _skip("about"))
    await harness.text(user, "Ищу верующего мужа, чтобы создать крепкую семью.")


async def test_registration_in_the_legacy_order_with_consent_and_18_plus(harness, session, make_user):
    user = await make_user(consent=False)
    await harness.text(user, "Создать анкету")
    assert harness.last_text(user) == texts.RULES
    assert [b.text for b in buttons(harness.last_markup(user))] == ["Согласен(на) с правилами", "Отмена"]

    await harness.press(user, ProfileCb(action=ProfileAction.RULES_OK).pack())
    assert "Кто вы?" in harness.last_text(user)
    assert [b.text for b in buttons(harness.last_markup(user))][:2] == ["Брат", "Сестра"]
    await harness.press(user, _set("gender", "female"))
    assert "Сколько вам лет? (от 18 до 80)" in harness.last_text(user)
    await harness.text(user, "17")
    assert "Возраст — от 18 до 80 лет." in harness.last_text(user)
    await harness.text(user, "двадцать")
    assert "Введите целое число." in harness.last_text(user)
    await harness.text(user, "28")
    assert "Из какого вы города?" in harness.last_text(user)
    await harness.text(user, "казань")
    assert "Ваша национальность?" in harness.last_text(user)
    await harness.text(user, "татарка")
    assert "Ваше семейное положение?" in harness.last_text(user)
    labels = [b.text for b in buttons(harness.last_markup(user))]
    assert "Не была замужем" in labels  # gender-aware choices
    await harness.press(user, _set("marital_status", "never_married"))
    await harness.press(user, _set("children", "0"))
    await harness.press(user, _set("education", "higher"))
    await harness.press(user, _set("employment", "employed"))
    assert "Расскажите о себе кратко" in harness.last_text(user)
    await harness.text(user, "Пишите мне @fatima")  # contacts are rejected like on the site
    assert "Уберите ссылки, телефоны" in harness.last_text(user)
    await harness.press(user, _skip("about"))
    assert "Опишите пожелания к будущему супругу" in harness.last_text(user)
    assert "Готово, остальное позже" not in str(harness.last_markup(user))  # wishes are required
    await harness.text(user, "Ищу верующего мужа, чтобы создать крепкую семью.")
    assert "Готово, остальное позже" in str(harness.last_markup(user))  # optional extras now
    await harness.text(user, "165")
    await harness.press(user, FieldCb(field="weight", action=FieldAction.FINISH).pack())

    preview = harness.last_text(user)
    assert preview.startswith("Проверьте анкету. После отправки её увидит модератор.\n\n<b>Новая анкета</b> — Сестра")
    assert "Возраст: 28" in preview and "Город: Казань" in preview and "Рост: 165 см" in preview
    await harness.press(user, button_data(harness.last_markup(user), "Отправить на модерацию"))
    assert "http://testserver/consent" in harness.last_text(user)
    assert await profiles.get_profile(session, user.id) is None  # nothing saved before consent and 18+
    await harness.press(user, ProfileCb(action=ProfileAction.CONSENT).pack())
    assert "18 лет" in harness.last_text(user) and "не замужем" in harness.last_text(user)
    await harness.press(user, ProfileCb(action=ProfileAction.ADULT).pack())

    profile = await profiles.get_profile(session, user.id)
    assert (profile.gender, profile.number, profile.city, profile.nationality, profile.height, profile.about) == (
        Gender.FEMALE,
        500,
        "Казань",
        "татарка",
        165,
        None,
    )
    assert profile.birth_year == current_year() - 28  # the age became a birth year
    assert profile.status == ProfileStatus.PENDING
    await session.refresh(user)
    assert user.consent_at is not None
    assert await session.scalar(select(Outbox).where(Outbox.kind == "profile.submitted")) is not None
    assert harness.last_text(user) == ("Ваша анкета №500 отправлена на модерацию. Мы уведомим вас, когда её одобрят.")
    assert menu_labels(harness.last_markup(user))[0] == ["Смотреть анкеты", "Моя анкета"]  # menu with a profile


async def test_create_command_and_existing_profile(harness, make_profile, make_user):
    owner = (await make_profile()).user
    await harness.text(owner, "/create")
    assert harness.last_text(owner) == texts.PROFILE_EXISTS
    newcomer = await make_user()
    await harness.text(newcomer, "/create")
    assert harness.last_text(newcomer) == texts.RULES


async def test_choice_steps_require_buttons_and_stale_buttons_are_ignored(harness, make_user):
    user = await make_user()
    await harness.text(user, "/create")
    await harness.press(user, ProfileCb(action=ProfileAction.RULES_OK).pack())
    await harness.text(user, "Брат")
    assert harness.last_text(user) == texts.WIZARD_PICK_BUTTON
    await harness.press(user, _set("education", "higher"))  # not the current step
    assert harness.callback_answers()[-1].text == texts.STALE_BUTTON


async def test_cancel_leaves_wizard_and_menu_button_works_mid_wizard(harness, session, make_user):
    user = await make_user()
    await harness.text(user, "/create")
    await harness.press(user, ProfileCb(action=ProfileAction.RULES_OK).pack())
    await harness.press(user, _set("gender", "male"))

    await harness.text(user, "Смотреть анкеты")  # not taken as the age
    assert harness.last_text(user) == texts.PROFILE_REQUIRED

    await harness.text(user, "/create")
    await harness.text(user, "/cancel")
    assert harness.last_text(user) == texts.CANCELLED
    await harness.text(user, "Ахмад")  # no wizard any more: plain text gets the chat hint
    assert harness.last_text(user) == texts.NO_OPEN_CHAT
    await harness.press(user, CANCEL)
    assert await profiles.get_profile(session, user.id) is None


async def test_edit_free_text_goes_back_to_moderation(harness, session, make_profile):
    profile = await make_profile(gender=Gender.MALE, city="Казань")
    user = profile.user
    await harness.text(user, "Моя анкета")
    await harness.press(user, button_data(harness.last_markup(user), "Изменить"))

    picker = [button.text for button in buttons(harness.last_markup(user))]
    assert "Город: Казань" in picker and "Возраст: 30" in picker
    assert not any(text.startswith("Пол") for text in picker)  # gender cannot change
    await harness.press(user, FieldCb(field="city", action=FieldAction.PICK).pack())
    assert "Сейчас: <i>Казань</i>" in harness.last_text(user)
    await harness.text(user, "Уфа")
    assert "Проверьте изменения" in harness.last_text(user) and "Город: Уфа" in harness.last_text(user)

    await harness.press(user, ProfileCb(action=ProfileAction.MORE).pack())  # «Изменить ещё»
    await harness.press(user, FieldCb(field="age", action=FieldAction.PICK).pack())
    await harness.text(user, "35")
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())

    updated = await session.scalar(
        select(Profile).where(Profile.user_id == user.id).execution_options(populate_existing=True)
    )
    assert (updated.number, updated.city, updated.birth_year, updated.status) == (
        500,
        "Уфа",
        current_year() - 35,
        ProfileStatus.PENDING,
    )
    assert "отправлены на модерацию" in harness.last_text(user)
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 1  # one moderation card


async def test_edit_of_choices_and_numbers_keeps_the_profile_published(harness, session, make_profile):
    # A migrated profile whose legacy wishes are shorter than today's minimum can still be edited.
    profile = await make_profile(gender=Gender.MALE, wishes="Верующая")
    user = profile.user
    await harness.press(user, ProfileCb(action=ProfileAction.EDIT).pack())
    await harness.press(user, FieldCb(field="height", action=FieldAction.PICK).pack())
    await harness.text(user, "180")
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())

    await session.refresh(profile)
    assert (profile.height, profile.status, profile.wishes) == (180, ProfileStatus.ACTIVE, "Верующая")
    assert harness.last_text(user) == "Изменения сохранены."
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_new_profile_can_skip_the_remaining_optional_fields(harness, session, make_user):
    user = await make_user()
    await harness.text(user, "/create")
    await _answer_required(harness, user, gender="male")
    await harness.text(user, "180")  # the first optional extra (height) is answered

    await harness.press(user, FieldCb(field="weight", action=FieldAction.FINISH).pack())
    assert "Проверьте анкету" in harness.last_text(user)
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())
    await harness.press(user, ProfileCb(action=ProfileAction.ADULT).pack())  # consent was given before

    profile = await profiles.get_profile(session, user.id)
    assert (profile.height, profile.weight, profile.about, profile.nationality) == (180, None, None, "татарка")


async def test_my_profile_shows_legacy_card_status_and_hide_show(harness, session, make_profile):
    profile = await make_profile()
    user = profile.user
    await harness.text(user, "/myprofile")
    card = harness.last_text(user)
    assert card.startswith("<b>Анкета №500</b> — Брат\nВозраст: 30") and "<i>Статус: опубликована</i>" in card
    assert [b.text for b in buttons(harness.last_markup(user))] == ["Изменить", "Скрыть", "Удалить аккаунт"]

    await harness.press(user, ProfileCb(action=ProfileAction.HIDE).pack())
    await session.refresh(profile)
    assert profile.status == ProfileStatus.HIDDEN
    await harness.press(user, ProfileCb(action=ProfileAction.SHOW).pack())
    await session.refresh(profile)
    assert profile.status == ProfileStatus.ACTIVE


async def test_synonym_label_of_the_first_new_bot_opens_my_profile(harness, make_profile, make_user):
    owner = (await make_profile()).user
    await harness.text(owner, "📝 Анкета")
    assert harness.last_text(owner).startswith("<b>Анкета №500</b>")
    newcomer = await make_user()
    await harness.text(newcomer, "Моя анкета")
    assert harness.last_text(newcomer) == texts.NO_PROFILE


async def test_complete_migrated_profile_one_button_per_field_without_moderation(harness, session, make_profile):
    profile = await make_profile(
        gender=Gender.FEMALE, marital_status=None, children=None, education=None, employment=None
    )
    user = profile.user
    await harness.text(user, "/start")
    assert "не заполнено: семейное положение" in harness.last_text(user)
    assert [b.text for b in buttons(harness.last_markup(user))] == [texts.COMPLETE_BUTTON]

    await harness.press(user, ProfileCb(action=ProfileAction.COMPLETE).pack())
    assert "Ваше семейное положение?" in harness.last_text(user) and "Осталось вопросов: 4" in harness.last_text(user)
    await harness.press(user, FillCb(field="marital_status", value="divorced").pack())
    await harness.press(user, FillCb(field="children", value="2").pack())
    await harness.press(user, FillCb(field="marital_status", value="widowed").pack())  # an old button: ignored
    await harness.press(user, FillCb(field="education", value="religious").pack())
    await harness.press(user, FillCb(field="employment", value="business").pack())
    assert harness.last_text(user) == texts.COMPLETED

    await session.refresh(profile)
    assert (profile.marital_status, profile.children, profile.education, profile.employment) == (
        MaritalStatus.DIVORCED,
        2,
        Education.RELIGIOUS,
        Employment.BUSINESS,
    )
    assert profile.status == ProfileStatus.ACTIVE and profile.approved_at is not None  # no re-moderation
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 0

    await harness.text(user, "Моя анкета")
    assert texts.COMPLETE_BUTTON not in [b.text for b in buttons(harness.last_markup(user))]
