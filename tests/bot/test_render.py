"""Pure rendering of bot texts and outbox notifications (no database, no Telegram)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.bot import render
from app.bot.callbacks import AdminProfileCb, ChatCb, InterestCb
from app.clock import current_year
from app.enums import (
    ConversationCloseReason,
    Education,
    Employment,
    Gender,
    InterestStatus,
    Madhab,
    MaritalStatus,
    MessageOrigin,
    NikahTimeline,
    Prayer,
    ProfileStatus,
    Relocation,
    ReportReason,
    ReportStatus,
)
from app.models import Interest, Message, Profile, Report, User
from tests.bot.conftest import buttons


def person(user_id: int, name: str = "Ахмад", gender: Gender = Gender.MALE, **fields: object) -> User:
    user = User(id=user_id, telegram_id=1000 + user_id, first_name="<Tg>", username=None)
    values: dict[str, object] = {
        "user_id": user_id,
        "gender": gender,
        "name": name,
        "birth_year": current_year() - 30,
        "city": "Казань",
        "marital_status": MaritalStatus.NEVER_MARRIED,
        "children": 0,
        "education": Education.HIGHER,
        "employment": Employment.EMPLOYED,
        "wishes": "Ищу <верующую> супругу & хозяйку",
        "status": ProfileStatus.ACTIVE,
    }
    values.update(fields)
    user.profile = Profile(**values)
    return user


def message(text: str, sender: User, conversation_id: int = 7) -> Message:
    return Message(
        id=1,
        conversation_id=conversation_id,
        sender_id=sender.id,
        text=text,
        origin=MessageOrigin.WEB,
        created_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
    )


def test_profile_card_escapes_and_shows_fields():
    user = person(
        5, gender=Gender.FEMALE, name="Фатима", about="<script>alert(1)</script>", relocation=Relocation.READY
    )
    card = render.profile_card(user.profile, has_guardian=True)
    assert "<b>Фатима</b>, 30 лет · #5" in card
    assert "Не была замужем · детей нет" in card
    assert "&lt;верующую&gt; супругу &amp; хозяйку" in card
    assert "&lt;script&gt;" in card and "<script>" not in card
    assert "Переезд: готова" in card
    assert "Пожелания к супругу" in card
    assert render.GUARDIAN_BADGE in card


def test_message_notification_has_reply_and_site_buttons():
    sender = person(3)
    rendered = render.message_notification(sender, message("Как дела? <b>&</b>", sender, conversation_id=9))
    assert rendered.text == "💬 <b>Ахмад</b> (#3):\nКак дела? &lt;b&gt;&amp;&lt;/b&gt;"
    reply, site = buttons(rendered.reply_markup)
    assert ChatCb.unpack(reply.callback_data).conversation_id == 9
    assert site.url == "http://testserver/chats/9"


def test_guardian_copy_marks_whose_conversation_without_buttons():
    ward, partner = person(4, "Фатима", Gender.FEMALE), person(3)
    rendered = render.guardian_copy(ward, partner, partner, message("<i>Салам</i>", partner))
    assert rendered.text.startswith("👁 Копия переписки: <b>Фатима</b> (#4) ↔ <b>Ахмад</b> (#3)")
    assert rendered.text.endswith("<b>Ахмад</b>: &lt;i&gt;Салам&lt;/i&gt;")
    assert rendered.reply_markup is None


def test_interest_notification_card_message_and_buttons():
    sender = person(3)
    interest = Interest(id=11, from_user_id=3, to_user_id=4, message="Хочу <познакомиться>")
    rendered = render.interest_notification(interest, sender.profile, has_guardian=False)
    assert "<b>Ахмад</b>" in rendered.text and "Хочу &lt;познакомиться&gt;" in rendered.text
    accept, decline, site = buttons(rendered.reply_markup)
    assert InterestCb.unpack(accept.callback_data).interest_id == 11
    assert InterestCb.unpack(decline.callback_data).action == "decline"
    assert site.url == "http://testserver/u/3"


def test_interest_accepted_and_declined_are_gender_aware_and_gentle():
    accepted = render.interest_accepted(person(4, "Фатима", Gender.FEMALE), conversation_id=5)
    assert accepted.text == "Ваш отклик принят 🤲 Теперь можно общаться.\nСобеседник: <b>Фатима</b> (#4)."
    assert ChatCb.unpack(buttons(accepted.reply_markup)[0].callback_data).conversation_id == 5
    declined = render.interest_declined(person(4, "Фатима", Gender.FEMALE))
    assert "<b>Фатима</b> (#4) не готова продолжить знакомство" in declined.text
    assert "Не расстраивайтесь" in declined.text and declined.reply_markup is None


def test_conversation_closed_text_by_reason():
    partner = person(4, "Фатима", Gender.FEMALE)
    assert "завершила общение" in render.conversation_closed(ConversationCloseReason.CLOSED_BY_USER, partner).text
    banned_sister = render.conversation_closed(ConversationCloseReason.USER_BANNED, partner).text
    assert banned_sister == "🔒 Чат закрыт: собеседница <b>Фатима</b> (#4) заблокирована модератором."
    banned_brother = render.conversation_closed(ConversationCloseReason.USER_BANNED, person(5, "Ахмад", Gender.MALE))
    assert "собеседник <b>Ахмад</b> (#5) заблокирован модератором" in banned_brother.text
    assert "удалил аккаунт" in render.conversation_closed(ConversationCloseReason.ACCOUNT_DELETED, None).text


def test_moderation_card_has_versioned_buttons_and_flags():
    user = person(8, submitted_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC), flagged=True, flag_reason="крипт<о>")
    rendered = render.moderation_card(user.profile)
    assert "Стоп-слова: крипт&lt;о&gt;" in rendered.text
    approve, reject = buttons(rendered.reply_markup)
    data = AdminProfileCb.unpack(approve.callback_data)
    assert (data.action, data.user_id, data.version) == ("approve", 8, render.profile_version(user.profile))
    assert AdminProfileCb.unpack(reject.callback_data).action == "reject"


def test_profile_moderated_texts():
    published = person(8)
    assert "опубликована" in render.profile_moderated(published.profile).text
    rejected = person(8, status=ProfileStatus.REJECTED, moderation_note="Уберите <ссылки>")
    text = render.profile_moderated(rejected.profile).text
    assert "Причина: Уберите &lt;ссылки&gt;" in text


def test_report_card_escapes_comment_and_message():
    reporter, target = person(4, "Фатима", Gender.FEMALE), person(3)
    report = Report(id=2, reporter_id=4, target_user_id=3, reason=ReportReason.FRAUD, comment="<просил деньги>")
    report.reporter, report.target_user = reporter, target
    report.message = message("Переведи <на карту>", target)
    report.status = ReportStatus.OPEN
    rendered = render.report_card(report)
    assert "Жалоба #2" in rendered.text and "&lt;просил деньги&gt;" in rendered.text
    assert "Переведи &lt;на карту&gt;" in rendered.text
    assert len(buttons(rendered.reply_markup)) == 3


def test_guardian_linked_escapes_telegram_name():
    guardian = User(id=9, telegram_id=99, first_name="<Отец>", username="father_1")
    text = render.guardian_linked(guardian).text
    assert "&lt;Отец&gt; (@father_1)" in text and "Отключить вали" in text


def test_outgoing_interest_list_and_split_blocks():
    target = person(4, "Фатима", Gender.FEMALE)
    interest = Interest(id=1, from_user_id=3, to_user_id=4, status=InterestStatus.PENDING)
    interest.to_user = target
    assert "<b>Фатима</b> (#4) — ожидает ответа" in render.outgoing_interests([interest])
    chunks = render.split_blocks(["a" * 3000, "b" * 3000, "c"], limit=4000)
    assert chunks == ["a" * 3000, "b" * 3000 + "\n\nc"]


def test_longest_cards_fit_in_one_telegram_message():
    longest = {
        "name": "А" * 40,
        "city": "Б" * 60,
        "occupation": "В" * 100,
        "nationality": "Г" * 50,
        "wishes": "Д" * 1000,
        "about": "Е" * 2000,
        "height": 200,
        "weight": 100,
        "madhab": Madhab.HANAFI,
        "prayer": Prayer.ALWAYS,
        "nikah_timeline": NikahTimeline.SIX_MONTHS,
        "relocation": Relocation.NEGOTIABLE,
        "submitted_at": datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        "flagged": True,
        "flag_reason": "Ж" * 500,
        "moderation_note": "З" * 500,
    }
    user = person(123456, **longest)
    interest = Interest(id=1, from_user_id=123456, to_user_id=2, message="И" * 500)
    rejected = person(123456, **longest, status=ProfileStatus.REJECTED)
    texts = [
        render.profile_card(user.profile, has_guardian=True),
        render.interest_notification(interest, user.profile, has_guardian=True).text,
        render.moderation_card(user.profile).text,
        render.my_profile(rejected.profile),
    ]
    assert max(len(text) for text in texts) <= 4096


def test_adult_step_names_the_spouse_by_gender():
    from app.bot.handlers.profile import _adult_text

    assert "не женаты и ищете супругу для никаха" in _adult_text(Gender.MALE)
    assert "не замужем и ищете супруга для никаха" in _adult_text(Gender.FEMALE)
