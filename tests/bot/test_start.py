"""/start: greeting and deep links (login_, wali_, p_), banned users."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import Chat, Message, ReplyKeyboardMarkup, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import CANCEL, LoginCb, WaliCb
from app.enums import Gender, LoginTokenState
from app.models import User
from app.services import auth, guardians
from tests.bot.conftest import button_data, buttons


async def test_plain_start_creates_account_and_shows_principles_and_menu(harness, session):
    await harness.text(555_000_001, "/start")

    user = await session.scalar(select(User).where(User.telegram_id == 555_000_001))
    assert user is not None and user.first_name == "Гость" and user.last_seen_at is not None
    text = harness.last_text(555_000_001)
    assert "Без фото" in text and "Отклик → согласие → чат" in text and "Вали" in text and "Модерация" in text
    menu = harness.last_markup(555_000_001)
    assert isinstance(menu, ReplyKeyboardMarkup)
    assert [button.text for row in menu.keyboard for button in row] == list(texts.MENU_BUTTONS)


async def test_unknown_payload_falls_back_to_greeting(harness):
    await harness.text(555_000_002, "/start ref_abc")
    assert harness.last_text(555_000_002) == texts.GREETING


async def test_login_deep_link_confirms_token_after_button(harness, session, make_user):
    user = await make_user()
    token = await auth.create_login_token(session)
    await session.commit()

    await harness.text(user, f"/start login_{token}")
    assert "Вход на сайт" in harness.last_text(user)
    assert await auth.login_token_state(session, token) == LoginTokenState.PENDING  # not before the button

    await harness.press(user, button_data(harness.last_markup(user), "это я"))

    assert "✅ Вход на сайт подтверждён. Вернитесь во вкладку браузера." in harness.edited_texts()
    assert await auth.login_token_state(session, token) == LoginTokenState.CONFIRMED
    assert await auth.poll_login_token(session, token) == user.id


async def test_login_with_invalid_token_explains(harness, make_user):
    user = await make_user()
    await harness.text(user, "/start login_" + "x" * 43)
    assert "недействительна" in harness.last_text(user)
    await harness.press(user, LoginCb(token="y" * 43).pack())
    assert any("недействительна" in text for text in harness.edited_texts())


async def test_wali_deep_link_links_guardian(harness, session, make_profile, make_user):
    ward = (await make_profile(gender=Gender.FEMALE, name="Фатима")).user
    link = await guardians.create_invite(session, ward)
    await session.commit()
    wali = await make_user(first_name="Отец")
    token = link.rsplit("wali_", 1)[1]

    await harness.text(wali, f"/start wali_{token}")
    prompt = harness.last_text(wali)
    assert f"<b>Фатима</b> (#{ward.id})" in prompt
    assert WaliCb.unpack(button_data(harness.last_markup(wali), "Подтвердить")).token == token

    await harness.press(wali, WaliCb(token=token).pack())

    active = await guardians.get_active_guardian(session, ward.id)
    assert active is not None and active.guardian_user_id == wali.id
    assert any("Вы стали вали для <b>Фатима</b>" in text for text in harness.edited_texts())


async def test_wali_invite_cannot_be_accepted_by_ward(harness, session, make_profile):
    ward = (await make_profile(gender=Gender.FEMALE)).user
    link = await guardians.create_invite(session, ward)
    await session.commit()
    await harness.text(ward, f"/start {link.rsplit('start=', 1)[1]}")
    assert "самого себя" in harness.last_text(ward)
    assert await guardians.get_active_guardian(session, ward.id) is None


async def test_wali_cancel_button_does_not_link(harness, session, make_profile, make_user):
    ward = (await make_profile(gender=Gender.FEMALE)).user
    link = await guardians.create_invite(session, ward)
    await session.commit()
    wali = await make_user()
    await harness.text(wali, f"/start {link.rsplit('start=', 1)[1]}")
    await harness.press(wali, CANCEL)
    assert await guardians.get_active_guardian(session, ward.id) is None
    assert texts.CANCELLED in harness.edited_texts()


async def test_profile_link_shows_card_only_when_allowed(harness, make_pair, make_user):
    man, woman = await make_pair()
    await harness.text(man, f"/start p_{woman.id}")
    card = harness.last_text(man)
    assert "<b>Фатима</b>" in card and f"#{woman.id}" in card
    assert [button.text for button in buttons(harness.last_markup(man))] == ["💌 Откликнуться", "⚠️ Пожаловаться"]

    outsider = await make_user()
    await harness.text(outsider, f"/start p_{woman.id}")
    assert harness.last_text(outsider) == texts.PROFILE_REQUIRED

    await harness.text(woman, f"/start p_{woman.id + 1000}")
    assert "не найдена" in harness.last_text(woman)


async def test_banned_user_only_gets_polite_reply(harness, make_user):
    banned = await make_user(is_banned=True)
    await harness.text(banned, "/start")
    assert harness.texts(banned) == [texts.BANNED]
    await harness.press(banned, CANCEL)
    [answer] = harness.callback_answers()
    assert answer.text == texts.BANNED and answer.show_alert


async def test_group_chats_are_ignored(harness, session):
    sender = TelegramUser(id=555_000_009, is_bot=False, first_name="Гость")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=-100, type="group"), from_user=sender, text="/start"
    )
    await harness.feed(Update(update_id=1, message=message))
    assert harness.sent() == []
    assert await session.scalar(select(User).where(User.telegram_id == 555_000_009)) is None
