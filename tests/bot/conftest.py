"""Bot test harness: a real aiogram Bot whose HTTP session records requests instead of calling
api.telegram.org, a fresh Dispatcher per test, and helpers that feed updates through it."""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from aiogram.types import User as TelegramUser

from app.bot.dispatcher import create_bot, create_dispatcher
from app.models import User

BOT_ID = 42


class RecordingSession(BaseSession):
    """Records every Bot API call and answers like Telegram would (sent messages get ids)."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._message_ids = itertools.count(10_000)

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            ).as_(bot)
        if isinstance(method, GetMe):
            return TelegramUser(id=BOT_ID, is_bot=True, first_name="Истина", username="nikah_istina_bot")
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def close(self) -> None:
        pass


class BotHarness:
    """Feeds updates from a user's Telegram account and exposes what the bot answered."""

    def __init__(self, bot: Bot, dispatcher: Dispatcher, session: RecordingSession) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.api = session
        self._ids = itertools.count(1)

    @staticmethod
    def account(user: User | int) -> TelegramUser:
        if isinstance(user, int):
            return TelegramUser(id=user, is_bot=False, first_name="Гость")
        return TelegramUser(id=user.telegram_id, is_bot=False, first_name=user.first_name, username=user.username)

    def _message(self, sender: TelegramUser, **content: Any) -> Message:
        return Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=Chat(id=sender.id, type="private"),
            from_user=sender,
            **content,
        )

    async def feed(self, update: Update) -> Any:
        return await self.dispatcher.feed_update(self.bot, update)

    async def text(self, user: User | int, text: str, *, reply_to: Message | None = None) -> None:
        sender = self.account(user)
        message = self._message(sender, text=text, reply_to_message=reply_to)
        await self.feed(Update(update_id=next(self._ids), message=message))

    def bot_message(self, user: User | int, text: str, reply_markup: Any = None) -> Message:
        """A message the bot sent earlier to ``user`` (to reply to it with Telegram's «Reply»)."""
        chat = Chat(id=self.account(user).id, type="private")
        bot_user = TelegramUser(id=BOT_ID, is_bot=True, first_name="Истина")
        return Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=chat,
            from_user=bot_user,
            text=text,
            reply_markup=reply_markup,
        )

    async def photo(self, user: User | int, caption: str | None = None) -> None:
        sender = self.account(user)
        photo = [PhotoSize(file_id="file", file_unique_id="unique", width=10, height=10)]
        await self.feed(Update(update_id=next(self._ids), message=self._message(sender, photo=photo, caption=caption)))

    async def press(self, user: User | int, data: str, message_text: str = "…") -> None:
        sender = self.account(user)
        bot_user = TelegramUser(id=BOT_ID, is_bot=True, first_name="Истина")
        message = Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=Chat(id=sender.id, type="private"),
            from_user=bot_user,
            text=message_text,
        )
        query = CallbackQuery(
            id=str(next(self._ids)), from_user=sender, chat_instance="test", data=data, message=message
        )
        await self.feed(Update(update_id=next(self._ids), callback_query=query))

    # --- What the bot did --------------------------------------------------------------------

    def sent(self, user: User | int | None = None) -> list[SendMessage]:
        chat_id = None if user is None else self.account(user).id
        return [
            method
            for method in self.api.requests
            if isinstance(method, SendMessage) and (chat_id is None or int(method.chat_id) == chat_id)
        ]

    def texts(self, user: User | int | None = None) -> list[str]:
        return [method.text for method in self.sent(user)]

    def last_text(self, user: User | int | None = None) -> str:
        texts = self.texts(user)
        assert texts, "the bot sent nothing"
        return texts[-1]

    def last_markup(self, user: User | int | None = None) -> Any:
        sent = self.sent(user)
        assert sent, "the bot sent nothing"
        return sent[-1].reply_markup

    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return [method for method in self.api.requests if isinstance(method, AnswerCallbackQuery)]

    def edited_texts(self) -> list[str]:
        return [method.text for method in self.api.requests if type(method).__name__ == "EditMessageText"]

    def clear(self) -> None:
        self.api.requests.clear()


def buttons(markup: Any) -> list[Any]:
    """All inline buttons of a markup, flattened."""
    return [button for row in (markup.inline_keyboard if markup else []) for button in row]


def button_data(markup: Any, text_part: str) -> str:
    """callback_data of the first inline button whose text contains ``text_part``."""
    for button in buttons(markup):
        if text_part in button.text:
            return button.callback_data
    raise AssertionError(f"no button containing {text_part!r}: {[b.text for b in buttons(markup)]}")


@pytest.fixture
def harness() -> BotHarness:
    session = RecordingSession()
    return BotHarness(create_bot("42:TEST-TOKEN", session=session), create_dispatcher(), session)
