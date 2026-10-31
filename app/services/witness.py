"""The witness (свидетель: махрам/администратор) of every conversation.

Copies of all participant messages reach the witness through ``witness.copy`` events (see
services.chat); these actions are the buttons under such a copy. They are called from a Telegram
group or a private chat, so each one checks the actor's admin rights itself (WitnessOnly).
"""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.enums import ConversationCloseReason, MessageKind, MessageOrigin
from app.models import Conversation, Message, User
from app.schemas import ChatMessageIn, first_error
from app.services import chat, events, guardians, users
from app.services.errors import ConversationClosed, ConversationNotFound, InvalidInput, MessageNotFound, WitnessOnly

WITNESS_FLAG_REASON = "witness"


def _require_witness(actor: User) -> None:
    if not users.is_admin(actor):
        raise WitnessOnly()


async def witness_flag(session: AsyncSession, actor: User, message_id: int) -> Message:
    """«⚠️ Пометить»: flag a participant's message for the moderators (idempotent)."""
    _require_witness(actor)
    message = await session.get(Message, message_id, with_for_update={"key_share": True}, populate_existing=True)
    if message is None or message.kind != MessageKind.USER:
        raise MessageNotFound()
    chat.flag_message(message, WITNESS_FLAG_REASON)
    await session.flush()
    return message


async def witness_say(
    session: AsyncSession,
    actor: User,
    conversation_id: int,
    text: str,
    origin: MessageOrigin = MessageOrigin.TELEGRAM,
) -> Message:
    """«✍️ Написать в чат»: a witness message stored in the conversation (kind 'witness', no sender),
    shown to both participants on the site and in Telegram; also allowed in a closed conversation
    (e.g. to explain why it was closed). Emits ``message.witness``."""
    _require_witness(actor)
    try:
        clean = ChatMessageIn(text=text).text
    except ValidationError as exc:
        raise InvalidInput(first_error(exc)) from exc
    conversation = await session.get(
        Conversation, conversation_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if conversation is None:
        raise ConversationNotFound()
    recipients = [uid for uid in (conversation.user_a_id, conversation.user_b_id) if uid is not None]
    message = Message(
        conversation_id=conversation.id, kind=MessageKind.WITNESS, sender_id=None, text=clean, origin=origin
    )
    session.add(message)
    conversation.last_message_at = utcnow()
    await session.flush()
    links = await guardians.active_guardians_for(session, recipients)
    await events.emit(
        session,
        events.EventKind.MESSAGE_WITNESS,
        {
            "conversation_id": conversation.id,
            "message_id": message.id,
            "recipient_ids": recipients,
            "guardians": [
                {"ward_user_id": ward_id, "guardian_user_id": guardian_id}
                for ward_id, guardian_id in sorted(links.items())
                if guardian_id not in recipients
            ],
        },
        notify_user_ids=recipients,
    )
    return message


async def witness_close(session: AsyncSession, actor: User, conversation_id: int) -> Conversation:
    """«🚫 Закрыть чат»: close with reason 'closed_by_witness' (``closed_by_id`` = the actor). The pair
    cannot open a new chat through interests afterwards (services.interests)."""
    _require_witness(actor)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()
    if not conversation.is_open:
        raise ConversationClosed("Чат уже закрыт.")
    await chat.force_close(session, conversation, ConversationCloseReason.CLOSED_BY_WITNESS, closed_by_id=actor.id)
    return conversation
