"""The bot's dialog state (aiogram FSM) in PostgreSQL, table ``bot_dialog_states``.

In memory, a restart (every deploy) forgot every dialog in progress, and the next text of a person
who was typing a report comment or an interest message fell through to the chat relay and reached
the chat partner. Here dialogs survive restarts. Inside an update the storage uses the update's
unit of work (app.bot.middlewares), so dialog changes commit or roll back together with the
handler's database work and never wait for locks the handler holds; outside one (aiogram reads the
state before the update's session is opened) it uses a short transaction of its own.

Rows not touched for ``DIALOG_TTL`` read as empty and are purged by the housekeeping task; a
person's rows are deleted with their account (services.users.delete_account).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.middlewares import current_update_session
from app.db import affected_rows, session_scope
from app.models import BotDialogState

DIALOG_TTL = timedelta(days=7)


def storage_key(key: StorageKey) -> str:
    parts = (key.bot_id, key.business_connection_id or "", key.chat_id, key.thread_id or "", key.user_id, key.destiny)
    return ":".join(str(part) for part in parts)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    session = current_update_session()
    if session is not None:
        yield session
        return
    async with session_scope() as own:
        yield own


async def purge_expired(session: AsyncSession, older_than: timedelta = DIALOG_TTL) -> int:
    """Housekeeping: delete dialog states unused for ``older_than``. Returns how many."""
    result = await session.execute(delete(BotDialogState).where(BotDialogState.updated_at < func.now() - older_than))
    return affected_rows(result)


class DatabaseStorage(BaseStorage):
    def __init__(self, ttl: timedelta = DIALOG_TTL) -> None:
        self._ttl = ttl

    async def _read(self, session: AsyncSession, key: StorageKey) -> tuple[str | None, dict[str, Any]]:
        row = (
            await session.execute(
                select(BotDialogState.state, BotDialogState.data).where(
                    BotDialogState.key == storage_key(key), BotDialogState.updated_at > func.now() - self._ttl
                )
            )
        ).one_or_none()
        return (row.state, dict(row.data)) if row is not None else (None, {})

    async def _write(self, key: StorageKey, *, state: Any = ..., data: Any = ...) -> None:
        async with _session() as session:
            current_state, current_data = await self._read(session, key)
            new_state = current_state if state is ... else state
            new_data = current_data if data is ... else data
            if new_state is None and not new_data:
                await session.execute(delete(BotDialogState).where(BotDialogState.key == storage_key(key)))
                return
            values = {"state": new_state, "data": new_data, "updated_at": func.now()}
            await session.execute(
                insert(BotDialogState)
                .values(key=storage_key(key), telegram_user_id=key.user_id, **values)
                .on_conflict_do_update(index_elements=[BotDialogState.key], set_=values)
            )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await self._write(key, state=state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey) -> str | None:
        async with _session() as session:
            return (await self._read(session, key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise DataNotDictLikeError(f"Data must be a dict or dict-like object, got {type(data).__name__}")
        await self._write(key, data=dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with _session() as session:
            return (await self._read(session, key))[1]

    async def close(self) -> None:
        pass
