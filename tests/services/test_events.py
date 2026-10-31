import asyncio
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import func, select

from app.clock import utcnow
from app.db import get_sessionmaker
from app.models import Outbox
from app.services import events, interests
from app.services.events import Event, EventKind, EventListener, asyncpg_dsn
from tests.conftest import TEST_DATABASE_URL


async def _no_notification(queue: asyncio.Queue, wait: float = 0.3) -> bool:
    try:
        await asyncio.wait_for(queue.get(), wait)
    except TimeoutError:
        return True
    return False


async def test_emit_writes_outbox_and_notifies_on_commit(session, notifications):
    row = await events.emit(
        session, EventKind.INTEREST_CREATED, {"interest_id": 7, "from_user_id": 1, "to_user_id": 2}, [2, 1, 2]
    )
    assert row.id is not None
    assert row.payload == {"interest_id": 7, "from_user_id": 1, "to_user_id": 2, "user_ids": [1, 2]}
    # NOTIFY is transactional: nothing is delivered before commit.
    assert await _no_notification(notifications)
    await session.commit()
    received = await asyncio.wait_for(notifications.get(), 5)
    assert received == {
        "kind": "interest.created",
        "outbox_id": row.id,
        "interest_id": 7,
        "from_user_id": 1,
        "to_user_id": 2,
        "user_ids": [1, 2],
    }


async def test_rollback_discards_outbox_row_and_notification(session, notifications):
    await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 1})
    await session.rollback()
    assert await _no_notification(notifications)
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_oversized_payload_is_rejected(session):
    with pytest.raises(ValueError):
        await events.emit(session, EventKind.REPORT_CREATED, {"blob": "x" * 9000})


async def test_fetch_pending_honours_schedule_and_status(session):
    due = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 1})
    later = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 2})
    sent = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 3})
    later.next_attempt_at = utcnow() + timedelta(minutes=5)
    sent.sent_at = utcnow()
    await session.commit()

    rows = await events.fetch_pending(session)
    assert [row.id for row in rows] == [due.id]


async def test_fetch_pending_skips_rows_locked_by_another_worker(session):
    first = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 1})
    second = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 2})
    await session.commit()

    async with get_sessionmaker()() as other:
        locked = await events.fetch_pending(other, limit=1)
        assert [row.id for row in locked] == [first.id]
        rows = await events.fetch_pending(session)
        assert [row.id for row in rows] == [second.id]
        await other.rollback()
    await session.rollback()


async def test_mark_sent_and_failed_with_backoff_and_give_up(session):
    row = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 1})
    await events.mark_failed(session, row, "telegram down", max_attempts=3)
    assert row.attempts == 1 and row.last_error == "telegram down" and row.failed_at is None
    assert row.next_attempt_at > utcnow() + timedelta(seconds=5)
    assert await events.fetch_pending(session) == []

    await events.mark_failed(session, row, "again", max_attempts=3)
    await events.mark_failed(session, row, "again", max_attempts=3)
    assert row.attempts == 3 and row.failed_at is not None

    other = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 2})
    await events.mark_sent(session, other)
    assert other.sent_at is not None
    assert await events.fetch_pending(session) == []


def test_retry_delay_grows_and_is_capped():
    assert events.retry_delay(1) == timedelta(seconds=10)
    assert events.retry_delay(2) == timedelta(seconds=20)
    assert events.retry_delay(4) == timedelta(seconds=80)
    assert events.retry_delay(30) == timedelta(hours=1)


async def test_purge_processed(session):
    old = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 1})
    fresh = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 2})
    pending = await events.emit(session, EventKind.REPORT_CREATED, {"report_id": 3})
    old.sent_at = utcnow() - timedelta(days=40)
    fresh.sent_at = utcnow()
    await session.flush()
    assert await events.purge_processed(session) == 1
    remaining = set(await session.scalars(select(Outbox.id)))
    assert remaining == {fresh.id, pending.id}


def test_event_parse():
    event = Event.parse('{"kind": "message.created", "outbox_id": 5, "user_ids": [1, 2], "message_id": 9}')
    assert event is not None
    assert event.kind == "message.created" and event.outbox_id == 5 and event.user_ids == {1, 2}
    assert event.data["message_id"] == 9
    assert Event.parse("not json") is None
    assert Event.parse('{"no_kind": 1}') is None


def test_asyncpg_dsn():
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


async def test_event_listener_delivers_and_survives_reconnect(session):
    listener = EventListener(min_backoff=0.05, max_backoff=0.2)
    callback_events: list[Event] = []

    async def on_event(event: Event) -> None:
        callback_events.append(event)

    listener.add_callback(on_event)
    async with listener:
        await listener.wait_connected(5)
        async with listener.subscribe() as subscription:
            row = await events.emit(session, EventKind.MESSAGE_CREATED, {"message_id": 1}, [10, 11])
            await session.commit()
            event = await asyncio.wait_for(subscription.get(), 5)
            assert event.kind == "message.created" and event.outbox_id == row.id
            assert event.user_ids == {10, 11}

            # Kill the LISTEN backend: the listener must reconnect and tell subscribers to resync.
            admin = await asyncpg.connect(asyncpg_dsn(TEST_DATABASE_URL))
            try:
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE query LIKE 'LISTEN%' AND pid <> pg_backend_pid() AND datname = current_database()"
                )
            finally:
                await admin.close()
            reconnected = await asyncio.wait_for(subscription.get(), 10)
            assert reconnected.kind == events.LISTENER_RECONNECTED
            await listener.wait_connected(5)

            await events.emit(session, EventKind.MESSAGE_CREATED, {"message_id": 2}, [10])
            await session.commit()
            event = await asyncio.wait_for(subscription.get(), 5)
            assert event.data["message_id"] == 2
    kinds = [event.kind for event in callback_events]
    assert kinds.count("message.created") == 2


async def test_subscription_drops_oldest_when_full():
    subscription = events.Subscription(maxsize=2)
    for index in range(3):
        subscription.put(Event(kind=str(index), outbox_id=index, user_ids=frozenset()))
    assert [(await subscription.get()).kind, (await subscription.get()).kind] == ["1", "2"]


async def test_outbox_rows_are_created_by_services(session, make_pair):
    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id, "Ассаляму алейкум")
    kinds = list(await session.scalars(select(Outbox.kind)))
    assert kinds == ["interest.created"]
