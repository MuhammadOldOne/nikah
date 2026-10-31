"""Bot test harness: a real aiogram Bot whose HTTP session records requests instead of calling
api.telegram.org, a fresh Dispatcher per test, and helpers that feed updates through it."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from aiogram.types import User as TelegramUser

from app.bot.dispatcher import create_bot, create_dispatcher
from app.bot.outbox import OutboxWorker
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

    def _message(self, sender: TelegramUser, chat: Chat | None = None, **content: Any) -> Message:
        return Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=chat or Chat(id=sender.id, type="private"),
            from_user=sender,
            **content,
        )

    async def feed(self, update: Update) -> Any:
        return await self.dispatcher.feed_update(self.bot, update)

    async def text(
        self, user: User | int, text: str, *, reply_to: Message | None = None, group_id: int | None = None
    ) -> None:
        """A text from ``user`` in the private chat, or in the group ``group_id``."""
        sender = self.account(user)
        chat = Chat(id=group_id, type="supergroup") if group_id is not None else None
        message = self._message(sender, chat, text=text, reply_to_message=reply_to)
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

    async def press(
        self,
        user: User | int,
        data: str,
        message_text: str = "…",
        *,
        group_id: int | None = None,
        message_date: datetime | None = None,
    ) -> None:
        """A press on a button of a bot message in the private chat (or in the group ``group_id``)."""
        sender = self.account(user)
        bot_user = TelegramUser(id=BOT_ID, is_bot=True, first_name="Истина")
        chat = Chat(id=group_id, type="supergroup") if group_id is not None else Chat(id=sender.id, type="private")
        message = Message(
            message_id=next(self._ids),
            date=message_date or datetime.now(UTC),
            chat=chat,
            from_user=bot_user,
            text=message_text,
        )
        query = CallbackQuery(
            id=str(next(self._ids)), from_user=sender, chat_instance="test", data=data, message=message
        )
        await self.feed(Update(update_id=next(self._ids), callback_query=query))

    # --- What the bot did --------------------------------------------------------------------

    def sent(self, user: User | int | None = None) -> list[SendMessage]:
        """Messages sent to ``user`` (a User or a raw chat id, e.g. a group)."""
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


def menu_labels(markup: Any) -> list[list[str]]:
    """Rows of a reply keyboard's labels."""
    return [[button.text for button in row] for row in markup.keyboard]


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


# --- Outbox worker with a fake Bot ---------------------------------------------------------------

SEND_METHOD = SendMessage(chat_id=1, text="x")  # for building Telegram exceptions


@dataclass
class Sent:
    chat_id: int
    text: str
    reply_markup: Any


class FakeBot:
    """Records send_message calls; ``fail(chat_id, exc, ...)`` makes the next sends to that chat raise."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.delivered = asyncio.Event()
        self._failures: dict[int, list[Exception]] = {}

    def fail(self, chat_id: int, *errors: Exception) -> None:
        self._failures.setdefault(chat_id, []).extend(errors)

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None, **_: Any) -> None:
        if self._failures.get(chat_id):
            raise self._failures[chat_id].pop(0)
        self.sent.append(Sent(chat_id, text, reply_markup))
        self.delivered.set()

    def to(self, user: User | int) -> list[Sent]:
        """Messages sent to ``user`` (a User or a raw chat id such as the witness group)."""
        chat_id = user if isinstance(user, int) else user.telegram_id
        return [item for item in self.sent if item.chat_id == chat_id]


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def worker(bot: FakeBot) -> OutboxWorker:
    return OutboxWorker(bot, send_delay=0)  # type: ignore[arg-type]
