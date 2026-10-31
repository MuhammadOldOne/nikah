"""Logging without personal data (shared by the website and the bot)."""

from __future__ import annotations

import traceback


def exc_summary(exc: BaseException) -> str:
    """Exception type and stack frames, without the message: messages of database and Telegram
    errors may contain user data (SQL parameters, message texts, chat ids)."""
    return f"{type(exc).__name__}\n{''.join(traceback.format_tb(exc.__traceback__)).rstrip()}"
