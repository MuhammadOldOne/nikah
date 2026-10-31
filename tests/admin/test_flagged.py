"""Stop-word flags: highlighting and the flagged lists."""

from __future__ import annotations

from markupsafe import Markup

from app.enums import Gender, MessageOrigin
from app.services import chat, interests
from app.web.routes.admin import excerpt, highlight_stop_words


def test_highlight_escapes_and_marks():
    result = highlight_stop_words("<b>Переведи</b> на карту, пожалуйста")
    assert isinstance(result, Markup)
    assert result == "&lt;b&gt;<mark>Переведи</mark>&lt;/b&gt; <mark>на карту</mark>, пожалуйста"


def test_highlight_handles_yo_and_case():
    assert highlight_stop_words("ПЕРЕВЁДИ ДЕНЬГИ") == "<mark>ПЕРЕВЁДИ ДЕНЬГИ</mark>"


def test_highlight_without_stop_words():
    assert highlight_stop_words("Ассаляму алейкум & добро пожаловать") == "Ассаляму алейкум &amp; добро пожаловать"
    assert highlight_stop_words(None) == ""


def test_excerpt():
    assert excerpt("коротко") == "коротко"
    assert excerpt("а" * 400, 10) == "а" * 9 + "…"


async def test_flagged_messages_page(admin_client, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, man, "Обычное сообщение", MessageOrigin.WEB)
    await chat.send_message(session, conversation.id, man, "Есть тема с криптой, вложи", MessageOrigin.TELEGRAM)
    await session.commit()
    response = await admin_client.get("/admin/flagged")
    assert response.status_code == 200
    assert "<mark>криптой</mark>" in response.text
    assert "Обычное сообщение" not in response.text
    assert f"/admin/users/{man.id}" in response.text and str(man.telegram_id) not in response.text


async def test_flagged_interests_and_profiles(admin_client, session, make_pair, make_profile):
    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id, "Давайте вместе в инвестиции")
    await make_profile(gender=Gender.FEMALE, flagged=True, flag_reason="ставки", about="Люблю ставки на спорт")
    await session.commit()
    interests_page = await admin_client.get("/admin/flagged", kind="interests")
    assert "<mark>инвестиции</mark>" in interests_page.text
    profiles_page = await admin_client.get("/admin/flagged", kind="profiles")
    assert "<mark>ставки</mark>" in profiles_page.text
    assert (await admin_client.get("/admin/flagged", kind="secrets")).status_code == 400
