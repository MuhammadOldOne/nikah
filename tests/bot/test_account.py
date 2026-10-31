"""Settings (notifications, wali), site magic link, account deletion, help."""

from __future__ import annotations

from aiogram.types import ReplyKeyboardRemove
from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import AccountAction, AccountCb, SettingsAction, SettingsCb
from app.enums import Gender
from app.models import User
from app.services import auth, guardians
from tests.bot.conftest import buttons


async def test_notifications_toggle(harness, session, make_user):
    user = await make_user()
    await harness.text(user, texts.MENU_SETTINGS)
    assert "Уведомления в Telegram: <b>включены</b>" in harness.last_text(user)

    await harness.press(user, SettingsCb(action=SettingsAction.NOTIFY_OFF).pack())

    await session.refresh(user)
    assert user.notify_telegram is False
    assert "Уведомления в Telegram: <b>выключены</b>" in harness.edited_texts()[-1]


async def test_wali_invite_status_and_revoke(harness, session, make_profile, make_user):
    ward = (await make_profile(gender=Gender.FEMALE)).user
    await harness.press(ward, SettingsCb(action=SettingsAction.WALI_INVITE).pack())
    instructions = next(text for text in harness.texts(ward) if "Ссылка-приглашение" in text)
    assert "https://t.me/nikah_istina_bot?start=wali_" in instructions

    invite = await guardians.get_pending_invite(session, ward)
    wali = await make_user(first_name="Отец")
    await guardians.accept_invite(session, invite.invite_token, wali)
    await session.commit()

    await harness.text(ward, texts.MENU_SETTINGS)
    assert "Вали: <b>подключён</b> — Отец" in harness.last_text(ward)
    await harness.press(ward, SettingsCb(action=SettingsAction.WALI_REVOKE).pack())
    assert await guardians.get_active_guardian(session, ward.id) is None


async def test_open_site_sends_one_time_link_as_button(harness, session, make_user):
    user = await make_user()
    await harness.text(user, texts.MENU_SITE)

    [button] = buttons(harness.last_markup(user))
    assert button.url.startswith("http://testserver/auth/magic/")
    assert "auth/magic" not in harness.last_text(user)  # never in the text: no link preview
    assert await auth.consume_magic_token(session, button.url.rsplit("/", 1)[1]) == user.id


async def test_delete_account_after_confirmation(harness, session, make_pair):
    man, _ = await make_pair()
    telegram_id = man.telegram_id
    await harness.text(man, "/delete")
    assert "Удалить аккаунт?" in harness.last_text(man)

    await harness.press(man, AccountCb(action=AccountAction.DELETE_CONFIRM).pack())

    assert isinstance(harness.last_markup(telegram_id), ReplyKeyboardRemove)
    assert harness.last_text(telegram_id) == texts.DELETED
    session.expunge_all()
    assert await session.scalar(select(User).where(User.telegram_id == telegram_id)) is None


async def test_banned_user_can_still_delete_the_account(harness, session, make_profile, make_user):
    banned = (await make_profile(await make_user(is_banned=True), gender=Gender.MALE)).user
    telegram_id = banned.telegram_id
    await harness.text(banned, "📝 Анкета")
    assert harness.last_text(banned) == texts.BANNED and "/delete" in texts.BANNED

    await harness.text(banned, "/delete")
    assert "Удалить аккаунт?" in harness.last_text(banned)
    await harness.press(banned, AccountCb(action=AccountAction.DELETE_CONFIRM).pack())

    assert harness.last_text(telegram_id) == texts.DELETED
    session.expunge_all()
    assert await session.scalar(select(User).where(User.telegram_id == telegram_id)) is None


async def test_help_and_unknown_command(harness, make_user):
    user = await make_user()
    await harness.text(user, "/help")
    assert "/delete" in harness.last_text(user)
    await harness.text(user, "/unknown")
    assert harness.last_text(user) == texts.UNKNOWN_COMMAND


async def test_settings_and_site_answer_their_legacy_style_and_emoji_labels(harness, make_user):
    user = await make_user()
    for label in ("Настройки", "⚙️ Настройки"):
        await harness.text(user, label)
        assert harness.last_text(user).startswith("<b>Настройки</b>")
    for label in ("Сайт", "🌐 Открыть сайт"):
        await harness.text(user, label)
        assert harness.last_text(user) == texts.SITE_LINK
