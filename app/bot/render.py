"""Pure rendering: model objects in, Telegram HTML (and keyboards) out. No I/O, no DB access.

Every user-provided value goes through ``esc`` (``html.escape``). Callers pass objects with the
relationships these functions read already loaded (``User.profile`` etc.). People are shown by
profile number only («Анкета №512»), never by Telegram name, username or id.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardMarkup

from app.bot import keyboards, texts
from app.clock import age_from_birth_year, msk_day_time, to_moscow
from app.enums import ConversationCloseReason, Gender, ProfileStatus, label
from app.models import Conversation, Interest, Message, Profile, Report, User
from app.schemas import FIELD_LABELS
from app.services.chat import ConversationSummary
from app.text import gender_word, profile_title

# Telegram allows 4096 characters per message. A full profile card is at most ~3700 characters,
# so cards that carry more text (an interest message, a moderation note) shorten «О себе».
MESSAGE_LIMIT = 4000
HISTORY_ITEM_LIMIT = 600
ABOUT_SHORT_LIMIT = 1500
FLAGS_LIMIT = 200
UNKNOWN = "не указано"


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
    """Name shown to other users: «Анкета №N» (never the Telegram name), unescaped."""
    if user is None:
        return "Собеседник"
    if user.profile is not None:
        return profile_title(user.profile)
    return "Пользователь без анкеты"


def person_ref(user: User | None) -> str:
    """«<b>Анкета №N</b>» in HTML."""
    if user is None:
        return "<b>Собеседник</b>"
    return f"<b>{esc(person_name(user))}</b>"


def profile_ref(profile: Profile) -> str:
    """«<b>Анкета №N</b>» from a profile."""
    return f"<b>{profile_title(profile)}</b>"


def person_label(user: User | None) -> str:
    """Plain-text «Анкета №N» for button labels (buttons are not parsed as HTML)."""
    if user is None:
        return "Собеседник удалил аккаунт"
    return person_name(user)


def number_ref(user: User | None) -> str:
    """«№512» for the witness's compact lines."""
    if user is None:
        return "аккаунт удалён"
    if user.profile is None:
        return "без анкеты"
    return f"№{user.profile.number}"


def telegram_name(user: User) -> str:
    """The Telegram name and @username, for people who know each other (ward and wali)."""
    name = esc(user.first_name or "Пользователь Telegram")
    return f"{name} (@{esc(user.username)})" if user.username else name


def msk(value: datetime) -> str:
    return to_moscow(value).strftime("%d.%m.%Y %H:%M")


GUARDIAN_BADGE = "👁 Вали подключён — переписка видна вали"
GUARDIAN_COPY_TITLE = "👁 Копия переписки"


def _choice(value: object, gender: Gender) -> str:
    return _cap(label(value, gender)) if value is not None else UNKNOWN  # type: ignore[arg-type]


def children_text(count: int | None) -> str:
    if count is None:
        return UNKNOWN
    return "нет" if count == 0 else str(count)


def profile_card(profile: Profile, *, has_guardian: bool = False, about_limit: int | None = None) -> str:
    """The legacy card: «<b>Анкета №N</b> — Брат/Сестра», then the fields line by line («не
    указано» for empty ones; optional extras only when filled), «О себе», «Пожелания к партнёру»."""
    gender = profile.gender
    lines = [
        f"<b>{profile_title(profile)}</b> — {gender_word(gender)}",
        f"Возраст: {age_from_birth_year(profile.birth_year)}",
        f"Город: {esc(profile.city)}",
        f"Национальность: {esc(profile.nationality) if profile.nationality else UNKNOWN}",
        f"Семейное положение: {_choice(profile.marital_status, gender)}",
        f"Дети: {children_text(profile.children)}",
        f"Образование: {_choice(profile.education, gender)}",
        f"Занятость: {_choice(profile.employment, gender)}",
    ]
    extras = (
        ("Рост", f"{profile.height} см" if profile.height else None),
        ("Вес", f"{profile.weight} кг" if profile.weight else None),
        ("Род занятий", esc(profile.occupation) if profile.occupation else None),
        ("Мазхаб", _choice(profile.madhab, gender) if profile.madhab else None),
        ("Намаз", _choice(profile.prayer, gender) if profile.prayer else None),
        ("Никах", _choice(profile.nikah_timeline, gender) if profile.nikah_timeline else None),
        ("Переезд", _choice(profile.relocation, gender) if profile.relocation else None),
    )
    lines += [f"{title}: {value}" for title, value in extras if value]
    about = profile.about or ""
    if about_limit:
        about = _shorten(about, about_limit)
    lines += [
        "",
        "<b>О себе:</b>",
        esc(about) if about else UNKNOWN,
        "",
        "<b>Пожелания к партнёру:</b>",
        esc(profile.wishes) if profile.wishes else UNKNOWN,
    ]
    if has_guardian:
        lines += ["", GUARDIAN_BADGE]
    return "\n".join(lines)


def missing_note(missing: Sequence[str], status: ProfileStatus) -> str:
    """The «Дополнить анкету» invitation for a profile that lacks required fields. Choice edits keep
    a published (or hidden) profile as it is; any edit sends a rejected one back to moderation."""
    names = ", ".join(FIELD_LABELS[name].lower() for name in missing)
    match status:
        case ProfileStatus.ACTIVE | ProfileStatus.HIDDEN:
            moderation = ", повторная модерация не нужна."
        case ProfileStatus.PENDING:
            moderation = ". Анкета пока на модерации."
        case _:
            moderation = ". После этого анкета снова уйдёт на модерацию."
    return (
        f"В вашей анкете не заполнено: {names}. Пока это так, у других людей там написано «{UNKNOWN}».\n"
        f"Дополните анкету — это займёт 1 минуту, каждое поле — одна кнопка{moderation}"
    )


def profile_completed(status: ProfileStatus) -> str:
    """The thanks after «Дополнить анкету», by the status the profile has now."""
    if status == ProfileStatus.ACTIVE:
        return "Спасибо! Анкета дополнена, модерация не нужна. Барака Аллаху фикум."
    if status == ProfileStatus.PENDING:
        return "Спасибо! Анкета дополнена. Она на модерации — мы сообщим, когда её одобрят."
    return "Спасибо! Анкета дополнена. Барака Аллаху фикум."


def profile_status(profile: Profile) -> str:
    status = f"<i>Статус: {label(profile.status)}</i>"
    match profile.status:
        case ProfileStatus.PENDING:
            return f"{status}\nПока анкета на модерации, смотреть анкеты и отправлять отклики нельзя."
        case ProfileStatus.HIDDEN:
            return f"{status}\nАнкету не видно в каталоге. Покажите её, когда будете готовы."
        case ProfileStatus.REJECTED:
            reason = f"\nПричина: {esc(profile.moderation_note)}" if profile.moderation_note else ""
            return f"{status}{reason}\nИсправьте анкету — «Изменить», и она снова уйдёт на проверку."
        case _:
            return status


def my_profile(profile: Profile, missing: Sequence[str] = ()) -> str:
    text = f"{profile_card(profile, about_limit=ABOUT_SHORT_LIMIT)}\n\n{profile_status(profile)}"
    return f"{text}\n\n{missing_note(missing, profile.status)}" if missing else text


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


# --- Chats -------------------------------------------------------------------------------------


def chat_history(messages: Sequence[Message], me: User, partner: User | None) -> list[str]:
    """One HTML block per message, long texts shortened (the full text is on the site)."""
    blocks = []
    for message in messages:
        if message.is_witness:
            author = "Свидетель"
        else:
            author = "Вы" if message.sender_id == me.id else esc(person_name(partner))
        when = msk_day_time(message.created_at)
        blocks.append(f"<b>{author}</b> · <i>{when}</i>\n{esc(_shorten(message.text, HISTORY_ITEM_LIMIT))}")
    return blocks


def my_chat_footer(partner: User | None, *, partner_has_guardian: bool) -> str:
    """The legacy «Мой чат» line for the only open chat."""
    badge = f"\n{GUARDIAN_BADGE}" if partner_has_guardian else ""
    return f"Открыт чат с анкетой {number_ref(partner)}. Пишите сюда — бот передаст.{badge}"


def chat_header(partner: User | None, *, partner_has_guardian: bool) -> str:
    """The chat chosen among several open ones."""
    badge = f"\n{GUARDIAN_BADGE}" if partner_has_guardian else ""
    return f"Вы пишете: {person_ref(partner)}. Просто отправьте сообщение.\n/stop — выйти из чата{badge}"


def chat_button(summary: ConversationSummary, active_id: int | None) -> str:
    parts = [person_label(summary.partner)]
    if summary.unread_count:
        parts.append(f"· {summary.unread_count} нов.")
    if summary.conversation.id == active_id:
        parts.insert(0, "✍️")
    return " ".join(parts)


def chat_opened(
    first: Profile, second: Profile, conversation_id: int, *, several_allowed: bool, badge: bool
) -> Rendered:
    """The legacy notice to both sides when an interest is accepted."""
    how = (
        "Нажмите «Написать», чтобы выбрать этот чат, — затем просто пишите сюда текстом."
        if several_allowed
        else texts.WRITE_HERE
    )
    guardian = f"\n\n{GUARDIAN_BADGE}" if badge else ""
    return Rendered(
        f"<b>Чат открыт между анкетами №{first.number} и №{second.number}.</b>\n{texts.CHAT_RULES}\n\n{how}{guardian}",
        keyboards.chat_opened(conversation_id, several_allowed=several_allowed),
    )


# --- Interests ------------------------------------------------------------------------------------


def interest_line(interest: Interest, other: User | None) -> str:
    return f"• {person_ref(other)} — {label(interest.status)}"


def outgoing_interests(items: Sequence[Interest]) -> str:
    if not items:
        return "Вы ещё не отправляли откликов. Найти анкеты — «Смотреть анкеты»."
    return "\n".join(["<b>Ваши отклики:</b>"] + [interest_line(item, item.to_user) for item in items])


def interest_card(interest: Interest, sender_profile: Profile, *, has_guardian: bool) -> str:
    """The legacy «Вами заинтересовалась анкета №N» card, with the optional message."""
    card = profile_card(sender_profile, has_guardian=has_guardian, about_limit=ABOUT_SHORT_LIMIT)
    text = f"<b>Вами заинтересовалась анкета №{sender_profile.number}</b>\n\n{card}"
    if interest.message:
        text += f"\n\n<b>Сообщение:</b>\n{esc(interest.message)}"
    return f"{text}\n\n{texts.INTEREST_QUESTION}"


# --- Outbox notifications ------------------------------------------------------------------------


def message_notification(
    sender: User, message: Message, *, with_reply: bool = False, sender_has_guardian: bool = False
) -> Rendered:
    """A partner's message, in the legacy format «<b>Анкета №N пишет:</b>»."""
    badge = f"\n\n{GUARDIAN_BADGE}" if sender_has_guardian else ""
    return Rendered(
        f"<b>{esc(person_name(sender))} пишет:</b>\n{esc(message.text)}{badge}",
        keyboards.relayed_message(message.id, message.conversation_id, with_reply=with_reply),
    )


def witness_message(message: Message) -> Rendered:
    """The witness wrote to both participants (legacy format)."""
    return Rendered(f"<b>Сообщение от свидетеля:</b>\n{esc(message.text)}")


def guardian_copy(ward: User, partner: User | None, sender: User | None, message: Message) -> Rendered:
    """Copy for a wali (``sender`` None = the witness); no buttons: the wali is not a participant."""
    author = "Свидетель" if sender is None else esc(person_name(sender))
    return Rendered(
        f"{GUARDIAN_COPY_TITLE}: {person_ref(ward)} ↔ {person_ref(partner)}\n<b>{author}</b>: {esc(message.text)}"
    )


def interest_notification(interest: Interest, sender_profile: Profile, *, has_guardian: bool) -> Rendered:
    return Rendered(
        interest_card(interest, sender_profile, has_guardian=has_guardian), keyboards.interest_decision(interest.id)
    )


def _profile_subject(user: User | None) -> str | None:
    """«Анкета №N» as the subject of a sentence (feminine verbs agree with «анкета»)."""
    return profile_title(user.profile) if user is not None and user.profile is not None else None


def interest_declined(decliner: User) -> Rendered:
    subject = _profile_subject(decliner)
    text = f"{subject} отклонила ваш отклик." if subject else "Ваш отклик отклонён."
    return Rendered(f"{text} Не отчаивайтесь, ин ша Аллах.")


def conversation_closed(reason: ConversationCloseReason, partner: User | None) -> Rendered:
    match reason:
        case ConversationCloseReason.CLOSED_BY_USER:
            subject = _profile_subject(partner)
            text = f"{subject} завершила общение." if subject else "Собеседник завершил общение."
        case ConversationCloseReason.USER_BANNED:
            who = by_gender(gender_of(partner), "собеседник", "собеседница", "собеседник")
            verb = by_gender(gender_of(partner), "заблокирован", "заблокирована", "заблокирован(а)")
            text = f"Чат закрыт: {who} {person_ref(partner)} {verb} модератором."
        case ConversationCloseReason.ACCOUNT_DELETED:
            text = "Собеседник удалил аккаунт. Чат закрыт."
        case ConversationCloseReason.CLOSED_BY_WITNESS:
            text = "Свидетель завершил ваш чат. Если есть вопросы — обратитесь к админу."
    return Rendered(text)


def moderation_card(profile: Profile) -> Rendered:
    flags = (
        f"\n⚠️ Стоп-слова: {esc(_shorten(profile.flag_reason, FLAGS_LIMIT))}"
        if profile.flagged and profile.flag_reason
        else ""
    )
    submitted = f"\nОтправлена: {msk(profile.submitted_at)}" if profile.submitted_at else ""
    return Rendered(
        f"<b>Новая анкета на модерацию</b>{flags}{submitted}\n\n{profile_card(profile)}",
        keyboards.admin_profile(profile.user_id, profile_version(profile)),
    )


def profile_moderated(profile: Profile) -> Rendered:
    if profile.status == ProfileStatus.ACTIVE:
        return Rendered(
            f"Ваша анкета №{profile.number} одобрена. Теперь вы можете смотреть анкеты и отправлять отклики."
        )
    reason = f"\nПричина: {esc(profile.moderation_note)}" if profile.moderation_note else ""
    return Rendered(
        f"К сожалению, ваша анкета №{profile.number} отклонена.{reason}\n\n"
        "Исправьте её: «Моя анкета» → «Изменить», и она снова уйдёт на проверку. Вопросы — к админу."
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
        "Если это не ваш вали, сразу отключите его: «Настройки» → «Отключить вали»."
    )


# --- The witness (WITNESS_CHAT_ID or each admin) ----------------------------------------------------


def witness_copy(conversation_id: int, message: Message, sender: User | None, recipient: User | None) -> Rendered:
    """«Чат #id | №A → №B (сайт/Telegram)» and the stored text (after the contact filter)."""
    return Rendered(
        f"Чат #{conversation_id} | {number_ref(sender)} → {number_ref(recipient)} ({label(message.origin)})\n"
        f"{esc(message.text)}",
        keyboards.witness_copy(message.id, conversation_id),
    )


def witness_conversation_opened(conversation: Conversation, a: User | None, b: User | None) -> Rendered:
    return Rendered(f"Открыт чат #{conversation.id}: {number_ref(a)} ↔ {number_ref(b)}.")


def witness_conversation_closed(
    conversation: Conversation, reason: ConversationCloseReason, a: User | None, b: User | None
) -> Rendered:
    return Rendered(f"Чат #{conversation.id} закрыт: {label(reason)} ({number_ref(a)} ↔ {number_ref(b)}).")


def witness_report(report: Report) -> Rendered:
    if report.message is None:
        head = f"⚠️ Жалоба на анкету {number_ref(report.target_user)} от {number_ref(report.reporter)}"
        lines = [head, f"Причина: {label(report.reason)}"]
    else:
        head = f"⚠️ Жалоба на сообщение #{report.message.id} в чате #{report.message.conversation_id}"
        lines = [
            head,
            f"От: {number_ref(report.reporter)}, на: {number_ref(report.target_user)}",
            f"«{esc(_shorten(report.message.text, HISTORY_ITEM_LIMIT))}»",
        ]
    if report.comment:
        lines.append(f"Комментарий: {esc(report.comment)}")
    return Rendered("\n".join(lines))
