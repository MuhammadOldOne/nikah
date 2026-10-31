"""Chats as the witness sees them: the list of open/closed conversations and one chat's history."""

from __future__ import annotations

from app.enums import MessageOrigin, ReportReason
from app.services import chat, moderation, witness


async def test_open_and_closed_chats(
    admin_client, session, make_pair, make_profile, make_conversation, override_settings
):
    override_settings(MAX_OPEN_CONVERSATIONS=2)
    man, woman = await make_pair()
    other = (await make_profile(number=640)).user
    open_one = await make_conversation(man, woman)
    closed_one = await make_conversation(other, woman)
    await chat.send_message(session, open_one.id, man, "Ассаляму алейкум", MessageOrigin.WEB)
    await chat.send_message(session, open_one.id, woman, "Мой номер 8 999 123 45 67", MessageOrigin.TELEGRAM)
    await chat.close_conversation(session, closed_one, other)
    await session.commit()

    page = await admin_client.get("/admin/chats")
    assert page.status_code == 200
    assert f'href="/admin/chats/{open_one.id}"' in page.text and f'href="/admin/chats/{closed_one.id}"' not in page.text
    assert "Анкета №500" in page.text and "Анкета №501" in page.text
    assert 'title="помеченных">1<' in page.text  # one message hidden by the contact filter
    assert str(man.telegram_id) not in page.text and "Тест" not in page.text  # numbers, not names

    closed = await admin_client.get("/admin/chats", status="closed")
    assert f'href="/admin/chats/{closed_one.id}"' in closed.text and "Анкета №640" in closed.text
    assert "общение завершено" in closed.text  # label(close_reason)
    assert (await admin_client.get("/admin/chats", status="other")).status_code == 400


async def test_chat_history_with_witness_messages_and_flags(admin_client, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    plain = await chat.send_message(session, conversation.id, man, "Ассаляму алейкум", MessageOrigin.WEB)
    leak = await chat.send_message(session, conversation.id, woman, "пишите t.me/someone", MessageOrigin.TELEGRAM)
    scam = await chat.send_message(session, conversation.id, man, "Переведи деньги на карту", MessageOrigin.WEB)
    await witness.witness_flag(session, admin_client.user, plain.id)
    await moderation.create_report(session, woman, man.id, ReportReason.FRAUD, message_id=scam.id)
    said = await witness.witness_say(session, admin_client.user, conversation.id, "Напоминаю о правилах общения")
    await session.commit()

    page = await admin_client.get(f"/admin/chats/{conversation.id}")
    assert page.status_code == 200
    text = page.text
    assert f'id="m{plain.id}"' in text and f'id="m{leak.id}"' in text and f'id="m{said.id}"' in text
    assert "[скрыто свидетелем]" in text and "t.me/someone" not in text
    assert "фильтр контактов" in text and "свидетель</span>" in text and "жалоба</span>" in text
    assert "<mark>переведи деньги</mark>" in text.lower()
    assert "adm-msg--witness" in text and "Напоминаю о правилах общения" in text
    assert "Анкета №500" in text and str(woman.telegram_id) not in text
    assert (await admin_client.get("/admin/chats/999999")).status_code == 404


async def test_chat_history_pages(admin_client, session, make_pair, make_conversation, override_settings):
    from app.web.routes import admin as admin_routes

    override_settings(CHAT_RATE_LIMIT_PER_MIN=1000)
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    ids = [
        (await chat.send_message(session, conversation.id, man, f"Сообщение {index}", MessageOrigin.WEB)).id
        for index in range(admin_routes.CHAT_PAGE_SIZE + 5)
    ]
    await session.commit()

    latest = await admin_client.get(f"/admin/chats/{conversation.id}")
    first_shown = ids[5]
    assert f'id="m{ids[-1]}"' in latest.text and f'id="m{ids[4]}"' not in latest.text
    assert f"?before={first_shown}" in latest.text
    earlier = await admin_client.get(f"/admin/chats/{conversation.id}", before=first_shown)
    assert f'id="m{ids[0]}"' in earlier.text and f'id="m{first_shown}"' not in earlier.text
    assert "?before=" not in earlier.text and "Последние сообщения" in earlier.text
