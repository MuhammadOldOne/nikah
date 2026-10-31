"""Buttons of the legacy bot still on people's screens (docs/LEGACY_BOT.md §3): every format is
understood, goes through the services with their checks, and anything unknown gets a polite
«Кнопка устарела»."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import AdminProfileCb, CatalogAction, CatalogCb
from app.enums import Gender, InterestStatus, ProfileStatus
from app.models import Interest
from app.services import interests
from app.services.errors import PermissionDenied
from tests.bot.conftest import buttons


async def test_legacy_interest_decision_buttons(harness, session, make_pair, make_user):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.press(man, f"int:acc:{interest.id}")  # not the recipient
    assert harness.callback_answers()[-1].text == "Отклик не найден."
    await harness.press(woman, f"int:dec:{interest.id}")
    await session.refresh(interest)
    assert interest.status == InterestStatus.DECLINED
    await harness.press(woman, f"int:acc:{interest.id}")
    assert harness.callback_answers()[-1].text == "На этот отклик уже ответили или его отозвали."


async def test_legacy_moderation_buttons_check_admin_and_staleness(harness, session, make_profile, make_user):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING, number=512)
    sent = pending.submitted_at.replace(microsecond=0) + timedelta(seconds=1)  # the old bot's card

    intruder = await make_user()
    await harness.press(intruder, "adm:appr:512", message_date=sent)
    assert harness.callback_answers()[-1].text == PermissionDenied().message

    admin = await make_user(is_admin=True)
    await harness.press(admin, "adm:appr:512", message_date=sent - timedelta(days=1))  # sent before an edit
    assert "изменена после отправки" in harness.callback_answers()[-1].text
    current = AdminProfileCb.unpack(buttons(harness.last_markup(admin))[0].callback_data)
    assert current.user_id == pending.user_id  # the current card was sent instead
    await session.refresh(pending)
    assert pending.status == ProfileStatus.PENDING

    await harness.press(admin, "adm:rej:512", message_date=sent)
    reasons = harness.api.requests[-1]
    assert type(reasons).__name__ == "EditMessageReplyMarkup"  # asks for the reason, as new cards do
    assert "Неполные данные" in [b.text for b in buttons(reasons.reply_markup)]

    await harness.press(admin, "adm:appr:512", message_date=sent)
    await session.refresh(pending)
    assert pending.status == ProfileStatus.ACTIVE
    assert harness.last_text(admin) == "Анкета №512 одобрена."
    await harness.press(admin, "adm:appr:512", message_date=sent)
    assert harness.callback_answers()[-1].text == "Уже обработана: опубликована."
    await harness.press(admin, "adm:rej:999")
    assert harness.callback_answers()[-1].text == "Анкета не найдена."


async def test_legacy_browse_buttons_reopen_browsing(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    first = await make_profile(gender=Gender.FEMALE)
    second = await make_profile(gender=Gender.FEMALE)

    await harness.press(man, "br:n:1")
    assert harness.edited_texts()[-1].startswith(f"<b>Анкета №{first.number}</b>")  # the 2nd card (newest first)
    await harness.press(man, "br:p:0")
    assert harness.edited_texts()[-1].startswith(f"<b>Анкета №{second.number}</b>")
    await harness.press(man, "br:n:40")  # the catalog got shorter: the last card
    assert harness.edited_texts()[-1].startswith(f"<b>Анкета №{first.number}</b>")
    await harness.press(man, "br:noop")
    await harness.press(man, "br:close")
    assert type(harness.api.requests[-1]).__name__ == "DeleteMessage"

    await harness.press(man, f"br:int:{second.number}")  # «Написать (заинтересоваться)» on an old card
    assert harness.last_text(man) == texts.INTEREST_MESSAGE_PROMPT
    await harness.press(man, CatalogCb(action=CatalogAction.NO_MESSAGE).pack())
    interest = await session.scalar(select(Interest))
    assert (interest.from_user_id, interest.to_user_id) == (man.id, second.user_id)


async def test_legacy_browse_needs_an_approved_profile(harness, make_profile):
    pending = (await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)).user
    await make_profile(gender=Gender.FEMALE)
    await harness.press(pending, "br:n:1")
    assert harness.last_text(pending) == texts.NOT_APPROVED
    await harness.press(pending, "br:int:501")
    assert harness.callback_answers()[-1].text == texts.NOT_APPROVED


@pytest.mark.parametrize("data", ["reg:consent", "reg:g:m", "reg:submit", "reg:cancel"])
async def test_legacy_registration_buttons_restart_registration(harness, make_user, data):
    user = await make_user()
    await harness.press(user, data)
    assert harness.callback_answers()[-1].text == "Анкету заполняем заново: нажмите «Создать анкету»."
    assert [b.text for b in buttons(harness.last_markup(user))] == ["Создать анкету"]


@pytest.mark.parametrize("data", ["something:old", "int:acc:x", "adm:other:1", "w:unknown:5", "rep:abc", ""])
async def test_unknown_buttons_get_a_polite_answer(harness, make_profile, data):
    user = (await make_profile()).user
    await harness.press(user, data or "x")
    answer = harness.callback_answers()[-1]
    assert answer.text == texts.STALE_BUTTON and answer.text.startswith("Кнопка устарела")


async def test_legacy_buttons_on_very_old_messages_still_work(harness, session, make_pair):
    """Messages older than 48 hours come as «inaccessible» to the bot: the actions still work."""
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()
    await harness.press(woman, f"int:acc:{interest.id}", message_date=datetime(2026, 1, 1, tzinfo=UTC))
    await session.refresh(interest)
    assert interest.status == InterestStatus.ACCEPTED
