"""The consent gate for accounts migrated from the legacy bot (consent_at NULL, profile kept)."""

from __future__ import annotations

from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import GIVE_CONSENT, ProfileAction, ProfileCb
from app.enums import Gender, MessageOrigin, ProfileStatus
from app.models import Message, Profile, User
from app.services import chat
from tests.bot.conftest import buttons


async def _migrated(make_user, make_profile, **fields) -> User:
    user = await make_user(consent=False)
    await make_profile(user, **fields)
    return user


def _is_prompt(text: str) -> bool:
    return text.startswith("<b>Согласие на обработку персональных данных</b>") and "http://testserver/consent" in text


async def test_start_shows_the_greeting_then_the_consent_prompt(harness, make_user, make_profile):
    user = await _migrated(make_user, make_profile)
    await harness.text(user, "/start")
    greeting, prompt = harness.texts(user)
    assert greeting.startswith("Ассаляму алейкум") and _is_prompt(prompt)
    assert [b.text for b in buttons(harness.last_markup(user))] == ["Даю согласие"]


async def test_a_section_waits_for_consent_and_then_opens(harness, session, make_user, make_profile):
    user = await _migrated(make_user, make_profile)
    sister = await make_profile(gender=Gender.FEMALE)

    await harness.text(user, "Смотреть анкеты")
    assert _is_prompt(harness.last_text(user))
    await harness.press(user, GIVE_CONSENT)

    await session.refresh(user)
    assert user.consent_at is not None
    assert texts.CONSENT_GIVEN in harness.texts(user)
    assert harness.last_text(user).startswith(f"<b>Анкета №{sister.number}</b>")  # the browsing continued


async def test_a_chat_message_is_delivered_after_consent(harness, session, make_user, make_profile, make_conversation):
    user = await _migrated(make_user, make_profile)
    sister = (await make_profile(gender=Gender.FEMALE)).user
    user.consent_at = sister.consent_at  # the chat was opened in the legacy bot: consent only for setup
    await session.commit()
    conversation = await make_conversation(user, sister)
    user.consent_at = None
    await session.commit()

    await harness.text(user, "Ассаляму алейкум, сестра")
    assert _is_prompt(harness.last_text(user))
    assert (await session.scalars(select(Message))).all() == []

    await harness.press(user, GIVE_CONSENT)
    sent = await session.scalar(select(Message))
    assert (sent.conversation_id, sent.text, sent.origin) == (
        conversation.id,
        "Ассаляму алейкум, сестра",
        MessageOrigin.TELEGRAM,
    )


async def test_rules_help_delete_and_withdrawing_work_without_consent(
    harness, session, make_user, make_profile, make_conversation
):
    user = await _migrated(make_user, make_profile)
    sister = (await make_profile(gender=Gender.FEMALE)).user
    user.consent_at = sister.consent_at
    await session.commit()
    conversation = await make_conversation(user, sister)
    rude = await chat.send_message(session, conversation.id, sister, "Грубость", MessageOrigin.WEB)
    user.consent_at = None
    await session.commit()

    await harness.text(user, "Правила")
    assert harness.last_text(user) == texts.RULES
    await harness.text(user, "/help")
    assert harness.last_text(user) == texts.HELP
    await harness.press(user, f"rep:{rude.id}")
    assert harness.callback_answers()[-1].text == texts.MESSAGE_REPORTED
    await harness.press(user, "chat:close")
    await harness.press(user, f"chat:closeok:{conversation.id}")
    await session.refresh(conversation)
    assert conversation.closed_at is not None
    await harness.press(user, ProfileCb(action=ProfileAction.HIDE).pack())
    profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
    await session.refresh(profile)
    assert profile.status == ProfileStatus.HIDDEN

    await harness.press(user, ProfileCb(action=ProfileAction.SHOW).pack())  # not a withdrawal: gated
    assert _is_prompt(harness.last_text(user))
    await harness.text(user, "/delete")
    assert "Удалить аккаунт?" in harness.last_text(user)
    await harness.press(user, "acc:deleteok")
    assert await session.scalar(select(User).where(User.id == user.id)) is None


async def test_consent_then_completion_of_a_migrated_profile(harness, session, make_user, make_profile):
    user = await _migrated(make_user, make_profile, marital_status=None, children=None, education=None, employment=None)
    await harness.text(user, "/start")
    assert not any("не заполнено" in text for text in harness.texts(user))  # consent first
    await harness.press(user, GIVE_CONSENT)
    assert "не заполнено: семейное положение" in harness.last_text(user)
    assert [b.text for b in buttons(harness.last_markup(user))] == [texts.COMPLETE_BUTTON]


async def test_new_people_without_a_profile_are_not_gated(harness, make_user):
    newcomer = await make_user(consent=False)
    await harness.text(newcomer, "Создать анкету")
    assert harness.last_text(newcomer) == texts.RULES  # consent comes at the end of the form
    await harness.text(newcomer, "Сайт")
    assert harness.last_text(newcomer) == texts.SITE_LINK
