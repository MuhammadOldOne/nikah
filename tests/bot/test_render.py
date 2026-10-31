"""Pure rendering of bot texts and outbox notifications (no database, no Telegram)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.bot import render
from app.bot.callbacks import AdminProfileCb, ChatCb
from app.clock import current_year
from app.enums import (
    ConversationCloseReason,
    Education,
    Employment,
    Gender,
    InterestStatus,
    Madhab,
    MaritalStatus,
    MessageKind,
    MessageOrigin,
    NikahTimeline,
    Prayer,
    ProfileStatus,
    Relocation,
    ReportReason,
    ReportStatus,
)
from app.models import Conversation, Interest, Message, Profile, Report, User
from tests.bot.conftest import buttons


def person(user_id: int, gender: Gender = Gender.MALE, **fields: object) -> User:
    """A user with a profile numbered 500 + ``user_id`` («Анкета №503» for user 3)."""
    user = User(id=user_id, telegram_id=1000 + user_id, first_name="<Tg>", username=None)
    values: dict[str, object] = {
        "user_id": user_id,
        "number": 500 + user_id,
        "gender": gender,
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


def test_profile_card_is_the_legacy_card_with_new_fields_and_escapes():
    user = person(5, gender=Gender.FEMALE, about="<script>alert(1)</script>", relocation=Relocation.READY)
    card = render.profile_card(user.profile, has_guardian=True)
    assert card.startswith(
        "<b>Анкета №505</b> — Сестра\nВозраст: 30\nГород: Казань\nНациональность: не указано\n"
        "Семейное положение: Не была замужем\nДети: нет\nОбразование: Высшее\nЗанятость: Работаю\n"
        "Переезд: Готова\n\n<b>О себе:</b>\n&lt;script&gt;alert(1)&lt;/script&gt;\n\n"
        "<b>Пожелания к партнёру:</b>\nИщу &lt;верующую&gt; супругу &amp; хозяйку"
    )
    assert "<script>" not in card
    assert card.endswith(render.GUARDIAN_BADGE)


def test_incomplete_migrated_card_says_not_specified():
    migrated = person(
        6, marital_status=None, children=None, education=None, employment=None, nationality="татарин", about=None
    )
    card = render.profile_card(migrated.profile)
    for line in ("Семейное положение: не указано", "Дети: не указано", "Образование: не указано"):
        assert line in card
    assert "Занятость: не указано" in card and "Национальность: татарин" in card
    assert "<b>О себе:</b>\nне указано" in card
    note = render.my_profile(migrated.profile, ["marital_status", "children", "education", "employment"])
    assert "не заполнено: семейное положение, количество детей, образование, занятость" in note


def test_relayed_message_is_the_legacy_format_with_report_button():
    sender = person(3)
    note = message("Как дела? <b>&</b>", sender, conversation_id=9)
    rendered = render.message_notification(sender, note)
    assert rendered.text == "<b>Анкета №503 пишет:</b>\nКак дела? &lt;b&gt;&amp;&lt;/b&gt;"
    [report] = buttons(rendered.reply_markup)
    assert report.text == "Пожаловаться" and report.callback_data == f"rep:{note.id}"  # the legacy format
    reply, report = buttons(render.message_notification(sender, note, with_reply=True).reply_markup)
    assert ChatCb.unpack(reply.callback_data).conversation_id == 9 and report.callback_data == "rep:1"


def test_chat_history_shows_the_year_of_older_messages():
    """History moved from the legacy bot is years old: «01.04.2025 13:05», this year «01.04 …»."""
    me, partner = person(1), person(2, Gender.FEMALE)
    old = message("Из старого бота", partner)
    old.created_at = datetime(2025, 4, 1, 10, 5, tzinfo=UTC)
    recent = message("Свежее", me)
    recent.created_at = datetime(current_year(), 4, 1, 10, 5, tzinfo=UTC)
    first, second = render.chat_history([old, recent], me, partner)
    assert first.startswith("<b>Анкета №502</b> · <i>01.04.2025 13:05</i>\n")
    assert second.startswith("<b>Вы</b> · <i>01.04 13:05</i>\n")


def test_witness_message_and_wali_copy_are_escaped():
    witness_note = Message(id=4, conversation_id=7, kind=MessageKind.WITNESS, sender_id=None, text="Будьте <вежливы>")
    assert render.witness_message(witness_note).text == "<b>Сообщение от свидетеля:</b>\nБудьте &lt;вежливы&gt;"
    copy = render.guardian_copy(person(4, Gender.FEMALE), person(3), None, witness_note)
    assert copy.text.endswith("<b>Свидетель</b>: Будьте &lt;вежливы&gt;") and copy.reply_markup is None


def test_witness_copy_line_buttons_and_escaping():
    sender, recipient = person(3), person(4, Gender.FEMALE)
    note = message("Мой номер [скрыто свидетелем] <b>", sender, conversation_id=12)
    note.origin = MessageOrigin.TELEGRAM
    rendered = render.witness_copy(12, note, sender, recipient)
    assert rendered.text == "Чат #12 | №503 → №504 (Telegram)\nМой номер [скрыто свидетелем] &lt;b&gt;"
    flag, say, close = buttons(rendered.reply_markup)
    assert (flag.text, flag.callback_data) == ("⚠️ Пометить", "w:flag:1")
    assert (say.text, say.callback_data) == ("✍️ Написать в чат", "w:say:12")
    assert (close.text, close.callback_data) == ("🚫 Закрыть чат", "w:close:12")
    note.origin = MessageOrigin.WEB
    assert render.witness_copy(12, note, sender, None).text.startswith("Чат #12 | №503 → аккаунт удалён (сайт)\n")


def test_guardian_copy_marks_whose_conversation_without_buttons():
    ward, partner = person(4, Gender.FEMALE), person(3)
    rendered = render.guardian_copy(ward, partner, partner, message("<i>Салам</i>", partner))
    assert rendered.text.startswith("👁 Копия переписки: <b>Анкета №504</b> ↔ <b>Анкета №503</b>")
    assert rendered.text.endswith("<b>Анкета №503</b>: &lt;i&gt;Салам&lt;/i&gt;")
    assert rendered.reply_markup is None


def test_interest_notification_is_the_legacy_card_with_the_message():
    sender = person(3)
    interest = Interest(id=11, from_user_id=3, to_user_id=4, message="Хочу <познакомиться>")
    rendered = render.interest_notification(interest, sender.profile, has_guardian=False)
    assert rendered.text.startswith("<b>Вами заинтересовалась анкета №503</b>\n\n<b>Анкета №503</b> — Брат")
    assert "<b>Сообщение:</b>\nХочу &lt;познакомиться&gt;" in rendered.text
    assert rendered.text.endswith("Желаете начать общение через бота под надзором свидетеля?")
    accept, decline = buttons(rendered.reply_markup)
    assert (accept.text, accept.callback_data) == ("Принять", "int:acc:11")  # legacy callback data
    assert (decline.text, decline.callback_data) == ("Отклонить", "int:dec:11")


def test_chat_opened_and_declined_are_the_legacy_texts():
    opened = render.chat_opened(
        person(4, Gender.FEMALE).profile, person(3).profile, 5, several_allowed=False, badge=False
    )
    assert opened.text.startswith("<b>Чат открыт между анкетами №504 и №503.</b>\nВсе сообщения проходят через бота")
    assert opened.text.endswith("Просто пишите сюда текстом — бот передаст второй стороне.")
    [close] = buttons(opened.reply_markup)
    assert close.text == "Завершить общение" and ChatCb.unpack(close.callback_data).conversation_id == 5
    several = render.chat_opened(person(4).profile, person(3).profile, 5, several_allowed=True, badge=True)
    assert [b.text for b in buttons(several.reply_markup)] == ["Написать", "Завершить общение"]
    assert render.GUARDIAN_BADGE in several.text
    declined = render.interest_declined(person(4, Gender.FEMALE))
    assert declined.text == "Анкета №504 отклонила ваш отклик. Не отчаивайтесь, ин ша Аллах."


def test_conversation_closed_text_by_reason():
    partner = person(4, Gender.FEMALE)
    closed = render.conversation_closed(ConversationCloseReason.CLOSED_BY_USER, partner).text
    assert closed == "Анкета №504 завершила общение."
    banned_sister = render.conversation_closed(ConversationCloseReason.USER_BANNED, partner).text
    assert banned_sister == "Чат закрыт: собеседница <b>Анкета №504</b> заблокирована модератором."
    banned_brother = render.conversation_closed(ConversationCloseReason.USER_BANNED, person(5, Gender.MALE))
    assert "собеседник <b>Анкета №505</b> заблокирован модератором" in banned_brother.text
    assert "удалил аккаунт" in render.conversation_closed(ConversationCloseReason.ACCOUNT_DELETED, None).text
    by_witness = render.conversation_closed(ConversationCloseReason.CLOSED_BY_WITNESS, partner).text
    assert by_witness == "Свидетель завершил ваш чат. Если есть вопросы — обратитесь к админу."


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
    assert render.profile_moderated(published.profile).text == (
        "Ваша анкета №508 одобрена. Теперь вы можете смотреть анкеты и отправлять отклики."
    )
    rejected = person(8, status=ProfileStatus.REJECTED, moderation_note="Уберите <ссылки>")
    text = render.profile_moderated(rejected.profile).text
    assert text.startswith("К сожалению, ваша анкета №508 отклонена.\nПричина: Уберите &lt;ссылки&gt;")


def test_report_card_escapes_comment_and_message():
    reporter, target = person(4, Gender.FEMALE), person(3)
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
    target = person(4, Gender.FEMALE)
    interest = Interest(id=1, from_user_id=3, to_user_id=4, status=InterestStatus.PENDING)
    interest.to_user = target
    assert "<b>Анкета №504</b> — ожидает ответа" in render.outgoing_interests([interest])
    chunks = render.split_blocks(["a" * 3000, "b" * 3000, "c"], limit=4000)
    assert chunks == ["a" * 3000, "b" * 3000 + "\n\nc"]


def test_longest_cards_fit_in_one_telegram_message():
    longest = {
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
    from app.bot.handlers.profile import adult_text

    assert "не женаты и ищете супругу для никаха" in adult_text(Gender.MALE)
    assert "не замужем и ищете супруга для никаха" in adult_text(Gender.FEMALE)


def test_witness_notices_name_profiles_by_number_only():
    a, b = person(3), person(4, Gender.FEMALE)
    conversation = Conversation(id=12, user_a_id=3, user_b_id=4)
    assert render.witness_conversation_opened(conversation, a, b).text == "Открыт чат #12: №503 ↔ №504."
    closed = render.witness_conversation_closed(conversation, ConversationCloseReason.CLOSED_BY_USER, a, None)
    assert closed.text == "Чат #12 закрыт: общение завершено (№503 ↔ аккаунт удалён)."
    report = Report(id=2, reporter_id=4, target_user_id=3, reason=ReportReason.OTHER, comment=None)
    report.reporter, report.target_user = b, a
    report.message = message("Переведи <деньги>", a, conversation_id=12)
    text = render.witness_report(report).text
    assert text == "⚠️ Жалоба на сообщение #1 в чате #12\nОт: №504, на: №503\n«Переведи &lt;деньги&gt;»"
    assert "1004" not in text and "<Tg>" not in text  # no Telegram ids or names
