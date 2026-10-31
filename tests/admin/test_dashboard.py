"""Dashboard numbers."""

from __future__ import annotations

from app.enums import Gender, InterestStatus, MessageOrigin, ProfileStatus, ReportReason
from app.services import chat, interests, moderation
from app.web.routes.admin import dashboard_stats


async def test_dashboard_stats(session, make_pair, make_profile, make_user, make_conversation):
    man, woman = await make_pair()
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    await make_profile(gender=Gender.MALE, status=ProfileStatus.REJECTED)
    await make_user(is_banned=True)
    other = await make_profile(gender=Gender.FEMALE)
    await interests.send_interest(session, man, other.user_id)
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, man, "Ассаляму алейкум", MessageOrigin.WEB)
    await chat.send_message(session, conversation.id, woman, "Переведи деньги на карту", MessageOrigin.TELEGRAM)
    await moderation.create_report(session, woman, man.id, ReportReason.FRAUD)
    await session.commit()

    stats = await dashboard_stats(session)

    assert stats.users_total == 6 and stats.users_banned == 1
    assert stats.profiles[ProfileStatus.ACTIVE] == {Gender.MALE: 1, Gender.FEMALE: 2}
    assert stats.profiles[ProfileStatus.PENDING][Gender.FEMALE] == 1
    assert stats.profiles[ProfileStatus.REJECTED][Gender.MALE] == 1
    assert stats.profiles_total == 5 and stats.profiles_by_gender(Gender.FEMALE) == 3
    assert stats.interests[InterestStatus.PENDING] == 1 and stats.interests[InterestStatus.ACCEPTED] == 1
    assert stats.open_conversations == 1
    assert stats.messages_24h == 2
    assert stats.open_reports == 1
    assert stats.flagged_messages == 1


async def test_dashboard_page(admin_client, make_profile):
    await make_profile(status=ProfileStatus.PENDING)
    response = await admin_client.get("/admin")
    assert response.status_code == 200
    assert "Сводка" in response.text and "анкет на модерации" in response.text
    assert 'adm-count">1<' in response.text  # pending badge in the navigation


async def test_dashboard_shows_legacy_import_status(
    admin_client, session, make_user, make_profile, make_pair, make_conversation
):
    from app.services import meta

    page = await admin_client.get("/admin")
    assert "Перенос из старого бота" in page.text and "Не выполнялся" in page.text

    man, woman = await make_pair()
    await make_conversation(man, woman)
    await make_user(consent=False)
    await make_profile(status=ProfileStatus.ACTIVE, marital_status=None, children=None)
    await meta.mark_legacy_import(session, {"users": 4, "profiles": 3, "conversations": 1, "messages": 0})
    await session.commit()

    text = (await admin_client.get("/admin")).text
    assert "Выполнен" in text and "Не выполнялся" not in text

    def row(label: str, table: str, imported: int, now: int) -> str:
        return (
            f'<th scope="row">{label} <span class="adm-muted">{table}</span></th>'
            f'<td class="num">{imported}</td><td class="num">{now}</td>'
        )

    assert row("Анкеты", "profiles", 3, 3) in text
    assert row("Пользователи", "users", 4, 5) in text  # + the admin
    assert row("Чаты", "conversations", 1, 1) in text
    assert "согласие на обработку данных: <b>1</b>" in text and "Неполных анкет" in text
    order = [
        text.index(f'<span class="adm-muted">{table}</span>')
        for table in ("users", "profiles", "conversations", "messages")
    ]
    assert order == sorted(order)  # the report's order, not JSONB's
    assert 'href="/admin/chats"' in text
