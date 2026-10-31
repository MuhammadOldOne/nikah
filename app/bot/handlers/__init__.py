"""Bot handlers. Routers are created per dispatcher (tests build several) and checked in order:
the witness buttons first (they also work in the witness group), then — in private chats only,
behind the consent gate — commands and menu, dialog states, the chat relay and fallbacks last."""

from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject

from app.bot.handlers import (
    account,
    admin,
    catalog,
    chats,
    consent,
    fallback,
    interests,
    legacy,
    navigation,
    profile,
    report,
    settings,
    start,
    witness,
)


def private_chat(_event: TelegramObject, event_chat: Chat | None = None) -> bool:
    return event_chat is not None and event_chat.type == ChatType.PRIVATE


def create_router() -> Router:
    root = Router(name="bot")
    root.include_router(witness.create_router())
    private = Router(name="private")
    private.message.filter(private_chat)
    private.callback_query.filter(private_chat)
    gate = consent.ConsentGate()
    private.message.middleware(gate)
    private.callback_query.middleware(gate)
    for module in (
        navigation,
        consent,
        admin,
        legacy,
        profile,
        catalog,
        report,
        interests,
        chats,
        settings,
        account,
        start,
        fallback,
    ):
        private.include_router(module.create_router())
    root.include_router(private)
    return root
