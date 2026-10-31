"""Per-process fan-out of database events to open browser tabs (Server-Sent Events).

One ``EventListener`` LISTENs on the ``events`` channel; every notification is copied to the queues
of the users listed in its ``user_ids``. A reconnect of the listener is broadcast to everybody so
the pages resync (notifications sent while it was down are lost).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.services.events import LISTENER_RECONNECTED, Event, EventListener

logger = logging.getLogger(__name__)

BROKER_STOPPED = "broker.stopped"
QUEUE_SIZE = 64


class EventBroker:
    def __init__(self) -> None:
        self._listener: EventListener | None = None
        self._queues: defaultdict[int, set[asyncio.Queue[Event]]] = defaultdict(set)

    async def start(self, listener: EventListener | None = None) -> None:
        if self._listener is not None:
            return
        self._listener = listener or EventListener()
        self._listener.add_callback(self.publish)
        await self._listener.start()

    async def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.remove_callback(self.publish)
            await listener.stop()
        self._broadcast(Event(kind=BROKER_STOPPED, outbox_id=None, user_ids=frozenset()))

    @property
    def subscriber_count(self) -> int:
        return sum(len(queues) for queues in self._queues.values())

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to the streams of the users it concerns."""
        if event.kind == LISTENER_RECONNECTED:
            self._broadcast(event)
            return
        for user_id in event.user_ids:
            for queue in self._queues.get(user_id, ()):
                _put_dropping_oldest(queue, event)

    def _broadcast(self, event: Event) -> None:
        for queues in self._queues.values():
            for queue in queues:
                _put_dropping_oldest(queue, event)

    @asynccontextmanager
    async def subscribe(self, user_id: int) -> AsyncIterator[asyncio.Queue[Event]]:
        """A queue of events for ``user_id``; removed when the block exits (browser disconnected)."""
        queue: asyncio.Queue[Event] = asyncio.Queue(QUEUE_SIZE)
        self._queues[user_id].add(queue)
        try:
            yield queue
        finally:
            queues = self._queues.get(user_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    del self._queues[user_id]


def _put_dropping_oldest(queue: asyncio.Queue[Event], event: Event) -> None:
    # A slow tab loses old events only: every page resyncs by id, so nothing is lost for good.
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)
