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


async def test_published_flagged_profile_can_be_taken_down(admin_client, session, make_profile, override_settings):
    import re

    from app.enums import ProfileStatus
    from app.services import profiles

    override_settings(PREMODERATION=False)  # published at once: it never enters the moderation queue
    profile = await make_profile(gender=Gender.FEMALE, about="Пассивный доход, инвестиции в крипту")
    profile.flagged, profile.flag_reason = True, "инвестиции, крипту"
    await session.commit()

    for page in ("/admin/flagged?kind=profiles", f"/admin/users/{profile.user_id}"):
        html = (await admin_client.get(page)).text
        assert f'action="/admin/moderation/{profile.user_id}/reject"' in html and "Снять с публикации" in html, page
    version = re.search(r'name="version" value="([^"]*)"', html).group(1)

    response = await admin_client.post(
        f"/admin/moderation/{profile.user_id}/reject", version=version, preset="1", next="/admin/flagged?kind=profiles"
    )

    assert response.headers["location"] == "/admin/flagged?kind=profiles"
    refreshed = await profiles.get_profile(session, profile.user_id)
    await session.refresh(refreshed)
    assert refreshed.status == ProfileStatus.REJECTED
    assert "Снять с публикации" not in (await admin_client.get(f"/admin/users/{profile.user_id}")).text


async def test_flagged_messages_show_why_and_who(admin_client, session, make_pair, make_conversation):
    from app.enums import ReportReason
    from app.services import moderation, witness

    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    leak = await chat.send_message(session, conversation.id, woman, "Звоните +7 999 123-45-67", MessageOrigin.TELEGRAM)
    marked = await chat.send_message(session, conversation.id, man, "Обычное сообщение", MessageOrigin.WEB)
    reported = await chat.send_message(session, conversation.id, man, "Грубость", MessageOrigin.WEB)
    await witness.witness_flag(session, admin_client.user, marked.id)
    await moderation.create_report(session, woman, man.id, ReportReason.INSULT, message_id=reported.id)
    await session.commit()

    text = (await admin_client.get("/admin/flagged")).text
    for message in (leak, marked, reported):
        assert f'href="/admin/chats/{conversation.id}#m{message.id}"' in text
    assert "фильтр контактов" in text and ">свидетель<" in text and ">жалоба<" in text
    assert "Анкета №501</a> → <a" in text  # the sister wrote to the brother
    assert "999 123" not in text and str(woman.telegram_id) not in text


async def test_witness_messages_tab(admin_client, session, make_pair, make_conversation):
    from app.services import witness

    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await witness.witness_say(session, admin_client.user, conversation.id, "Свидетель напоминает о правилах")
    await chat.send_message(session, conversation.id, man, "Есть тема с криптой", MessageOrigin.WEB)
    await session.commit()

    page = await admin_client.get("/admin/flagged", kind="witness")
    assert "Свидетель напоминает о правилах" in page.text and "криптой" not in page.text
    assert "Анкета №500" in page.text and "Анкета №501" in page.text
    assert "Свидетель пока ничего не писал" in (await admin_client.get("/admin/flagged", kind="witness", page=2)).text


async def test_legacy_age_flag_is_labelled(admin_client, make_profile):
    from app.enums import ProfileStatus
    from app.services.moderation import LEGACY_AGE_FLAG_REASON

    await make_profile(
        status=ProfileStatus.PENDING, flagged=True, flag_reason=LEGACY_AGE_FLAG_REASON, marital_status=None
    )
    page = await admin_client.get("/admin/flagged", kind="profiles")
    assert "возраст при переносе" in page.text and "<mark>legacy age</mark>" not in page.text
    queue = await admin_client.get("/admin/moderation")
    assert "возраст при переносе" in queue.text and "не указано" in queue.text  # empty migrated field
