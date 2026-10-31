import hashlib
import hmac
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.clock import utcnow
from app.enums import LoginTokenKind, LoginTokenState
from app.models import LoginToken
from app.services import auth
from app.services.errors import TokenInvalid, UserBanned

BOT_TOKEN = "123456:TEST-TOKEN"
# Login Widget data and the hash computed independently for BOT_TOKEN:
# HMAC_SHA256(key=SHA256(BOT_TOKEN), msg="auth_date=1700000000\nfirst_name=Ahmad\nid=987654321\nusername=ahmad_k")
WIDGET_DATA = {
    "id": "987654321",
    "first_name": "Ahmad",
    "username": "ahmad_k",
    "auth_date": "1700000000",
    "hash": "f7dec2860794e7b561432f9b3de8f7243173df8437f1c4adbd7b7b0b83621246",
}
WIDGET_NOW = 1700000000 + 60


async def expire(session, token: str) -> None:
    await session.execute(
        update(LoginToken)
        .where(LoginToken.token_hash == hashlib.sha256(token.encode()).hexdigest())
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )


async def test_tokens_are_stored_hashed(session):
    token = await auth.create_login_token(session)
    row = await session.scalar(select(LoginToken))
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
    assert row.kind == LoginTokenKind.LOGIN
    assert row.expires_at - row.created_at <= timedelta(minutes=10, seconds=1)
    assert auth.login_deep_link(token) == f"https://t.me/nikah_istina_bot?start=login_{token}"
    assert len(f"login_{token}") <= 64  # Telegram's limit for start parameters


async def test_deep_link_login_flow(session, make_user):
    user = await make_user()
    token = await auth.create_login_token(session)
    assert await auth.login_token_state(session, token) == LoginTokenState.PENDING
    assert await auth.poll_login_token(session, token) is None

    await auth.confirm_login_token(session, token, user)
    await auth.confirm_login_token(session, token, user)  # idempotent for the same user
    assert await auth.login_token_state(session, token) == LoginTokenState.CONFIRMED

    assert await auth.poll_login_token(session, token) == user.id
    assert await auth.poll_login_token(session, token) is None  # single use
    assert await auth.login_token_state(session, token) == LoginTokenState.CONSUMED
    with pytest.raises(TokenInvalid):
        await auth.confirm_login_token(session, token, user)


async def test_confirm_rejects_other_user_unknown_expired_and_banned(session, make_user):
    user, other = await make_user(), await make_user()
    token = await auth.create_login_token(session)
    await auth.confirm_login_token(session, token, user)
    with pytest.raises(TokenInvalid):
        await auth.confirm_login_token(session, token, other)
    with pytest.raises(TokenInvalid):
        await auth.confirm_login_token(session, "unknown", user)

    expired = await auth.create_login_token(session)
    await expire(session, expired)
    with pytest.raises(TokenInvalid):
        await auth.confirm_login_token(session, expired, user)
    assert await auth.login_token_state(session, expired) == LoginTokenState.EXPIRED
    assert await auth.login_token_state(session, "unknown") == LoginTokenState.EXPIRED

    banned = await make_user(is_banned=True)
    with pytest.raises(UserBanned):
        await auth.confirm_login_token(session, await auth.create_login_token(session), banned)


async def test_confirmed_token_expires_before_poll(session, make_user):
    user = await make_user()
    token = await auth.create_login_token(session)
    await auth.confirm_login_token(session, token, user)
    await expire(session, token)
    assert await auth.poll_login_token(session, token) is None


async def test_magic_link_single_use_and_expiry(session, make_user):
    user = await make_user()
    url = await auth.create_magic_link(session, user)
    assert url.startswith("http://testserver/auth/magic/")
    token = url.rsplit("/", 1)[1]
    # A magic token cannot be used as a deep-link login token and vice versa.
    assert await auth.poll_login_token(session, token) is None
    assert await auth.consume_magic_token(session, token) == user.id
    assert await auth.consume_magic_token(session, token) is None

    stale = (await auth.create_magic_link(session, user)).rsplit("/", 1)[1]
    await expire(session, stale)
    assert await auth.consume_magic_token(session, stale) is None


async def test_magic_link_refused_for_banned_users(session, make_user):
    user = await make_user()
    token = (await auth.create_magic_link(session, user)).rsplit("/", 1)[1]
    user.is_banned = True
    await session.flush()
    assert await auth.consume_magic_token(session, token) is None
    with pytest.raises(UserBanned):
        await auth.create_magic_link(session, user)


async def test_magic_token_requires_user(session):
    with pytest.raises(ValueError):
        await auth.create_login_token(session, LoginTokenKind.MAGIC)


async def test_purge_expired_tokens(session):
    fresh = await auth.create_login_token(session)
    old = await auth.create_login_token(session)
    await session.execute(
        update(LoginToken)
        .where(LoginToken.token_hash == hashlib.sha256(old.encode()).hexdigest())
        .values(expires_at=utcnow() - timedelta(days=2))
    )
    assert await auth.purge_expired_tokens(session) == 1
    assert await auth.login_token_state(session, fresh) == LoginTokenState.PENDING


def test_widget_vector_is_accepted():
    user = auth.verify_telegram_widget(WIDGET_DATA, BOT_TOKEN, now=WIDGET_NOW)
    assert user == auth.TelegramWidgetUser(telegram_id=987654321, username="ahmad_k", first_name="Ahmad")


def test_widget_hash_matches_independent_computation():
    check_string = "auth_date=1700000000\nfirst_name=Ahmad\nid=987654321\nusername=ahmad_k"
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    assert hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest() == WIDGET_DATA["hash"]


@pytest.mark.parametrize(
    "tamper",
    [
        {"id": "1"},
        {"username": "someone_else"},
        {"extra": "field"},
        {"hash": "0" * 64},
        {"hash": "фальшивый"},
    ],
)
def test_widget_rejects_tampered_data(tamper):
    assert auth.verify_telegram_widget({**WIDGET_DATA, **tamper}, BOT_TOKEN, now=WIDGET_NOW) is None


def test_widget_rejects_wrong_token_missing_hash_and_old_or_future_data():
    assert auth.verify_telegram_widget(WIDGET_DATA, "654321:OTHER", now=WIDGET_NOW) is None
    assert auth.verify_telegram_widget(WIDGET_DATA, "", now=WIDGET_NOW) is None
    without_hash = {key: value for key, value in WIDGET_DATA.items() if key != "hash"}
    assert auth.verify_telegram_widget(without_hash, BOT_TOKEN, now=WIDGET_NOW) is None
    too_late = 1700000000 + 5 * 60 + 1  # widget data is accepted for 5 minutes
    assert auth.verify_telegram_widget(WIDGET_DATA, BOT_TOKEN, now=too_late) is None
    too_early = 1700000000 - 3600
    assert auth.verify_telegram_widget(WIDGET_DATA, BOT_TOKEN, now=too_early) is None


async def test_widget_login_data_is_single_use(session):
    assert await auth.consume_widget_login(session, WIDGET_DATA, BOT_TOKEN, now=WIDGET_NOW) == auth.TelegramWidgetUser(
        telegram_id=987654321, username="ahmad_k", first_name="Ahmad"
    )
    # The same signed URL again (browser history, a log line): rejected.
    assert await auth.consume_widget_login(session, WIDGET_DATA, BOT_TOKEN, now=WIDGET_NOW + 1) is None
    assert await auth.consume_widget_login(session, {**WIDGET_DATA, "id": "1"}, BOT_TOKEN, now=WIDGET_NOW) is None
    stored = (await session.scalars(select(LoginToken))).all()
    assert [(row.kind, row.consumed_at is not None) for row in stored] == [(LoginTokenKind.WIDGET, True)]
    assert WIDGET_DATA["hash"] not in stored[0].token_hash
