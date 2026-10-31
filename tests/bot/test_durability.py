"""Actions are durable before the bot talks to Telegram: a Telegram call that fails afterwards (a
network error, a callback query answered too late after a restart) must not roll them back."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, TelegramMethod
from sqlalchemy import select

from app.bot.callbacks import AccountAction, AccountCb, InterestAction, InterestCb, SettingsAction, SettingsCb
from app.bot.dispatcher import create_bot, create_dispatcher
from app.enums import Gender, InterestStatus
from app.models import Conversation, User
from app.services import guardians, interests
from tests.bot.conftest import BotHarness, RecordingSession


class FailingSession(RecordingSession):
    """Raises the given error for chosen Bot API methods (after recording the call)."""

    def __init__(self, failures: dict[type[TelegramMethod[Any]], Exception]) -> None:
        super().__init__()
        self.failures = failures

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        result = await super().make_request(bot, method, timeout)
        if (error := self.failures.get(type(method))) is not None:
            raise error
        return result


def failing_harness(method: type[TelegramMethod[Any]], error_factory: Any) -> BotHarness:
    session = FailingSession({})
    session.failures[method] = error_factory(method)
    return BotHarness(create_bot("42:TEST-TOKEN", session=session), create_dispatcher(), session)


def network_error(method: type[TelegramMethod[Any]]) -> Exception:
    return TelegramNetworkError(method=method.model_construct(), message="Cannot connect")


def query_too_old(method: type[TelegramMethod[Any]]) -> Exception:
    return TelegramBadRequest(method=method.model_construct(), message="Bad Request: query is too old")


async def test_wali_revoke_survives_a_failed_edit(session, make_profile, make_user):
    harness = failing_harness(EditMessageText, network_error)
    ward = (await make_profile(gender=Gender.FEMALE)).user
    link = await guardians.create_invite(session, ward)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], await make_user())
    await session.commit()

    await harness.press(ward, SettingsCb(action=SettingsAction.WALI_REVOKE).pack())

    assert harness.callback_answers()[0].text == "Вали отключён"
    assert await guardians.get_active_guardian(session, ward.id) is None


async def test_accept_survives_a_callback_query_that_is_too_old(session, make_pair):
    harness = failing_harness(AnswerCallbackQuery, query_too_old)
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.press(woman, InterestCb(action=InterestAction.ACCEPT, interest_id=interest.id).pack())

    await session.refresh(interest)
    assert interest.status == InterestStatus.ACCEPTED
    assert await session.scalar(select(Conversation)) is not None
    assert "Отклик принят" in harness.edited_texts()[-1]


async def test_account_deletion_survives_a_callback_query_that_is_too_old(session, make_user):
    harness = failing_harness(AnswerCallbackQuery, query_too_old)
    user = await make_user()

    await harness.press(user, AccountCb(action=AccountAction.DELETE_CONFIRM).pack())

    assert await session.scalar(select(User).where(User.id == user.id)) is None


@pytest.mark.parametrize("method", [EditMessageText, AnswerCallbackQuery])
async def test_other_telegram_errors_still_reach_the_error_handler(session, make_pair, method):
    harness = failing_harness(method, network_error)
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()

    await harness.press(woman, InterestCb(action=InterestAction.DECLINE, interest_id=interest.id).pack())

    await session.refresh(interest)
    assert interest.status == InterestStatus.DECLINED  # committed before the failing call
