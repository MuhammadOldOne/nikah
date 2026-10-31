"""Pure rendering: model objects in, Telegram HTML (and keyboards) out. No I/O, no DB access.

Every user-provided value goes through ``esc`` (``html.escape``). Callers pass objects with the
relationships these functions read already loaded (``User.profile`` etc.).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardMarkup

from app.bot import keyboards
from app.clock import age_from_birth_year, to_moscow
from app.enums import ConversationCloseReason, Gender, ProfileStatus, label
from app.models import Interest, Message, Profile, Report, User
from app.services.chat import ConversationSummary
from app.text import plural

# Telegram allows 4096 characters per message. A full profile card is at most ~3600 characters,
# so cards that carry more text (an interest message, a moderation note) shorten «О себе».
MESSAGE_LIMIT = 4000
HISTORY_ITEM_LIMIT = 600
ABOUT_SHORT_LIMIT = 1500
FLAGS_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


def esc(value: object) -> str:
    """Escape user-provided text for Telegram's HTML parse mode."""
    return escape(str(value), quote=False)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def by_gender(gender: Gender | None, male: str, female: str, neutral: str) -> str:
    if gender is None:
        return neutral
    return male if gender == Gender.MALE else female


def gender_of(user: User | None) -> Gender | None:
    return user.profile.gender if user is not None and user.profile is not None else None


def person_name(user: User | None) -> str:
    """Name shown to other users: the profile name (never the Telegram name), unescaped."""
    if user is None:
        return "Собеседник"
    if user.profile is not None:
        return user.profile.name
    return f"Пользователь #{user.id}"


def person_ref(user: User | None) -> str:
    """«<b>Имя</b> (#id)» in HTML."""
    if user is None:
        return "<b>Собеседник</b>"
    if user.profile is None:
        return f"<b>{esc(person_name(user))}</b>"
    return f"<b>{esc(user.profile.name)}</b> (#{user.id})"


def profile_ref(profile: Profile) -> str:
    """«<b>Имя</b> (#id)» from a profile."""
    return f"<b>{esc(profile.name)}</b> (#{profile.user_id})"


def person_label(user: User | None) -> str:
    """Plain-text «Имя #id» for button labels (buttons are not parsed as HTML)."""
    if user is None:
        return "Собеседник удалил аккаунт"
    return f"{person_name(user)} #{user.id}" if user.profile is not None else person_name(user)


def telegram_name(user: User) -> str:
    """The Telegram name and @username, for people who know each other (ward and wali)."""
    name = esc(user.first_name or f"Пользователь #{user.id}")
    return f"{name} (@{esc(user.username)})" if user.username else name


def age_text(birth_year: int) -> str:
    age = age_from_birth_year(birth_year)
    return f"{age} {plural(age, 'год', 'года', 'лет')}"


def children_text(count: int) -> str:
    if count == 0:
        return "детей нет"
    return f"{count} {plural(count, 'ребёнок', 'ребёнка', 'детей')}"


def msk(value: datetime) -> str:
    return to_moscow(value).strftime("%d.%m.%Y %H:%M")


GUARDIAN_BADGE = "👁 Вали подключён — переписка видна вали"
GUARDIAN_COPY_TITLE = "👁 Копия переписки"


def profile_card(profile: Profile, *, has_guardian: bool = False, about_limit: int | None = None) -> str:
    """The profile as shown in the bot. Without photo by principle: a faceless silhouette."""
    gender = profile.gender
    ident = f" · #{profile.user_id}" if profile.user_id else ""
    lines = [
        f"👤 <b>{esc(profile.name)}</b>, {age_text(profile.birth_year)}{ident}",
        f"📍 {esc(profile.city)}",
        f"💍 {_cap(label(profile.marital_status, gender))} · {children_text(profile.children)}",
        f"🎓 {_cap(label(profile.education))} · 💼 {_cap(label(profile.employment))}",
    ]
    body = [f"{profile.height} см" if profile.height else "", f"{profile.weight} кг" if profile.weight else ""]
    if any(body):
        lines.append("📏 " + " · ".join(part for part in body if part))
    if profile.occupation:
        lines.append(f"🛠 {esc(profile.occupation)}")
    if profile.nationality:
        lines.append(f"🌍 {esc(profile.nationality)}")
    faith = [
        f"мазхаб: {label(profile.madhab)}" if profile.madhab else "",
        f"намаз: {label(profile.prayer)}" if profile.prayer else "",
    ]
    if any(faith):
        lines.append("🕌 " + _cap(" · ".join(part for part in faith if part)))
    plans = [
        f"никах: {label(profile.nikah_timeline)}" if profile.nikah_timeline else "",
        f"переезд: {label(profile.relocation, gender)}" if profile.relocation else "",
    ]
    if any(plans):
        lines.append("💞 " + _cap(" · ".join(part for part in plans if part)))
    wishes_title = by_gender(gender, "Пожелания к супруге", "Пожелания к супругу", "Пожелания к супругу")
    lines += ["", f"<b>{wishes_title}:</b>", esc(profile.wishes)]
    if profile.about:
        about = _shorten(profile.about, about_limit) if about_limit else profile.about
        lines += ["", "<b>О себе:</b>", esc(about)]
    if has_guardian:
        lines += ["", GUARDIAN_BADGE]
    return "\n".join(lines)


def profile_status(profile: Profile) -> str:
    match profile.status:
        case ProfileStatus.ACTIVE:
            return "✅ Анкета опубликована."
        case ProfileStatus.PENDING:
            return "⏳ Анкета на модерации. Пока её не видно в каталоге, но смотреть анкеты уже можно."
        case ProfileStatus.HIDDEN:
            return "🙈 Анкета скрыта: её не видно в каталоге."
        case ProfileStatus.REJECTED:
            reason = f"\nПричина: {esc(profile.moderation_note)}" if profile.moderation_note else ""
            return (
                f"❌ Анкета отклонена модератором.{reason}\nИсправьте её — «✏️ Изменить», и она снова уйдёт на проверку."
            )


def my_profile(profile: Profile) -> str:
    return f"{profile_card(profile, about_limit=ABOUT_SHORT_LIMIT)}\n\n{profile_status(profile)}"


def profile_version(profile: Profile) -> int:
    """Identifies the submitted content an admin card shows (see AdminProfileCb.version)."""
    return int(profile.submitted_at.timestamp()) if profile.submitted_at else 0


def split_blocks(blocks: Iterable[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Join HTML blocks into messages of at most ``limit`` characters without cutting a block."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def chat_history(messages: Sequence[Message], me: User, partner: User | None) -> list[str]:
    """One HTML block per message, long texts shortened (the full text is on the site)."""
    blocks = []
    for message in messages:
        author = "Вы" if message.sender_id == me.id else esc(person_name(partner))
        when = to_moscow(message.created_at).strftime("%d.%m %H:%M")
        blocks.append(f"<b>{author}</b> · <i>{when}</i>\n{esc(_shorten(message.text, HISTORY_ITEM_LIMIT))}")
    return blocks


def chat_header(partner: User | None, *, partner_has_guardian: bool) -> str:
    badge = f"\n{GUARDIAN_BADGE}" if partner_has_guardian else ""
    return f"✍️ Вы пишете: {person_ref(partner)}. Просто отправьте сообщение.\n/stop — выйти из чата{badge}"


def chat_button(summary: ConversationSummary, active_id: int | None) -> str:
    conversation = summary.conversation
    parts = [person_label(summary.partner)]
    if summary.unread_count:
        parts.append(f"· {summary.unread_count} нов.")
    if not conversation.is_open:
        parts.append("🔒")
    elif conversation.id == active_id:
        parts.insert(0, "✍️")
    return " ".join(parts)


def interest_line(interest: Interest, other: User | None) -> str:
    return f"• {person_ref(other)} — {label(interest.status)}"


def outgoing_interests(items: Sequence[Interest]) -> str:
    if not items:
        return "Вы ещё не отправляли откликов. Найти анкеты — «🔍 Смотреть анкеты»."
    lines = ["<b>Ваши отклики:</b>"] + [interest_line(item, item.to_user) for item in items]
    return "\n".join(lines)


def interest_card(interest: Interest, sender_profile: Profile, *, has_guardian: bool) -> str:
    card = profile_card(sender_profile, has_guardian=has_guardian, about_limit=ABOUT_SHORT_LIMIT)
    text = f"💌 <b>Отклик</b>\n\n{card}"
    if interest.message:
        text += f"\n\n✉️ <b>Сообщение:</b>\n{esc(interest.message)}"
    return text


# --- Outbox notifications ------------------------------------------------------------------------


def message_notification(sender: User, message: Message, *, sender_has_guardian: bool = False) -> Rendered:
    badge = f"\n\n{GUARDIAN_BADGE}" if sender_has_guardian else ""
    return Rendered(
        f"💬 {person_ref(sender)}:\n{esc(message.text)}{badge}",
        keyboards.message_notification(message.conversation_id),
    )


def guardian_copy(ward: User, partner: User | None, sender: User, message: Message) -> Rendered:
    """Copy for a wali; no reply buttons: the wali is not a participant."""
    return Rendered(
        f"{GUARDIAN_COPY_TITLE}: {person_ref(ward)} ↔ {person_ref(partner)}\n"
        f"<b>{esc(person_name(sender))}</b>: {esc(message.text)}"
    )


def interest_notification(interest: Interest, sender_profile: Profile, *, has_guardian: bool) -> Rendered:
    return Rendered(
        interest_card(interest, sender_profile, has_guardian=has_guardian),
        keyboards.incoming_interest(interest.id, interest.from_user_id),
    )


def interest_accepted(acceptor: User, conversation_id: int, *, acceptor_has_guardian: bool = False) -> Rendered:
    badge = f"\n{GUARDIAN_BADGE}" if acceptor_has_guardian else ""
    return Rendered(
        f"Ваш отклик принят 🤲 Теперь можно общаться.\nСобеседник: {person_ref(acceptor)}.{badge}",
        keyboards.write(conversation_id),
    )


def interest_declined(decliner: User) -> Rendered:
    return Rendered(
        f"К сожалению, {person_ref(decliner)} "
        f"{by_gender(gender_of(decliner), 'не готов', 'не готова', 'не готов(а)')} продолжить знакомство.\n"
        "Не расстраивайтесь: всё происходит по предопределению. Пусть Аллах облегчит вам поиск 🤲"
    )


def conversation_closed(reason: ConversationCloseReason, partner: User | None) -> Rendered:
    match reason:
        case ConversationCloseReason.CLOSED_BY_USER:
            verb = by_gender(gender_of(partner), "завершил", "завершила", "завершил(а)")
            text = f"🔒 {person_ref(partner)} {verb} общение. Чат закрыт."
        case ConversationCloseReason.USER_BANNED:
            who = by_gender(gender_of(partner), "собеседник", "собеседница", "собеседник")
            verb = by_gender(gender_of(partner), "заблокирован", "заблокирована", "заблокирован(а)")
            text = f"🔒 Чат закрыт: {who} {person_ref(partner)} {verb} модератором."
        case ConversationCloseReason.ACCOUNT_DELETED:
            text = "🔒 Собеседник удалил аккаунт. Чат закрыт."
    return Rendered(text)


def moderation_card(profile: Profile) -> Rendered:
    flags = (
        f"\n⚠️ Стоп-слова: {esc(_shorten(profile.flag_reason, FLAGS_LIMIT))}"
        if profile.flagged and profile.flag_reason
        else ""
    )
    submitted = f"\nОтправлена: {msk(profile.submitted_at)}" if profile.submitted_at else ""
    return Rendered(
        f"🆕 <b>Анкета на модерации</b>{flags}{submitted}\n\n{profile_card(profile)}",
        keyboards.admin_profile(profile.user_id, profile_version(profile)),
    )


def profile_moderated(profile: Profile) -> Rendered:
    if profile.status == ProfileStatus.ACTIVE:
        return Rendered(
            "✅ Ваша анкета прошла модерацию и опубликована. "
            "Теперь её видят в каталоге, и вы можете отправлять отклики: «🔍 Смотреть анкеты»."
        )
    reason = f"\nПричина: {esc(profile.moderation_note)}" if profile.moderation_note else ""
    return Rendered(
        f"❌ Анкета не прошла модерацию.{reason}\n\n"
        "Исправьте её: «📝 Анкета» → «✏️ Изменить», и она снова уйдёт на проверку."
    )


def report_card(report: Report) -> Rendered:
    lines = [
        f"⚠️ <b>Жалоба #{report.id}</b>",
        f"От: {person_ref(report.reporter)}",
        f"На: {person_ref(report.target_user)}",
        f"Причина: {label(report.reason)}",
    ]
    if report.comment:
        lines.append(f"Комментарий: {esc(report.comment)}")
    if report.message is not None:
        lines.append(f"Сообщение: «{esc(_shorten(report.message.text, HISTORY_ITEM_LIMIT))}»")
    return Rendered("\n".join(lines), keyboards.admin_report(report.id))


def guardian_linked_to_partner(ward: User, conversation_id: int) -> Rendered:
    """For the ward's chat partner: from now on their messages in this chat are copied to the wali."""
    return Rendered(
        f"👁 К переписке с {person_ref(ward)} подключён вали: ваши сообщения в этом чате теперь видны вали.",
        keyboards.write(conversation_id),
    )


def guardian_linked(guardian: User) -> Rendered:
    return Rendered(
        f"👁 Вали подключён: {telegram_name(guardian)}.\n"
        "Теперь он получает копии вашей переписки, а собеседники видят значок «Вали подключён».\n"
        "Если это не ваш вали, сразу отключите его: «⚙️ Настройки» → «Отключить вали»."
    )
