"""Chats (/chats, /chats/{id}), the messages partial and the per-chat SSE stream."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.clock import to_moscow, utcnow
from app.enums import MessageOrigin
from app.models import Conversation, User
from app.schemas import CHAT_MESSAGE_MAX, REPORT_COMMENT_MAX
from app.services import chat, guardians
from app.services.errors import ConversationNotFound, DomainError
from app.services.events import Event, EventKind
from app.web.deps import DB_ID_MAX, IdPath, SessionDep, UserDep, flash, is_partial
from app.web.render import render, render_partial, toast_response
from app.web.reports import submit_report
from app.web.sse import sse_response

router = APIRouter()

PAGE_SIZE = 50


async def _conversation(session: SessionDep, conversation_id: int, user: User) -> Conversation:
    try:
        return await chat.get_conversation_for_participant(session, conversation_id, user)
    except ConversationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc


def _partner(conversation: Conversation, user: User) -> User | None:
    partner_id = conversation.partner_id(user.id)
    if partner_id is None:
        return None
    return conversation.user_a if conversation.user_a_id == partner_id else conversation.user_b


def _messages_context(user: User, messages: list[Any]) -> dict[str, Any]:
    return {"messages": messages, "me": user, "today": to_moscow(utcnow()).date()}


def _back_to_chat(conversation_id: int) -> RedirectResponse:
    return RedirectResponse(f"/chats/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/chats")
async def chat_list(request: Request, session: SessionDep, user: UserDep) -> Response:
    summaries = await chat.list_conversations(session, user)
    return await render(request, "chats/index.html", {"summaries": summaries}, session=session, user=user)


@router.get("/chats/{conversation_id:int}")
async def chat_page(request: Request, session: SessionDep, user: UserDep, conversation_id: IdPath) -> Response:
    conversation = await _conversation(session, conversation_id, user)
    messages = await chat.list_messages(session, conversation, limit=PAGE_SIZE)
    await chat.mark_read(session, conversation, user)
    partner = _partner(conversation, user)
    participants = [uid for uid in (conversation.user_a_id, conversation.user_b_id) if uid is not None]
    return await render(
        request,
        "chats/view.html",
        {
            "conversation": conversation,
            "partner": partner,
            "partner_profile": partner.profile if partner else None,
            "wali_watching": bool(await guardians.users_with_active_guardian(session, participants)),
            "has_more": len(messages) == PAGE_SIZE,
            "message_max": CHAT_MESSAGE_MAX,
            "comment_max": REPORT_COMMENT_MAX,
            **_messages_context(user, messages),
        },
        session=session,
        user=user,
    )


@router.get("/chats/{conversation_id:int}/messages")
async def messages_partial(
    request: Request,
    session: SessionDep,
    user: UserDep,
    conversation_id: IdPath,
    after_id: Annotated[int | None, Query(ge=0, le=DB_ID_MAX)] = None,
    before_id: Annotated[int | None, Query(ge=1, le=DB_ID_MAX)] = None,
    read: bool = False,
) -> Response:
    """New messages after ``after_id`` (live updates, resync) or older ones before ``before_id``."""
    conversation = await _conversation(session, conversation_id, user)
    messages = await chat.list_messages(
        session, conversation, after_id=after_id, before_id=None if after_id is not None else before_id, limit=PAGE_SIZE
    )
    if read:
        await chat.mark_read(session, conversation, user)
    response = render_partial(request, "chats/_messages.html", _messages_context(user, messages))
    response.headers["X-More"] = "1" if len(messages) == PAGE_SIZE else "0"
    return response


@router.post("/chats/{conversation_id:int}/messages")
async def send_message(request: Request, session: SessionDep, user: UserDep, conversation_id: IdPath) -> Response:
    form = await request.form()
    text = form.get("text")
    try:
        await chat.send_message(
            session, conversation_id, user, text if isinstance(text, str) else "", MessageOrigin.WEB
        )
    except ConversationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    except DomainError as exc:
        if is_partial(request):
            return toast_response(exc.message, status_code=422)
        flash(request, exc.message, "error")
        return _back_to_chat(conversation_id)
    if not is_partial(request):
        return _back_to_chat(conversation_id)
    try:
        after_id = min(max(int(str(form.get("after_id") or 0)), 0), DB_ID_MAX)
    except ValueError:
        after_id = 0
    conversation = await _conversation(session, conversation_id, user)
    messages = await chat.list_messages(session, conversation, after_id=after_id, limit=PAGE_SIZE)
    return render_partial(request, "chats/_messages.html", _messages_context(user, messages), status_code=201)


@router.post("/chats/{conversation_id:int}/close")
async def close(request: Request, session: SessionDep, user: UserDep, conversation_id: IdPath) -> Response:
    conversation = await _conversation(session, conversation_id, user)
    try:
        await chat.close_conversation(session, conversation, user)
    except DomainError as exc:
        flash(request, exc.message, "error")
    else:
        flash(request, "Общение завершено. Собеседник получит уведомление.", "info")
    return _back_to_chat(conversation_id)


@router.post("/chats/{conversation_id:int}/report")
async def report(request: Request, session: SessionDep, user: UserDep, conversation_id: IdPath) -> Response:
    conversation = await _conversation(session, conversation_id, user)
    partner_id = conversation.partner_id(user.id)
    if partner_id is None:
        flash(request, "Собеседник удалил аккаунт.", "error")
    elif await submit_report(request, session, user, partner_id):
        flash(request, "Жалоба отправлена модератору. Спасибо, что помогаете сохранять порядок.", "success")
    return _back_to_chat(conversation_id)


@router.get("/chats/{conversation_id:int}/stream")
async def stream(request: Request, session: SessionDep, user: UserDep, conversation_id: IdPath) -> StreamingResponse:
    """SSE for an open chat page: ``message`` / ``closed`` for this chat, ``wali`` when one of its
    participants links a wali (the page shows the notice at once), ``badge`` for the rest."""
    conversation = await _conversation(session, conversation_id, user)
    participants = {conversation.user_a_id, conversation.user_b_id} - {None}

    def classify(event: Event) -> tuple[str, dict[str, Any]] | None:
        if event.data.get("conversation_id") == conversation_id:
            if event.kind == EventKind.MESSAGE_CREATED:
                return "message", {"id": event.data.get("message_id")}
            if event.kind == EventKind.CONVERSATION_CLOSED:
                return "closed", {}
        if event.kind == EventKind.GUARDIAN_LINKED and event.data.get("ward_user_id") in participants:
            return "wali", {}
        return "badge", {}

    return sse_response(request, user.id, classify)
