"""Catalog browsing, filters, interests and reports from the bot."""

from __future__ import annotations

from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import CatalogAction, CatalogCb, InterestAction, InterestCb, ReportCb
from app.clock import current_year
from app.enums import Gender, InterestStatus, ProfileStatus, ReportReason
from app.models import Conversation, Interest, Report
from app.services import interests
from tests.bot.conftest import button_data


async def test_browse_one_card_at_a_time_and_send_interest(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    first = (await make_profile(gender=Gender.FEMALE, name="Фатима")).user
    second = (await make_profile(gender=Gender.FEMALE, name="Мариям")).user

    await harness.text(man, texts.MENU_CATALOG)
    assert "<b>Мариям</b>" in harness.last_text(man) and "Анкета 1 из 2" in harness.last_text(man)
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT).pack())
    assert "<b>Фатима</b>" in harness.edited_texts()[-1]
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT).pack())
    assert "посмотрели все анкеты" in harness.edited_texts()[-1]

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, user_id=first.id).pack())
    assert harness.last_text(man) == texts.INTEREST_MESSAGE_PROMPT
    await harness.text(man, "Мой номер +7 900 123-45-67")  # contacts are not allowed before consent
    assert "Уберите ссылки, телефоны" in harness.last_text(man)
    await harness.text(man, "Ас-саляму алейкум! Хотел бы познакомиться.")

    interest = await session.scalar(select(Interest))
    assert (interest.from_user_id, interest.to_user_id, interest.message) == (
        man.id,
        first.id,
        "Ас-саляму алейкум! Хотел бы познакомиться.",
    )
    assert "Отклик отправлен" in harness.last_text(man)

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, user_id=second.id).pack())
    await harness.press(man, CatalogCb(action=CatalogAction.NO_MESSAGE).pack())
    assert await session.scalar(select(Interest).where(Interest.to_user_id == second.id)) is not None


async def test_domain_errors_are_shown(harness, session, make_profile, override_settings):
    man = (await make_profile(gender=Gender.MALE)).user
    woman = (await make_profile(gender=Gender.FEMALE)).user
    other = (await make_profile(gender=Gender.FEMALE)).user
    override_settings(INTEREST_DAILY_LIMIT=1)
    await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, user_id=other.id).pack())
    await harness.press(man, CatalogCb(action=CatalogAction.NO_MESSAGE).pack())
    assert "Лимит откликов на сегодня исчерпан" in harness.last_text(man)

    pending = (await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)).user
    await harness.press(pending, CatalogCb(action=CatalogAction.INTEREST, user_id=woman.id).pack())
    assert "после того, как ваша анкета будет опубликована" in harness.callback_answers()[-1].text


async def test_filter_by_age_and_city(harness, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    await make_profile(gender=Gender.FEMALE, name="Айша", birth_year=current_year() - 22, city="Уфа")
    await make_profile(gender=Gender.FEMALE, name="Зайнаб", birth_year=current_year() - 35, city="Уфа")
    await make_profile(gender=Gender.FEMALE, name="Хадиджа", birth_year=current_year() - 23, city="Казань")

    await harness.press(man, CatalogCb(action=CatalogAction.FILTER).pack())
    await harness.text(man, "сто")
    assert "Напишите возраст числами" in harness.last_text(man)
    await harness.text(man, "30-20")
    await harness.text(man, "уфа")

    text = harness.last_text(man)
    assert "<b>Айша</b>" in text and "Анкета 1 из 1" in text and "20–30 лет, уфа" in text


async def test_incoming_interest_accept_and_decline(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    other_man = (await make_profile(gender=Gender.MALE, name="Юсуф")).user
    woman = (await make_profile(gender=Gender.FEMALE)).user
    first = await interests.send_interest(session, man, woman.id, "Салам")
    second = await interests.send_interest(session, other_man, woman.id)
    await session.commit()

    await harness.text(woman, texts.MENU_INTERESTS)
    assert "Входящие отклики: 2" in "\n".join(harness.texts(woman))

    await harness.press(woman, InterestCb(action=InterestAction.ACCEPT, interest_id=first.id).pack())
    await harness.press(woman, InterestCb(action=InterestAction.DECLINE, interest_id=second.id).pack())

    conversation = await session.scalar(select(Conversation))
    assert conversation is not None and {conversation.user_a_id, conversation.user_b_id} == {man.id, woman.id}
    await session.refresh(second)
    assert second.status == InterestStatus.DECLINED

    await harness.press(man, InterestCb(action=InterestAction.ACCEPT, interest_id=second.id).pack())
    assert harness.callback_answers()[-1].text == "Отклик не найден."


async def test_outgoing_interest_can_be_withdrawn(harness, session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.text(man, texts.MENU_INTERESTS)
    assert "ожидает ответа" in harness.last_text(man)
    await harness.press(man, button_data(harness.last_markup(man), "Отозвать"))

    await session.refresh(interest)
    assert interest.status == InterestStatus.CANCELLED


async def test_report_from_catalog(harness, session, make_pair):
    man, woman = await make_pair()
    await harness.press(woman, CatalogCb(action=CatalogAction.REPORT, user_id=man.id).pack())
    await harness.press(woman, ReportCb(user_id=man.id, reason=ReportReason.FRAUD).pack())
    await harness.text(woman, "Просил перевести деньги")

    report = await session.scalar(select(Report))
    assert (report.reporter_id, report.target_user_id, report.reason, report.comment) == (
        woman.id,
        man.id,
        ReportReason.FRAUD,
        "Просил перевести деньги",
    )
    assert harness.last_text(woman) == texts.REPORT_SENT
