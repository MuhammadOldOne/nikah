"""Grant or revoke admin rights by Telegram id.

    docker compose exec web python -m scripts.make_admin 123456789
    docker compose exec web python -m scripts.make_admin 123456789 --revoke

The person must have pressed /start in the bot first (that creates the account). Admins open /admin
on the site and receive new profiles and reports in the bot. People listed in ADMIN_TELEGRAM_IDS
are admins regardless of this flag.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.db import dispose_engine, session_scope
from app.services import users


class CommandError(Exception):
    """A problem to report to the operator (Russian message)."""


async def set_admin(telegram_id: int, *, grant: bool) -> str:
    """Set ``users.is_admin`` for the account with this Telegram id; returns a report line."""
    async with session_scope() as session:
        user = await users.get_by_telegram_id(session, telegram_id)
        if user is None:
            raise CommandError(
                f"Пользователь с Telegram ID {telegram_id} не найден. Пусть он сначала нажмёт /start в боте."
            )
        if grant and user.is_banned:
            raise CommandError(f"Пользователь #{user.id} заблокирован. Сначала разблокируйте его в /admin.")
        user.is_admin = grant
        name = f"{user.display_name} (#{user.id})"
    if grant:
        return f"Готово: {name} — администратор. Панель: {get_settings().SITE_URL}/admin"
    message = f"Готово: {name} больше не администратор."
    if telegram_id in get_settings().ADMIN_TELEGRAM_IDS:
        message += " Но этот ID указан в ADMIN_TELEGRAM_IDS: уберите его из .env и перезапустите web и bot."
    return message


async def _run(telegram_id: int, *, grant: bool) -> str:
    try:
        return await set_admin(telegram_id, grant=grant)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.make_admin", description="Выдать или снять права администратора по Telegram ID."
    )
    parser.add_argument("telegram_id", type=int, help="Telegram ID пользователя (число)")
    parser.add_argument("--revoke", action="store_true", help="снять права администратора")
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(_run(args.telegram_id, grant=not args.revoke)))
    except CommandError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
