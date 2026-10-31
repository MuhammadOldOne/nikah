"""Profile wizard in the bot: same fields and validation as the site, consent, 18+, editing."""

from __future__ import annotations

from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import CANCEL, FieldAction, FieldCb, ProfileAction, ProfileCb
from app.clock import current_year
from app.enums import Gender, ProfileStatus
from app.models import Outbox, Profile
from app.services import profiles
from tests.bot.conftest import button_data


def _set(field: str, value: str) -> str:
    return FieldCb(field=field, action=FieldAction.SET, value=value).pack()


def _skip(field: str) -> str:
    return FieldCb(field=field, action=FieldAction.CLEAR).pack()


async def test_wizard_creates_pending_profile_with_consent(harness, session, make_user):
    user = await make_user(consent=False)
    await harness.text(user, texts.MENU_PROFILE)
    await harness.press(user, ProfileCb(action=ProfileAction.CREATE).pack())

    await harness.press(user, _set("gender", "female"))
    await harness.text(user, "Фатима")
    await harness.text(user, "2015")  # under 18: rejected, the step is asked again
    assert "только с 18 лет" in harness.last_text(user)
    await harness.text(user, str(current_year() - 28))
    await harness.text(user, "казань")
    labels = [b.text for row in harness.last_markup(user).inline_keyboard for b in row]
    assert "Не была замужем" in labels  # gender-aware choices
    await harness.press(user, _set("marital_status", "never_married"))
    await harness.press(user, _set("children", "0"))
    await harness.press(user, _set("education", "higher"))
    await harness.press(user, _set("employment", "employed"))
    await harness.text(user, "Ищу верующего мужа, чтобы создать крепкую семью.")
    await harness.text(user, "165")
    for field in ("weight", "occupation", "nationality", "madhab", "prayer", "nikah_timeline", "relocation"):
        await harness.press(user, _skip(field))
    await harness.text(user, "Пишите мне @fatima")  # contacts are rejected like on the site
    assert "Уберите ссылки, телефоны" in harness.last_text(user)
    await harness.press(user, _skip("about"))

    summary = harness.last_text(user)
    assert "Проверьте анкету" in summary and "<b>Фатима</b>" in summary and "📍 Казань" in summary
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())
    assert "/consent" in harness.last_text(user)
    assert await profiles.get_profile(session, user.id) is None  # nothing saved before consent and 18+
    await harness.press(user, ProfileCb(action=ProfileAction.CONSENT).pack())
    assert "18 лет" in harness.last_text(user) and "не замужем" in harness.last_text(user)
    await harness.press(user, ProfileCb(action=ProfileAction.ADULT).pack())

    profile = await profiles.get_profile(session, user.id)
    assert profile is not None
    assert (profile.gender, profile.name, profile.city, profile.height, profile.about) == (
        Gender.FEMALE,
        "Фатима",
        "Казань",
        165,
        None,
    )
    assert profile.status == ProfileStatus.PENDING
    await session.refresh(user)
    assert user.consent_at is not None
    assert await session.scalar(select(Outbox).where(Outbox.kind == "profile.submitted")) is not None
    assert "отправлена на модерацию" in harness.last_text(user)


async def test_choice_steps_require_buttons_and_stale_buttons_are_ignored(harness, make_user):
    user = await make_user()
    await harness.press(user, ProfileCb(action=ProfileAction.CREATE).pack())
    await harness.text(user, "Мужчина")
    assert harness.last_text(user) == texts.WIZARD_PICK_BUTTON
    await harness.press(user, _set("education", "higher"))  # not the current step
    assert harness.callback_answers()[-1].text == texts.STALE_BUTTON


async def test_cancel_leaves_wizard_and_menu_button_works_mid_wizard(harness, session, make_user):
    user = await make_user()
    await harness.press(user, ProfileCb(action=ProfileAction.CREATE).pack())
    await harness.press(user, _set("gender", "male"))

    await harness.text(user, texts.MENU_CATALOG)  # not taken as the name
    assert harness.last_text(user) == texts.PROFILE_REQUIRED

    await harness.press(user, ProfileCb(action=ProfileAction.CREATE).pack())
    await harness.text(user, "/cancel")
    assert harness.last_text(user) == texts.CANCELLED
    await harness.text(user, "Ахмад")  # no wizard any more: plain text gets the chat hint
    assert harness.last_text(user) == texts.NO_ACTIVE_CHAT
    await harness.press(user, CANCEL)
    assert await profiles.get_profile(session, user.id) is None


async def test_edit_changes_one_field_and_resubmits(harness, session, make_profile):
    profile = await make_profile(gender=Gender.MALE, name="Ахмад", city="Казань")
    user = profile.user
    await harness.text(user, texts.MENU_PROFILE)
    await harness.press(user, button_data(harness.last_markup(user), "Изменить"))

    picker = [button.text for row in harness.last_markup(user).inline_keyboard for button in row]
    assert "Имя или псевдоним: Ахмад" in picker and "Город: Казань" in picker
    assert not any(text.startswith("Пол") for text in picker)  # gender cannot change
    await harness.press(user, FieldCb(field="city", action=FieldAction.PICK).pack())
    assert "Сейчас: <i>Казань</i>" in harness.last_text(user)
    await harness.text(user, "Уфа")
    assert "Проверьте анкету" in harness.last_text(user) and "📍 Уфа" in harness.last_text(user)

    await harness.press(user, ProfileCb(action=ProfileAction.MORE).pack())  # «Изменить ещё»
    await harness.press(user, FieldCb(field="height", action=FieldAction.PICK).pack())
    await harness.text(user, "180")
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())

    updated = await session.scalar(
        select(Profile).where(Profile.user_id == user.id).execution_options(populate_existing=True)
    )
    assert (updated.name, updated.city, updated.height, updated.status) == (
        "Ахмад",
        "Уфа",
        180,
        ProfileStatus.PENDING,
    )
    assert "отправлены на модерацию" in harness.last_text(user)
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 1  # one moderation card


async def test_new_profile_can_skip_the_remaining_optional_fields(harness, session, make_user):
    user = await make_user()
    await harness.press(user, ProfileCb(action=ProfileAction.CREATE).pack())
    await harness.press(user, _set("gender", "male"))
    await harness.text(user, "Ахмад")
    await harness.text(user, str(current_year() - 30))
    await harness.text(user, "Казань")
    for field, value in (("marital_status", "never_married"), ("children", "0"), ("education", "higher")):
        await harness.press(user, _set(field, value))
    await harness.press(user, _set("employment", "employed"))
    assert "Готово, остальное позже" not in str(harness.last_markup(user))  # required step
    await harness.text(user, "Ищу верующую и заботливую жену для крепкой семьи.")
    await harness.text(user, "180")  # the first optional step (height) is answered

    await harness.press(user, FieldCb(field="weight", action=FieldAction.FINISH).pack())
    assert "Проверьте анкету" in harness.last_text(user)
    await harness.press(user, ProfileCb(action=ProfileAction.SAVE).pack())
    await harness.press(user, ProfileCb(action=ProfileAction.ADULT).pack())

    profile = await profiles.get_profile(session, user.id)
    assert (profile.height, profile.weight, profile.about) == (180, None, None)


async def test_hide_and_show_profile(harness, session, make_profile):
    profile = await make_profile()
    user = profile.user
    await harness.press(user, ProfileCb(action=ProfileAction.HIDE).pack())
    await session.refresh(profile)
    assert profile.status == ProfileStatus.HIDDEN
    await harness.press(user, ProfileCb(action=ProfileAction.SHOW).pack())
    await session.refresh(profile)
    assert profile.status == ProfileStatus.ACTIVE
