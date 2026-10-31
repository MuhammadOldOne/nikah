"""Installation facts kept in ``app_meta`` (key -> JSON), e.g. the completed legacy import."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.models import AppMeta
from app.services.errors import LegacyImportDone

LEGACY_IMPORT_KEY = "legacy_import"
# Telegram moved the witness group to a supergroup (a new chat id): {"from": old id, "to": new id}.
WITNESS_CHAT_MIGRATION_KEY = "witness_chat_migration"


async def get_value(session: AsyncSession, key: str) -> dict[str, Any] | None:
    return await session.scalar(select(AppMeta.value).where(AppMeta.key == key))


async def set_value(session: AsyncSession, key: str, value: Mapping[str, Any]) -> None:
    """Insert or replace ``key``."""
    stmt = insert(AppMeta).values(key=key, value=dict(value), updated_at=utcnow())
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppMeta.key], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}
        )
    )


async def legacy_import_marker(session: AsyncSession) -> dict[str, Any] | None:
    """``{"imported_at": ISO time, "counts": {table: rows}}`` once the legacy import was committed."""
    return await get_value(session, LEGACY_IMPORT_KEY)


async def mark_legacy_import(session: AsyncSession, counts: Mapping[str, int]) -> None:
    """Record the legacy import in the importer's transaction (counts only, no personal data).

    Raises LegacyImportDone if it was recorded before, also by a concurrent run (the insert waits
    for the other transaction), so a second import cannot commit."""
    value = {"imported_at": utcnow().isoformat(), "counts": {str(k): int(v) for k, v in counts.items()}}
    inserted = await session.scalar(
        insert(AppMeta)
        .values(key=LEGACY_IMPORT_KEY, value=value, updated_at=utcnow())
        .on_conflict_do_nothing(index_elements=[AppMeta.key])
        .returning(AppMeta.key)
    )
    if inserted is None:
        raise LegacyImportDone()


async def witness_chat_id(session: AsyncSession) -> int | None:
    """The witness group: WITNESS_CHAT_ID, or the supergroup Telegram upgraded it to (recorded by
    the bot when Telegram reported the upgrade), until the setting itself is changed."""
    configured = get_settings().WITNESS_CHAT_ID
    if configured is None:
        return None
    moved = await get_value(session, WITNESS_CHAT_MIGRATION_KEY)
    if moved is not None and moved.get("from") == configured:
        return int(moved["to"])
    return configured


async def record_witness_chat_migration(session: AsyncSession, old_chat_id: int, new_chat_id: int) -> None:
    await set_value(session, WITNESS_CHAT_MIGRATION_KEY, {"from": old_chat_id, "to": new_chat_id})
