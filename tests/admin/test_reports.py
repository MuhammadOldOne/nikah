"""Reports: list and resolve (dismiss / hide profile / ban user)."""

from __future__ import annotations

from sqlalchemy import select

from app.enums import Gender, MessageOrigin, ProfileStatus, ReportAction, ReportReason, ReportStatus
from app.models import Profile, Report, User
from app.services import chat, moderation, profiles
from app.services.moderation import HIDE_PROFILE_NOTE


async def _report(session, reporter: User, target: User, **kwargs) -> Report:
    report = await moderation.create_report(session, reporter, target.id, ReportReason.FRAUD, **kwargs)
    await session.commit()
    return report


async def _fresh(session, model, key):
    return await session.get(model, key, populate_existing=True)


async def test_open_reports_page(admin_client, session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    message_text = "Переведи деньги, верну <завтра>"
    message = await chat.send_message(session, conversation.id, man, message_text, MessageOrigin.WEB)
    report = await _report(session, woman, man, comment="Просит денег", message_id=message.id)

    response = await admin_client.get("/admin/reports")
    assert response.status_code == 200
    text = response.text
    assert f"Жалоба #{report.id}" in text and "мошенничество" in text
    assert "Просит денег" in text
    assert "<mark>переведи деньги</mark>, верну &lt;завтра&gt;" in text.lower()
    # A report on a specific message: its excerpt, why it is flagged and a link into the chat.
    assert "Жалоба на сообщение" in text and f'href="/admin/chats/{conversation.id}#m{message.id}"' in text
    assert ">жалоба<" in text and "стоп-слова" in text
    assert f"/admin/users/{man.id}" in text and f"/admin/users/{woman.id}" in text
    assert str(man.telegram_id) not in text and str(woman.telegram_id) not in text
    for action in ReportAction:
        assert f'value="{action.value}"' in text
    assert "Рассмотренных жалоб пока нет" in (await admin_client.get("/admin/reports", status="resolved")).text


async def test_dismiss(admin_client, session, make_pair):
    man, woman = await make_pair()
    report = await _report(session, woman, man)
    response = await admin_client.post(f"/admin/reports/{report.id}/resolve", action="dismiss")
    assert response.status_code == 303 and response.headers["location"] == "/admin/reports"
    report = await _fresh(session, Report, report.id)
    assert report.status == ReportStatus.RESOLVED and report.resolution == ReportAction.DISMISS
    assert report.resolved_by_id == admin_client.user.id
    assert (await _fresh(session, Profile, man.id)).status == ProfileStatus.ACTIVE
    resolved = await admin_client.get("/admin/reports", status="resolved")
    assert "жалоба отклонена" in resolved.text
    assert f'<a href="/admin/users/{admin_client.user.id}">модератор</a>' in resolved.text


async def test_hide_profile(admin_client, session, make_pair):
    man, woman = await make_pair()
    report = await _report(session, woman, man)
    await admin_client.post(f"/admin/reports/{report.id}/resolve", action="hide_profile")
    profile = await _fresh(session, Profile, man.id)
    assert profile.status == ProfileStatus.REJECTED and profile.moderation_note == HIDE_PROFILE_NOTE
    assert (await _fresh(session, Report, report.id)).resolution == ReportAction.HIDE_PROFILE


async def test_ban_user_hides_profile_from_catalog(admin_client, session, make_pair, make_profile):
    man, woman = await make_pair()
    viewer = (await make_profile()).user  # another man: sees women in the catalog
    second_reporter = (await make_profile()).user
    report = await _report(session, man, woman)
    other_report = await _report(session, second_reporter, woman)
    assert woman.id in [p.user_id for p in (await profiles.list_catalog(session, viewer))[0]]

    await admin_client.post(f"/admin/reports/{report.id}/resolve", action="ban_user")

    banned = await _fresh(session, User, woman.id)
    assert banned.is_banned
    assert (await profiles.list_catalog(session, viewer))[0] == []
    statuses = await session.scalars(
        select(Report.resolution)
        .where(Report.id.in_([report.id, other_report.id]))
        .execution_options(populate_existing=True)
    )
    assert set(statuses.all()) == {ReportAction.BAN_USER}


async def test_admin_cannot_be_banned_through_a_report(admin_client, session, make_profile, admin):
    reporter = (await make_profile()).user
    await make_profile(admin, gender=Gender.FEMALE)  # reports need a profile the reporter has seen
    report = await _report(session, reporter, admin)
    page = await admin_client.get("/admin/reports")
    assert 'value="ban_user"' not in page.text
    await admin_client.post(f"/admin/reports/{report.id}/resolve", action="ban_user")
    assert not (await _fresh(session, User, admin.id)).is_banned
    assert (await _fresh(session, Report, report.id)).status == ReportStatus.OPEN
    assert "Администратора нельзя заблокировать" in (await admin_client.get("/admin/reports")).text


async def test_resolving_twice(admin_client, session, make_pair):
    man, woman = await make_pair()
    report = await _report(session, woman, man)
    await admin_client.post(f"/admin/reports/{report.id}/resolve", action="dismiss")
    await admin_client.post(f"/admin/reports/{report.id}/resolve", action="hide_profile")
    assert (await _fresh(session, Report, report.id)).resolution == ReportAction.DISMISS
    assert "Жалоба уже рассмотрена." in (await admin_client.get("/admin/reports")).text


async def test_invalid_action_is_rejected(admin_client, session, make_pair):
    man, woman = await make_pair()
    report = await _report(session, woman, man)
    response = await admin_client.post(f"/admin/reports/{report.id}/resolve", action="delete_everything")
    assert response.status_code == 400
    assert (await _fresh(session, Report, report.id)).status == ReportStatus.OPEN


async def test_resolve_requires_csrf(admin_client, session, make_pair):
    man, woman = await make_pair()
    report = await _report(session, woman, man)
    response = await admin_client.http.post(f"/admin/reports/{report.id}/resolve", data={"action": "ban_user"})
    assert response.status_code == 403
    assert not (await _fresh(session, User, man.id)).is_banned
