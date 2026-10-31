"""Moderation callbacks: admin rights are checked on every action."""

from __future__ import annotations

from app.bot import keyboards, render
from app.bot.callbacks import AdminProfileAction, AdminProfileCb, AdminReportAction, AdminReportCb
from app.enums import Gender, ProfileStatus, ReportReason, ReportStatus
from app.services import moderation, profiles
from app.services.errors import PermissionDenied
from tests.bot.conftest import buttons


def _profile_cb(profile, action: AdminProfileAction, **extra) -> str:
    return AdminProfileCb(
        action=action, user_id=profile.user_id, version=render.profile_version(profile), **extra
    ).pack()


async def test_non_admin_cannot_approve_reject_or_resolve(harness, session, make_profile, make_pair, make_user):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    man, woman = await make_pair()
    report = await moderation.create_report(session, woman, man.id, ReportReason.FAKE)
    await session.commit()
    intruder = await make_user()

    for data in (
        _profile_cb(pending, AdminProfileAction.APPROVE),
        _profile_cb(pending, AdminProfileAction.PRESET, preset=0),
        AdminReportCb(action=AdminReportAction.BAN_CONFIRM, report_id=report.id).pack(),
    ):
        await harness.press(intruder, data)

    assert [answer.text for answer in harness.callback_answers()] == [PermissionDenied().message] * 3
    await session.refresh(pending)
    await session.refresh(report)
    await session.refresh(man)
    assert pending.status == ProfileStatus.PENDING
    assert report.status == ReportStatus.OPEN and not man.is_banned


async def test_admin_approves_profile(harness, session, make_profile, make_user):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    admin = await make_user(is_admin=True)

    await harness.press(admin, _profile_cb(pending, AdminProfileAction.APPROVE), message_text="Анкета")

    await session.refresh(pending)
    assert pending.status == ProfileStatus.ACTIVE
    assert harness.edited_texts()[-1].endswith("✅ <b>Одобрено</b>")


async def test_admin_from_settings_rejects_with_preset(harness, session, make_profile, make_user, override_settings):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    admin = await make_user()
    override_settings(ADMIN_TELEGRAM_IDS=[admin.telegram_id])

    await harness.press(admin, _profile_cb(pending, AdminProfileAction.PRESET, preset=2))

    await session.refresh(pending)
    assert pending.status == ProfileStatus.REJECTED
    assert pending.moderation_note.startswith("Анкета похожа на ненастоящую")


async def test_admin_rejects_with_custom_reason(harness, session, make_profile, make_user):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    admin = await make_user(is_admin=True)

    await harness.press(admin, _profile_cb(pending, AdminProfileAction.OTHER))
    await harness.text(admin, "Уберите, пожалуйста, ссылку из раздела «О себе».")

    await session.refresh(pending)
    assert pending.status == ProfileStatus.REJECTED
    assert pending.moderation_note == "Уберите, пожалуйста, ссылку из раздела «О себе»."


async def test_stale_card_cannot_approve_edited_profile(harness, session, make_profile, make_user):
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    admin = await make_user(is_admin=True)
    stale = AdminProfileCb(
        action=AdminProfileAction.APPROVE, user_id=pending.user_id, version=render.profile_version(pending) - 60
    ).pack()

    await harness.press(admin, stale)

    await session.refresh(pending)
    assert pending.status == ProfileStatus.PENDING
    assert "уже рассмотрена или изменена" in harness.callback_answers()[-1].text


async def test_admin_bans_after_confirmation(harness, session, make_pair, make_user):
    man, woman = await make_pair()
    report = await moderation.create_report(session, woman, man.id, ReportReason.FRAUD)
    await session.commit()
    admin = await make_user(is_admin=True)

    await harness.press(admin, AdminReportCb(action=AdminReportAction.BAN, report_id=report.id).pack())
    await session.refresh(man)
    assert not man.is_banned  # asks for confirmation first

    await harness.press(admin, AdminReportCb(action=AdminReportAction.BAN_CONFIRM, report_id=report.id).pack())
    await session.refresh(man)
    await session.refresh(report)
    assert man.is_banned and report.status == ReportStatus.RESOLVED


async def test_admin_cannot_ban_another_admin_from_the_bot(harness, session, make_profile, make_user):
    woman = (await make_profile(gender=Gender.FEMALE)).user
    moderator = (await make_profile(await make_user(is_admin=True), gender=Gender.MALE)).user
    report = await moderation.create_report(session, woman, moderator.id, ReportReason.FRAUD)
    await session.commit()
    admin = await make_user(is_admin=True)

    await harness.press(admin, AdminReportCb(action=AdminReportAction.BAN_CONFIRM, report_id=report.id).pack())

    assert "Администратора нельзя заблокировать" in harness.callback_answers()[-1].text
    await session.refresh(moderator)
    await session.refresh(report)
    assert not moderator.is_banned and report.status == ReportStatus.OPEN
    restored = harness.api.requests[-1]  # the report buttons come back instead of disappearing
    assert type(restored).__name__ == "EditMessageReplyMarkup"
    assert restored.reply_markup == keyboards.admin_report(report.id)


async def test_admin_hides_reported_profile(harness, session, make_pair, make_user):
    man, woman = await make_pair()
    report = await moderation.create_report(session, woman, man.id, ReportReason.MARRIED)
    await session.commit()
    admin = await make_user(is_admin=True)

    await harness.press(admin, AdminReportCb(action=AdminReportAction.HIDE_PROFILE, report_id=report.id).pack())

    profile = await profiles.get_profile(session, man.id)
    await session.refresh(profile)
    assert profile.status == ProfileStatus.REJECTED


async def test_admin_command_sends_the_moderation_queue(harness, make_profile, make_user):
    admin = await make_user(is_admin=True)
    await harness.text(admin, "/admin")
    assert harness.last_text(admin) == "Очередь модерации пуста."

    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    harness.clear()
    await harness.text(admin, "/admin")
    head, *cards = harness.texts(admin)
    assert head == "Анкет в очереди: 2. Высылаю..."
    assert [card.split("\n\n")[1].split("\n")[0] for card in cards] == [
        "<b>Анкета №500</b> — Сестра",
        "<b>Анкета №501</b> — Брат",
    ]
    assert [button.text for button in buttons(harness.last_markup(admin))] == ["Одобрить", "Отклонить"]


async def test_non_admin_admin_commands_are_refused(harness, session, make_pair, make_user):
    man, _ = await make_pair()
    intruder = await make_user()
    for command in ("/admin", "/ban №500", "/unban №500"):
        await harness.text(intruder, command)
        assert harness.last_text(intruder) == PermissionDenied().message
    await session.refresh(man)
    assert not man.is_banned


async def test_ban_and_unban_by_profile_number_or_telegram_id(harness, session, make_pair, make_user):
    man, woman = await make_pair()
    admin = await make_user(is_admin=True)

    await harness.text(admin, "/ban №500")
    await session.refresh(man)
    assert man.is_banned and harness.last_text(admin) == "Пользователь №500 (Анкета №500) заблокирован."
    await harness.text(admin, "/unban #500")
    await session.refresh(man)
    assert not man.is_banned and harness.last_text(admin) == "Пользователь #500 (Анкета №500) разблокирован."
    await harness.text(admin, f"/ban {woman.telegram_id}")  # the legacy form: a Telegram id
    await session.refresh(woman)
    assert woman.is_banned

    await harness.text(admin, "/ban")
    assert harness.last_text(admin) == "Использование: /ban <Telegram ID или №анкеты>"
    await harness.text(admin, "/ban Ахмад")
    assert harness.last_text(admin) == "Укажите Telegram ID (только цифры) или номер анкеты: №512 или #512."
    await harness.text(admin, "/ban №999")
    assert harness.last_text(admin) == "Пользователь не найден."
    other_admin = await make_user(is_admin=True)
    await harness.text(admin, f"/ban {other_admin.telegram_id}")
    assert "Администратора нельзя заблокировать" in harness.last_text(admin)
