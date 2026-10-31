"""Contact filter in chats (both channels): phones, links, t.me/…, @usernames and e-mails are hidden
by «[скрыто свидетелем]», ordinary numbers stay; flags, notice and the per-minute limit."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.enums import Gender, MessageOrigin
from app.models import Outbox
from app.schemas import ProfileUpdate
from app.services import chat, interests
from app.services.errors import InvalidInput, MessageRateLimited
from app.text import CONTACT_PLACEHOLDER, find_contacts, hide_contacts

CONTACTS = [
    "+7 912 345-67-89",
    "89123456789",
    "8 (912) 345 67 89",
    "8-912-345-67-89",
    "+7(912)3456789",
    "8 9 1 2 3 4 5 6 7 8 9",
    "8.912.345.67.89",
    "８９１２３４５６７８９",  # fullwidth digits
    "٨٩١٢٣٤٥٦٧٨٩",  # Arabic-Indic digits
    "①②③④⑤⑥⑦⑧⑨⓪",
    "123-45-67",
    "https://example.com/x",
    "www.site.ru",
    "ＷＷＷ．ＳＩＴＥ．ＲＵ",
    "tg://resolve?domain=ahmad",
    "t.me/username",
    "t . me / username",
    "т.ме/username",
    "Telegram.me/abc",
    "wa.me/79123456789",
    "vk.com/id1",
    "instagram.com/user",
    "example.ru",
    "x.com/abc",
    "мойсайт.рф",
    "mysite точка ру",
    "mysite dot com",
    "mysite[.]ru",
    "mysite (.) ru",
    "site . ru",
    "@username",
    "＠username",
    "@ ahmad_95",
    "@ivan.petrov",
    "ivan@mail.ru",
    "ivan собака mail точка ru",
    "ivan (at) gmail (dot) com",
    "123456789012",
    # Latin look-alikes, invisible characters and keycaps inside a contact, link-in-bio hosts.
    "@ivаnov_99",  # Cyrillic «а»
    "@сосо",  # Cyrillic look-alikes only
    "@iv\u034fanov_99",  # combining grapheme joiner
    "t.mе/ivanov",  # Cyrillic «е»
    "8\u3164912\u3164345\u316467\u316489",  # Hangul filler
    "8\u2800912\u2800345\u28006789",  # braille blank
    "8\ufe00912\ufe00345\ufe006789",  # variation selector
    "8️⃣9️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣",  # keycaps
    "@ivánov_99",
    "linktr.ee/ivanov",
    "goo.gl/abc",
    "is.gd/xyz",
    "beacons.ai/ivanov",
    "lnk.bio/ivanov",
]
# An @username glued to the word before it (the legacy filter caught these too).
GLUED = ["тг@ivanov_99", "ник@ivanov_99", "inst@ivanov_99", "telegram@ivanov99", "пиши@ivanov", "a@ivanov_99"]
ORDINARY = [
    "мне 25 лет",
    "в 2024 году",
    "рост 175, вес 70",
    "встретимся в 18:30",
    "в 9:00-18:00 работаю",
    "цена 15 000 рублей",
    "1 500 000 руб",
    "100.000.000 сум",
    "12.05.2024",
    "15/06/2024",
    "с 12.05.2024 по 15.06.2024",
    "12.05.2024-15.06.2024",
    "суббота 14.11, в 15:00",
    "в 2020-2021 гг",
    "1990-1995 г.р.",
    "Мой рост 1.75 м",
    "т.е. всё, и т.д.",
    "г. Казань, ул. Ленина 5, кв. 34",
    "e-mail не дам",
    "Привет.Как дела?",
    "Хорошо.Нет проблем",
    "P.S. me too",
    "Мне 25.Я из Казани",
    "Анкета №512 пишет",
    "#512",
    "Я @ работе",
    "Скидка @ 100",
    "у меня собака и кошка",
    "Точка зрения у меня такая",
    "100% согласна",
    "версия 1.2.3",
    "Ин ша Аллах, 2-3 раза в неделю",
    "Я из Казани.Со мной всё хорошо",
    "Хорошо.То есть да",
    "и/или, км/ч",
    "Привет 👨‍👩‍👧 ❤️ 1️⃣ раз",
]


@pytest.mark.parametrize("contact", CONTACTS)
def test_contacts_are_hidden(contact: str):
    text, hidden = hide_contacts(f"Пишите: {contact} после обеда")
    assert hidden and text == f"Пишите: {CONTACT_PLACEHOLDER} после обеда"
    assert find_contacts(contact)


@pytest.mark.parametrize("contact", GLUED)
def test_username_glued_to_a_word_is_hidden(contact: str):
    word, _, handle = contact.partition("@")
    assert hide_contacts(f"мой {contact}!") == (f"мой {word}{CONTACT_PLACEHOLDER}!", True)
    assert find_contacts(contact) == [f"@{handle}"]


def test_email_stays_one_placeholder():
    assert hide_contacts("почта ivan@mail.ru, звоните") == (f"почта {CONTACT_PLACEHOLDER}, звоните", True)
    assert hide_contacts("ivan.petrov@gmail.com") == (CONTACT_PLACEHOLDER, True)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_left_alone(text: str):
    assert hide_contacts(text) == (text, False)
    assert find_contacts(text) == []


def test_several_contacts_and_long_input():
    text, hidden = hide_contacts("Звони 89123456789 или пиши @ahmad_k, сайт ahmad.ru")
    assert hidden and text == f"Звони {CONTACT_PLACEHOLDER} или пиши {CONTACT_PLACEHOLDER}, сайт {CONTACT_PLACEHOLDER}"
    for evil in ("a." * 1000, "1 " * 1000, "a@" * 1000, "-1" * 1000):  # stays fast (no backtracking blow-up)
        hide_contacts(evil)
    # Contacts separated only by spaces or punctuation become one placeholder.
    assert hide_contacts("пишите: @ahmad_k, @ahmad_k2; 89123456789!") == (f"пишите: {CONTACT_PLACEHOLDER}!", True)


async def test_send_message_hides_contacts_flags_and_tells_the_sender(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    for origin in (MessageOrigin.WEB, MessageOrigin.TELEGRAM):
        message = await chat.send_message(session, conversation.id, man, "Мой номер +7 912 345-67-89", origin)
        assert message.text == f"Мой номер {CONTACT_PLACEHOLDER}"
        assert message.flagged and message.flag_reason == "contact-leak filter" and message.contacts_hidden
    assert chat.CONTACTS_HIDDEN_NOTICE == "Часть текста была скрыта фильтром (телефоны/ссылки/@). Свидетель уведомлён."

    plain = await chat.send_message(session, conversation.id, woman, "Мне 25 лет, рост 165", MessageOrigin.WEB)
    assert (plain.text, plain.flagged, plain.contacts_hidden) == ("Мне 25 лет, рост 165", False, False)
    scam = await chat.send_message(session, conversation.id, woman, "Переведи деньги, пожалуйста", MessageOrigin.WEB)
    assert scam.flagged and not scam.contacts_hidden and scam.text == "Переведи деньги, пожалуйста"
    both = await chat.send_message(session, conversation.id, man, "Скинь на карту, пиши @ahmad_k", MessageOrigin.WEB)
    assert both.contacts_hidden and both.flag_reason.startswith("contact-leak filter, ")
    with pytest.raises(InvalidInput, match="много ссылок или номеров"):  # stays within the 2000 characters
        await chat.send_message(session, conversation.id, man, "@abc и " * 285, MessageOrigin.WEB)


async def test_glued_and_disguised_contacts_in_chats_interests_and_profiles(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    for text in ("мой тг@ivan_1990", "пишите @ivаnov_99", "8\u3164912\u3164345\u316467\u316489", "linktr.ee/ivan"):
        message = await chat.send_message(session, conversation.id, man, text, MessageOrigin.TELEGRAM)
        assert message.contacts_hidden and message.flag_reason.startswith("contact-leak filter"), text
    with pytest.raises(InvalidInput):
        await interests.send_interest(session, woman, man.id, "пишите мне inst@ivan_ov")
    with pytest.raises(ValidationError):
        ProfileUpdate(about="Мой инстаграм inst@ivan_ov")


async def test_rate_limit_counts_both_channels(session, make_profile, make_conversation, override_settings):
    override_settings(CHAT_RATE_LIMIT_PER_MIN=3, MAX_OPEN_CONVERSATIONS=2)
    man = (await make_profile(gender=Gender.MALE)).user
    first = await make_conversation(man, (await make_profile(gender=Gender.FEMALE)).user)
    second = await make_conversation(man, (await make_profile(gender=Gender.FEMALE)).user)
    await chat.send_message(session, first.id, man, "1", MessageOrigin.WEB)
    await chat.send_message(session, second.id, man, "2", MessageOrigin.TELEGRAM)
    await chat.send_message(session, first.id, man, "3", MessageOrigin.TELEGRAM)
    with pytest.raises(MessageRateLimited):
        await chat.send_message(session, second.id, man, "4", MessageOrigin.WEB)
    assert get_settings().CHAT_RATE_LIMIT_PER_MIN == 3
    copies = (await session.scalars(select(Outbox).where(Outbox.kind == "witness.copy"))).all()
    assert len(copies) == 3
