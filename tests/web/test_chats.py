"""Chats: access control, sending, the messages partial, closing, reports and live updates (SSE)."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.enums import ConversationCloseReason, Gender, MessageOrigin
from app.models import Message, Report
from app.services import chat, guardians, users
from app.services.events import LISTENER_RECONNECTED, Event, EventListener
from app.web.broker import EventBroker
from app.web.main import SESSION_COOKIE
from tests.conftest import session_cookie
from tests.web.conftest import csrf_of


@pytest.fixture
async def couple(make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    return man, woman, conversation


async def test_chat_list_and_page(client, dev_login, couple, session):
    man, woman, conversation = couple
    await chat.send_message(session, conversation.id, woman, "Ва алейкум ассалам", MessageOrigin.TELEGRAM)
    await session.commit()
    await dev_login(client, man)

    listing = await client.get("/chats")
    assert listing.status_code == 200
    assert f"Анкета №{woman.profile.number}" in listing.text and "Ва алейкум ассалам" in listing.text
    assert 'data-badge="unread">1<' in listing.text

    page = await client.get(f"/chats/{conversation.id}")
    assert page.status_code == 200
    assert "Ва алейкум ассалам" in page.text
    assert "✈️ из Telegram" in page.text
    assert f'data-stream="/chats/{conversation.id}/stream"' in page.text
    assert await chat.unread_total(session, man) == 0  # viewing marks messages read


async def test_non_participants_get_404(client, dev_login, couple, make_profile):
    _, _, conversation = couple
    stranger = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, stranger.user)
    csrf = await csrf_of(client)
    cid = conversation.id
    assert (await client.get(f"/chats/{cid}")).status_code == 404
    assert (await client.get(f"/chats/{cid}/messages?after_id=0")).status_code == 404
    assert (await client.get(f"/chats/{cid}/stream")).status_code == 404
    assert (await client.post(f"/chats/{cid}/messages", data={"csrf_token": csrf, "text": "x"})).status_code == 404
    assert (await client.post(f"/chats/{cid}/close", data={"csrf_token": csrf})).status_code == 404
    assert (await client.post(f"/chats/{cid}/report", data={"csrf_token": csrf, "reason": "fraud"})).status_code == 404


async def test_anonymous_stream_redirects_to_login(client, couple):
    _, _, conversation = couple
    response = await client.get(f"/chats/{conversation.id}/stream")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert (await client.get("/events")).status_code == 303


async def test_message_post_appears_in_partial(client, dev_login, couple, session):
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(
        f"/chats/{conversation.id}/messages",
        data={"csrf_token": csrf, "text": "Ассаляму алейкум!\n<b>Как дела?</b>", "after_id": "0"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 201
    assert "msg--mine" in response.text and "&lt;b&gt;Как дела?&lt;/b&gt;" in response.text
    message = await session.scalar(select(Message))
    assert message.origin == MessageOrigin.WEB and message.sender_id == man.id

    partial = await client.get(f"/chats/{conversation.id}/messages?after_id=0", headers={"HX-Request": "true"})
    assert f'id="msg-{message.id}"' in partial.text and "<html" not in partial.text
    assert (await client.get(f"/chats/{conversation.id}/messages?after_id={message.id}")).text.strip() == ""


async def test_message_post_without_js_redirects(client, dev_login, couple):
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(f"/chats/{conversation.id}/messages", data={"csrf_token": csrf, "text": "Салам"})
    assert response.status_code == 303 and response.headers["location"] == f"/chats/{conversation.id}"
    assert "Салам" in (await client.get(f"/chats/{conversation.id}")).text


async def test_partner_reads_messages_through_partial(client, dev_login, couple, session):
    man, woman, conversation = couple
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    await session.commit()
    await dev_login(client, woman)
    await client.get(f"/chats/{conversation.id}/messages?after_id=0")
    assert await chat.unread_total(session, woman) == 1
    await client.get(f"/chats/{conversation.id}/messages?after_id=0&read=1")
    assert await chat.unread_total(session, woman) == 0


async def test_empty_message_is_rejected(client, dev_login, couple):
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(
        f"/chats/{conversation.id}/messages", data={"csrf_token": csrf, "text": "   "}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 422 and "toast" in response.headers["HX-Trigger"]


async def test_close_conversation(client, dev_login, couple, session):
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(f"/chats/{conversation.id}/close", data={"csrf_token": csrf})
    assert response.status_code == 303
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert "Писать в этот чат нельзя" in page and 'id="composer"' not in page
    response = await client.post(f"/chats/{conversation.id}/messages", data={"csrf_token": csrf, "text": "Ещё"})
    assert response.status_code == 422 and "Общение в этом чате завершено" in response.text
    assert "Вы завершили общение." in page


async def test_a_migrated_closed_chat_does_not_blame_either_side(client, dev_login, couple, session):
    """The import closes legacy chats as closed_by_user without a closer: nobody is told that the
    other person ended it."""
    man, woman, conversation = couple
    await chat.force_close(session, conversation, ConversationCloseReason.CLOSED_BY_USER, closed_by_id=None)
    await session.commit()
    for person, other in ((man, woman), (woman, man)):
        await dev_login(client, person)
        for page in (f"/chats/{conversation.id}", f"/p/{other.profile.number}"):
            text = (await client.get(page)).text
            assert "Общение завершено." in text and "Собеседник завершил общение" not in text, page
            assert "Вы завершили общение" not in text, page


async def test_report_from_chat(client, dev_login, couple, session):
    man, woman, conversation = couple
    await dev_login(client, woman)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(f"/chats/{conversation.id}/report", data={"csrf_token": csrf, "reason": "insult"})
    assert response.status_code == 303
    report = await session.scalar(select(Report))
    assert (report.reporter_id, report.target_user_id) == (woman.id, man.id)
    assert f"Жалоба на анкету №{man.profile.number} отправлена" in (await client.get(f"/chats/{conversation.id}")).text


async def test_wali_banner_and_deleted_partner(client, dev_login, couple, make_profile, session):
    man, woman, conversation = couple
    await dev_login(client, man)
    assert 'id="wali-notice" role="status" hidden>' in (await client.get(f"/chats/{conversation.id}")).text
    wali = await make_profile(gender=Gender.MALE)
    link = await guardians.create_invite(session, woman)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], wali.user)
    await session.commit()
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert 'id="wali-notice" role="status"><span>👁 Вали подключён — переписка видна вали.' in page

    await users.delete_account(session, woman)
    await session.commit()
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert "Собеседник удалил аккаунт" in page and 'id="composer"' not in page


# --- Live updates -----------------------------------------------------------------------------


async def _open_stream(app, path: str, user_id: int, csrf: str = "t"):
    """Run the ASGI app for an SSE request; returns (task, chunks queue, disconnect event)."""
    cookie = f"{SESSION_COOKIE}={session_cookie({'uid': user_id, 'csrf': csrf})}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode()), (b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    disconnected = asyncio.Event()
    started: dict[str, object] = {}

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            started.update(message)
        elif message["type"] == "http.response.body" and message.get("body"):
            await chunks.put(message["body"])

    task = asyncio.create_task(app(scope, receive, send))
    return task, chunks, disconnected, started


async def _read_until(chunks: asyncio.Queue[bytes], marker: str, timeout: float = 5.0) -> str:
    received = ""
    async with asyncio.timeout(timeout):
        while marker not in received:
            received += (await chunks.get()).decode()
    return received


@pytest.fixture
async def running_broker(app) -> AsyncIterator[EventBroker]:
    broker: EventBroker = app.state.broker
    listener = EventListener()
    await broker.start(listener)
    await listener.wait_connected(5)
    yield broker
    await broker.stop()


async def test_chat_stream_delivers_new_messages(app, running_broker, couple, session):
    man, woman, conversation = couple
    task, chunks, disconnected, started = await _open_stream(app, f"/chats/{conversation.id}/stream", man.id)
    try:
        assert "event: ready" in await _read_until(chunks, "event: ready")
        assert started["status"] == 200
        headers = dict(started["headers"])
        assert headers[b"content-type"].startswith(b"text/event-stream")
        assert running_broker.subscriber_count == 1

        message = await chat.send_message(session, conversation.id, woman, "Как ваши дела?", MessageOrigin.TELEGRAM)
        await session.commit()
        received = await _read_until(chunks, "event: message")
        assert f'"id":{message.id}' in received
        assert "Как ваши дела" not in received  # only ids travel over the stream

        await chat.close_conversation(session, conversation, woman)
        await session.commit()
        assert "event: closed" in await _read_until(chunks, "event: closed")
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 5)
    assert running_broker.subscriber_count == 0


async def test_chat_stream_delivers_witness_messages(app, running_broker, couple, make_user, session):
    from app.services import witness

    man, _, conversation = couple
    admin = await make_user(is_admin=True)
    task, chunks, disconnected, _ = await _open_stream(app, f"/chats/{conversation.id}/stream", man.id)
    try:
        await _read_until(chunks, "event: ready")
        note = await witness.witness_say(session, admin, conversation.id, "Напоминаю о правилах общения.")
        await session.commit()
        received = await _read_until(chunks, "event: message")
        assert f'"id":{note.id}' in received and "Напоминаю" not in received
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 5)


async def test_open_chat_of_the_partner_learns_about_a_new_wali(app, running_broker, couple, make_user, session):
    man, woman, conversation = couple
    task, chunks, disconnected, _ = await _open_stream(app, f"/chats/{conversation.id}/stream", man.id)
    try:
        await _read_until(chunks, "event: ready")
        link = await guardians.create_invite(session, woman)
        await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], await make_user())
        await session.commit()
        assert "event: wali" in await _read_until(chunks, "event: wali")
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 5)


async def test_events_stream_sends_badge_updates(app, running_broker, make_pair, session):
    from app.services import interests

    man, woman = await make_pair()
    task, chunks, disconnected, _ = await _open_stream(app, "/events", woman.id)
    try:
        await _read_until(chunks, "event: ready")
        await interests.send_interest(session, man, woman.id)
        await session.commit()
        assert "event: badge" in await _read_until(chunks, "event: badge")
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 5)


async def test_stream_of_foreign_chat_is_refused_before_streaming(app, running_broker, couple, make_profile):
    _, _, conversation = couple
    stranger = await make_profile(gender=Gender.FEMALE)
    task, _, disconnected, started = await _open_stream(app, f"/chats/{conversation.id}/stream", stranger.user_id)
    disconnected.set()
    await asyncio.wait_for(task, 5)
    assert started["status"] == 404
    assert running_broker.subscriber_count == 0


async def test_broker_fans_out_by_user_ids():
    broker = EventBroker()
    async with broker.subscribe(1) as first, broker.subscribe(2) as second:
        broker.publish(Event(kind="message.created", outbox_id=1, user_ids=frozenset({1}), data={}))
        assert first.qsize() == 1 and second.qsize() == 0
        broker.publish(Event(kind=LISTENER_RECONNECTED, outbox_id=None, user_ids=frozenset(), data={}))
        assert first.qsize() == 2 and second.qsize() == 1
    assert broker.subscriber_count == 0


async def test_nav_badges_endpoint(client, dev_login, couple, session):
    man, woman, conversation = couple
    await chat.send_message(session, conversation.id, woman, "Салам", MessageOrigin.WEB)
    await session.commit()
    await dev_login(client, man)
    assert (await client.get("/nav/badges")).json() == {"interests": 0, "unread": 1}
