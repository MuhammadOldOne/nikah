"""Bot handlers. Routers are created per dispatcher (tests build several) and checked in order:
commands and menu first, dialog states next, the chat relay and fallbacks last."""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import (
    account,
    admin,
    catalog,
    chats,
    fallback,
    interests,
    navigation,
    profile,
    report,
    settings,
    start,
)


def create_router() -> Router:
    root = Router(name="bot")
    for module in (navigation, admin, profile, catalog, report, interests, chats, settings, account, start, fallback):
        root.include_router(module.create_router())
    return root
