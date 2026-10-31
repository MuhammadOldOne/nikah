"""Time helpers. Timestamps are stored in UTC; users see Moscow time."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def utcnow() -> datetime:
    return datetime.now(UTC)


def current_year() -> int:
    return datetime.now(MOSCOW_TZ).year


def age_from_birth_year(birth_year: int) -> int:
    """Age by birth year only (the profile stores no birth date)."""
    return current_year() - birth_year


def to_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MOSCOW_TZ)


def msk_day_time(value: datetime) -> str:
    """Compact Moscow date and time for message lists: «01.04 13:05» in the current year,
    «01.04.2025 13:05» otherwise (chat history moved from the legacy bot is years old)."""
    local = to_moscow(value)
    return local.strftime("%d.%m %H:%M" if local.year == current_year() else "%d.%m.%Y %H:%M")
