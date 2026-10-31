"""Server-Sent Events streams fed by the per-process EventBroker.

Streams carry only event names and ids; the page then fetches the data through normal,
authorized endpoints. No database session is held while streaming.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.services.events import LISTENER_RECONNECTED, Event
from app.web.broker import BROKER_STOPPED, EventBroker

KEEPALIVE_SECONDS = 15.0
# Streams end after a while; EventSource reconnects and access is checked again on every connection.
MAX_STREAM_SECONDS = 600.0
RETRY_MS = 3000

# Maps an event to (SSE event name, data) or None to skip it.
Classifier = Callable[[Event], tuple[str, dict[str, Any]] | None]


def format_event(name: str, data: dict[str, Any] | None = None) -> str:
    return f"event: {name}\ndata: {json.dumps(data or {}, separators=(',', ':'))}\n\n"


async def event_stream(
    broker: EventBroker,
    user_id: int,
    classify: Classifier,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``user_id``. ``ready`` is sent once subscribed, so the page can fetch
    anything it may have missed without a gap; ``resync`` after the DB listener reconnected."""
    async with broker.subscribe(user_id) as queue:
        yield f"retry: {RETRY_MS}\n\n"
        yield format_event("ready")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        while (remaining := deadline - loop.time()) > 0:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=min(keepalive, remaining))
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event.kind == BROKER_STOPPED:
                return
            if event.kind == LISTENER_RECONNECTED:
                yield format_event("resync")
                continue
            frame = classify(event)
            if frame is not None:
                yield format_event(*frame)


def badge_events(event: Event) -> tuple[str, dict[str, Any]] | None:
    """Pages other than an open chat only refresh the navigation badges."""
    return "badge", {}


def sse_response(request: Request, user_id: int, classify: Classifier) -> StreamingResponse:
    broker: EventBroker = request.app.state.broker
    return StreamingResponse(
        event_stream(broker, user_id, classify),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
