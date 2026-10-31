"""Browsing in the legacy style (⬅️ N/M ➡️, «Написать (заинтересоваться)», «Закрыть»), gating,
filters, interests with an optional message and «Отклики»."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import REPORT_SEND, CatalogAction, CatalogCb, InterestAction, InterestCb, ReportCb
from app.clock import current_year, utcnow
from app.enums import Gender, InterestStatus, ProfileStatus, ReportReason
from app.models import Conversation, Interest, Report
from app.services import interests
from tests.bot.conftest import button_data, buttons


def _nav(harness, user) -> list[str]:
    return [button.text for button in buttons(harness.last_markup(user))]


async def test_browse_one_card_with_legacy_buttons_and_send_interest(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    first = await make_profile(gender=Gender.FEMALE, approved_at=utcnow() - timedelta(minutes=5))
    second = await make_profile(gender=Gender.FEMALE)

    await harness.text(man, "Смотреть анкеты")
    assert harness.last_text(man).startswith("<b>Анкета №502</b> — Сестра\nВозраст: 30")
    assert _nav(harness, man) == ["1/2", "➡️", "Написать (заинтересоваться)", "Пожаловаться", "Фильтр", "Закрыть"]
    await harness.press(man, button_data(harness.last_markup(man), "➡️"))
    assert harness.edited_texts()[-1].startswith("<b>Анкета №501</b>")
    edit = harness.api.requests[-1]
    assert [b.text for b in buttons(edit.reply_markup)][:2] == ["⬅️", "2/2"]
    await harness.press(man, button_data(edit.reply_markup, "⬅️"))
    assert harness.edited_texts()[-1].startswith("<b>Анкета №502</b>")

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=first.number).pack())
    assert harness.last_text(man) == texts.INTEREST_MESSAGE_PROMPT
    await harness.text(man, "Мой номер +7 900 123-45-67")  # contacts are not allowed before consent
    assert "Уберите ссылки, телефоны" in harness.last_text(man)
    await harness.text(man, "Ас-саляму алейкум! Хотел бы познакомиться.")

    interest = await session.scalar(select(Interest))
    assert (interest.from_user_id, interest.to_user_id, interest.message) == (
        man.id,
        first.user_id,
        "Ас-саляму алейкум! Хотел бы познакомиться.",
    )
    assert harness.last_text(man) == "Ваш отклик анкете №501 отправлен. Когда поступит ответ, бот сообщит."

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=second.number).pack())
    await harness.press(man, CatalogCb(action=CatalogAction.NO_MESSAGE).pack())
    sent = await session.scalar(select(Interest).where(Interest.to_user_id == second.user_id))
    assert sent is not None and sent.message is None


async def test_browse_command_close_and_empty_catalog(harness, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    await harness.text(man, "/browse")
    assert harness.last_text(man) == texts.CATALOG_EMPTY
    await make_profile(gender=Gender.FEMALE)
    await harness.text(man, "🔍 Смотреть анкеты")  # the first new bot's label still works
    await harness.press(man, CatalogCb(action=CatalogAction.CLOSE).pack())
    assert type(harness.api.requests[-1]).__name__ == "DeleteMessage"


async def test_gating_texts_of_the_legacy_bot(harness, make_profile, make_user):
    newcomer = await make_user()
    await harness.text(newcomer, "Смотреть анкеты")
    assert harness.last_text(newcomer) == "Сначала создайте анкету."
    pending = (await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)).user
    woman = await make_profile(gender=Gender.FEMALE)
    await harness.text(pending, "/browse")
    assert harness.last_text(pending) == "Ваша анкета ещё не одобрена. Просмотр откроется после модерации."
    await harness.press(pending, CatalogCb(action=CatalogAction.INTEREST, number=woman.number).pack())
    assert harness.callback_answers()[-1].text == texts.NOT_APPROVED
    rejected = (await make_profile(gender=Gender.MALE, status=ProfileStatus.REJECTED)).user
    await harness.text(rejected, "/browse")
    assert "отклонена модератором" in harness.last_text(rejected)


async def test_domain_errors_are_shown(harness, session, make_profile, override_settings):
    man = (await make_profile(gender=Gender.MALE)).user
    woman = (await make_profile(gender=Gender.FEMALE)).user
    other = await make_profile(gender=Gender.FEMALE)
    override_settings(INTEREST_DAILY_LIMIT=1)
    await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.press(man, CatalogCb(action=CatalogAction.INTEREST, number=other.number).pack())
    await harness.press(man, CatalogCb(action=CatalogAction.NO_MESSAGE).pack())
    assert "Лимит откликов на сегодня исчерпан" in harness.last_text(man)


async def test_filter_by_age_and_city(harness, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    aisha = await make_profile(gender=Gender.FEMALE, birth_year=current_year() - 22, city="Уфа")
    await make_profile(gender=Gender.FEMALE, birth_year=current_year() - 35, city="Уфа")
    await make_profile(gender=Gender.FEMALE, birth_year=current_year() - 23, city="Казань")

    await harness.press(man, CatalogCb(action=CatalogAction.FILTER).pack())
    await harness.text(man, "сто")
    assert "Напишите возраст числами" in harness.last_text(man)
    await harness.text(man, "30-20")
    await harness.text(man, "уфа")

    text = harness.last_text(man)
    assert f"<b>Анкета №{aisha.number}</b>" in text and "Фильтр: 20–30 лет, уфа" in text
    assert "1/1" in _nav(harness, man) and "Сбросить фильтр" in _nav(harness, man)


async def test_incoming_interest_accept_and_decline(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    other_man = (await make_profile(gender=Gender.MALE)).user
    woman = (await make_profile(gender=Gender.FEMALE)).user
    first = await interests.send_interest(session, man, woman.id, "Салам")
    second = await interests.send_interest(session, other_man, woman.id)
    await session.commit()

    await harness.text(woman, "Отклики")
    joined = "\n".join(harness.texts(woman))
    assert "Входящие отклики: 2" in joined and "<b>Вами заинтересовалась анкета №500</b>" in joined

    await harness.press(woman, f"int:acc:{first.id}")
    assert harness.last_text(woman).startswith("<b>Чат открыт между анкетами №502 и №500.</b>")
    await harness.press(woman, f"int:dec:{second.id}")
    assert harness.edited_texts()[-1].endswith("Вы отклонили отклик.")

    conversation = await session.scalar(select(Conversation))
    assert conversation is not None and {conversation.user_a_id, conversation.user_b_id} == {man.id, woman.id}
    await session.refresh(second)
    assert second.status == InterestStatus.DECLINED

    await harness.press(man, InterestCb(action=InterestAction.ACCEPT, interest_id=second.id).pack())
    assert harness.callback_answers()[-1].text == "Отклик не найден."


async def test_accept_at_the_open_chat_limit_shows_the_error_verbatim(
    harness, session, make_profile, make_conversation
):
    sister = (await make_profile(gender=Gender.FEMALE)).user
    ahmad = (await make_profile(gender=Gender.MALE)).user
    yusuf = (await make_profile(gender=Gender.MALE)).user
    await make_conversation(ahmad, sister)
    interest = await interests.send_interest(session, yusuf, sister.id)
    await session.commit()

    await harness.press(sister, InterestCb(action=InterestAction.ACCEPT, interest_id=interest.id).pack())

    answer = harness.callback_answers()[-1]
    assert answer.text == "У одного из участников уже есть открытый чат. Завершите его сначала." and answer.show_alert


async def test_outgoing_interest_can_be_withdrawn(harness, session, make_pair):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.text(man, "💌 Отклики")
    assert "<b>Анкета №501</b> — ожидает ответа" in harness.last_text(man)
    await harness.press(man, button_data(harness.last_markup(man), "Отозвать"))

    await session.refresh(interest)
    assert interest.status == InterestStatus.CANCELLED


async def test_report_from_catalog_by_number(harness, session, make_pair):
    man, woman = await make_pair()
    await harness.press(woman, CatalogCb(action=CatalogAction.REPORT, number=500).pack())
    await harness.press(woman, ReportCb(number=500, reason=ReportReason.FRAUD).pack())
    await harness.text(woman, "Просил перевести деньги")

    report = await session.scalar(select(Report))
    assert (report.reporter_id, report.target_user_id, report.reason, report.comment) == (
        woman.id,
        man.id,
        ReportReason.FRAUD,
        "Просил перевести деньги",
    )
    assert harness.last_text(woman) == texts.REPORT_SENT


async def test_next_after_reporting_the_shown_card_skips_nobody(harness, session, make_profile):
    man = (await make_profile(gender=Gender.MALE)).user
    sisters = [  # newest approved first
        await make_profile(gender=Gender.FEMALE, approved_at=utcnow() - timedelta(minutes=age)) for age in range(5)
    ]

    await harness.text(man, "Смотреть анкеты")
    assert f"<b>Анкета №{sisters[0].number}</b>" in harness.last_text(man)
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT, number=sisters[0].number, page=1).pack())
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT, number=sisters[1].number, page=2).pack())
    assert f"<b>Анкета №{sisters[2].number}</b>" in harness.edited_texts()[-1]
    await harness.press(man, CatalogCb(action=CatalogAction.REPORT, number=sisters[2].number).pack())
    await harness.press(man, ReportCb(number=sisters[2].number, reason=ReportReason.FAKE).pack())
    await harness.press(man, REPORT_SEND)
    assert harness.last_text(man) == texts.REPORT_SENT

    # «➡️» on the reported card: the next card has moved up to its position.
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT, number=sisters[2].number, page=3).pack())
    assert f"<b>Анкета №{sisters[3].number}</b>" in harness.edited_texts()[-1]
    await harness.press(man, CatalogCb(action=CatalogAction.NEXT, number=sisters[3].number, page=3).pack())
    assert f"<b>Анкета №{sisters[4].number}</b>" in harness.edited_texts()[-1]
    assert "4/4" in [b.text for b in buttons(harness.api.requests[-1].reply_markup)]
