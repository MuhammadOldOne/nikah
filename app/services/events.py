"""Domain events: transactional outbox + PostgreSQL LISTEN/NOTIFY.

``emit`` inserts an ``outbox`` row and calls ``pg_notify('events', …)`` in the caller's
transaction, so the notification is delivered only if (and when) that transaction commits.

- The bot worker delivers outbox rows to Telegram (``fetch_pending`` / ``mark_sent`` /
  ``mark_failed``) and uses NOTIFY only as a wake-up signal.
- The website fans NOTIFY events out to open browser tabs (SSE) by ``Event.user_ids``.

Payloads and notifications carry IDs only, never personal data. Schemas: docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

import asyncpg
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import affected_rows
from app.models import Outbox

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "events"
NOTIFY_MAX_BYTES = 7800  # PostgreSQL's hard limit is 8000 bytes; leaves room for the outbox id
MAX_ATTEMPTS = 8
BACKOFF_BASE = timedelta(seconds=10)
BACKOFF_MAX = timedelta(hours=1)


class EventKind(StrEnum):
    MESSAGE_CREATED = "message.created"  # a participant's message (kind 'user')
    MESSAGE_WITNESS = "message.witness"  # the witness wrote to both participants (kind 'witness')
    INTEREST_CREATED = "interest.created"
    INTEREST_ACCEPTED = "interest.accepted"
    INTEREST_DECLINED = "interest.declined"
    CONVERSATION_CLOSED = "conversation.closed"
    PROFILE_SUBMITTED = "profile.submitted"
    PROFILE_MODERATED = "profile.moderated"
    REPORT_CREATED = "report.created"
    GUARDIAN_LINKED = "guardian.linked"
    # Delivered to the witness (WITNESS_CHAT_ID, or each admin when it is unset); no SSE user_ids.
    WITNESS_COPY = "witness.copy"
    WITNESS_CONVERSATION_OPENED = "witness.conversation_opened"
    WITNESS_CONVERSATION_CLOSED = "witness.conversation_closed"
    WITNESS_REPORT = "witness.report"


# Synthetic event delivered by EventListener to its subscribers after the LISTEN connection was
# re-established: notifications sent while it was down are lost, so consumers should resync.
LISTENER_RECONNECTED = "listener.reconnected"


async def emit(
    session: AsyncSession,
    kind: EventKind,
    payload: Mapping[str, Any],
    notify_user_ids: Iterable[int] = (),
) -> Outbox:
    """Record an event in the outbox and NOTIFY listeners in the same transaction.

    ``notify_user_ids`` are the users whose open web pages should update (SSE fan-out); they are
    stored in the payload as ``user_ids``. Telegram recipients are derived by the bot per kind.
    """
    data: dict[str, Any] = {**payload, "user_ids": sorted(set(notify_user_ids))}
    if len(_encode({"kind": kind.value, "outbox_id": 0, **data})) > NOTIFY_MAX_BYTES:
        raise ValueError(f"event payload is too large for NOTIFY ({kind})")
    row = Outbox(kind=kind.value, payload=data)
    session.add(row)
    await session.flush()
    notification = _encode({"kind": kind.value, "outbox_id": row.id, **data}).decode()
    await session.execute(select(func.pg_notify(EVENTS_CHANNEL, notification)))
    return row


async def count_recent(session: AsyncSession, kind: EventKind, within: timedelta, **payload: Any) -> int:
    """How many ``kind`` events whose payload contains ``payload`` were emitted in the last
    ``within`` (rate limits on actions that notify the admins)."""
    count = await session.scalar(
        select(func.count())
        .select_from(Outbox)
        .where(
            Outbox.kind == kind.value,
            Outbox.created_at > func.now() - within,
            Outbox.payload.contains(payload),
        )
    )
    return count or 0


def _encode(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


async def fetch_pending(session: AsyncSession, limit: int = 50) -> list[Outbox]:
    """Lock and return due, undelivered rows (FOR UPDATE SKIP LOCKED: safe with several workers).

    The locks are held until the caller's transaction ends, so process and mark the rows in the
    same unit of work.
    """
    stmt = (
        select(Outbox)
        .where(Outbox.sent_at.is_(None), Outbox.failed_at.is_(None), Outbox.next_attempt_at <= func.now())
        .order_by(Outbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.scalars(stmt)).all())


async def mark_sent(session: AsyncSession, row: Outbox) -> None:
    row.sent_at = utcnow()
    await session.flush()


def retry_delay(attempts: int) -> timedelta:
    """Exponential backoff: 10s, 20s, 40s, … capped at one hour."""
    return min(BACKOFF_BASE * 2 ** max(attempts - 1, 0), BACKOFF_MAX)


async def mark_failed(session: AsyncSession, row: Outbox, error: str, *, max_attempts: int = MAX_ATTEMPTS) -> None:
    """Count a failed delivery; schedule a retry with backoff or give up after ``max_attempts``."""
    row.attempts += 1
    row.last_error = error[:2000]
    now = utcnow()
    if row.attempts >= max_attempts:
        row.failed_at = now
    else:
        row.next_attempt_at = now + retry_delay(row.attempts)
    await session.flush()


async def purge_processed(session: AsyncSession, older_than: timedelta = timedelta(days=30)) -> int:
    """Delete delivered or abandoned rows older than ``older_than``. Returns the number deleted."""
    cutoff = utcnow() - older_than
    result = await session.execute(delete(Outbox).where(or_(Outbox.sent_at < cutoff, Outbox.failed_at < cutoff)))
    return affected_rows(result)


# --- Listening -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """A NOTIFY payload: ``kind``, ``outbox_id``, ``user_ids`` and the kind's ID fields in ``data``."""

    kind: str
    outbox_id: int | None
    user_ids: frozenset[int]
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> Event | None:
        try:
            data = json.loads(raw)
            return cls(
                kind=str(data["kind"]),
                outbox_id=data.get("outbox_id"),
                user_ids=frozenset(int(uid) for uid in data.get("user_ids", ())),
                data=data,
            )
        except (ValueError, KeyError, TypeError):
            logger.warning("ignoring malformed event notification: %.200s", raw)
            return None


EventCallback = Callable[[Event], Awaitable[None] | None]


def asyncpg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy URL (postgresql+asyncpg://…) to a plain libpq DSN for asyncpg."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _on_terminated(event: asyncio.Event, _conn: object) -> None:
    event.set()


class Subscription:
    """A subscriber's bounded queue. When it is full the oldest event is dropped (consumers
    resync by ``after_id`` anyway)."""

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)

    def put(self, event: Event) -> None:
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()


class EventListener:
    """Keeps one dedicated asyncpg connection LISTENing on ``events`` and dispatches every
    notification to subscriptions and callbacks. Reconnects with exponential backoff.

    Usage (one listener per process)::

        listener = EventListener()
        await listener.start()
        async with listener.subscribe() as events:      # web: one per SSE stream
            async for event in events:
                ...
        listener.add_callback(on_event)                 # bot: wake the outbox worker
        await listener.stop()
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        channel: str = EVENTS_CHANNEL,
        min_backoff: float = 0.5,
        max_backoff: float = 30.0,
        health_check_interval: float = 30.0,
    ) -> None:
        self._dsn = dsn or asyncpg_dsn(get_settings().DATABASE_URL)
        self._channel = channel
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._health_check_interval = health_check_interval
        self._subscriptions: set[Subscription] = set()
        self._callbacks: list[EventCallback] = []
        self._callback_tasks: set[asyncio.Future[Any]] = set()
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="event-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._callback_tasks):
            task.cancel()

    async def __aenter__(self) -> EventListener:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @contextlib.asynccontextmanager
    async def subscribe(self, maxsize: int = 256) -> AsyncIterator[Subscription]:
        subscription = Subscription(maxsize)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)

    def add_callback(self, callback: EventCallback) -> None:
        """Call ``callback(event)`` (sync or async) for every event; errors are logged."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: EventCallback) -> None:
        with contextlib.suppress(ValueError):
            self._callbacks.remove(callback)

    def _dispatch(self, event: Event) -> None:
        for subscription in list(self._subscriptions):
            subscription.put(event)
        for callback in list(self._callbacks):
            try:
                result = callback(event)
            except Exception:
                logger.exception("event callback failed")
                continue
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._callback_tasks.add(task)
                task.add_done_callback(self._on_callback_done)

    def _on_callback_done(self, task: asyncio.Future[Any]) -> None:
        self._callback_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("event callback failed", exc_info=task.exception())

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        event = Event.parse(payload)
        if event is not None:
            self._dispatch(event)

    async def _run(self) -> None:
        backoff = self._min_backoff
        was_connected = False
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self._dsn, timeout=10)
                terminated = asyncio.Event()
                conn.add_termination_listener(functools.partial(_on_terminated, terminated))
                await conn.add_listener(self._channel, self._on_notify)
                self._connected.set()
                if was_connected:
                    self._dispatch(Event(kind=LISTENER_RECONNECTED, outbox_id=None, user_ids=frozenset()))
                was_connected = True
                backoff = self._min_backoff
                while not terminated.is_set():
                    try:
                        await asyncio.wait_for(terminated.wait(), self._health_check_interval)
                    except TimeoutError:
                        await conn.execute("SELECT 1", timeout=10)
                raise ConnectionError("LISTEN connection terminated")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("event listener disconnected (%s); retrying in %.1fs", exc, backoff)
            finally:
                self._connected.clear()
                if conn is not None and not conn.is_closed():
                    conn.terminate()
            await asyncio.sleep(backoff * random.uniform(0.8, 1.2))  # jitter, not crypto
            backoff = min(backoff * 2, self._max_backoff)
