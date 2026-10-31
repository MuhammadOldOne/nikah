"""Enumerations shared by the database, web and bot, with Russian labels.

Values (lowercase English) are stored in the database and used in URLs/callback data.
Use ``label(value, gender)`` for display: some labels depend on the profile owner's gender.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"

    @property
    def opposite(self) -> Gender:
        return Gender.FEMALE if self is Gender.MALE else Gender.MALE


class MaritalStatus(StrEnum):
    NEVER_MARRIED = "never_married"
    DIVORCED = "divorced"
    WIDOWED = "widowed"


class Education(StrEnum):
    SECONDARY = "secondary"
    VOCATIONAL = "vocational"
    INCOMPLETE_HIGHER = "incomplete_higher"
    HIGHER = "higher"
    ACADEMIC_DEGREE = "academic_degree"
    RELIGIOUS = "religious"


class Employment(StrEnum):
    EMPLOYED = "employed"
    BUSINESS = "business"
    STUDENT = "student"
    UNEMPLOYED = "unemployed"


class Madhab(StrEnum):
    HANAFI = "hanafi"
    SHAFII = "shafii"
    MALIKI = "maliki"
    HANBALI = "hanbali"
    UNSPECIFIED = "unspecified"


class Prayer(StrEnum):
    ALWAYS = "always"
    MOSTLY = "mostly"
    SOMETIMES = "sometimes"
    LEARNING = "learning"


class NikahTimeline(StrEnum):
    ASAP = "asap"
    SIX_MONTHS = "six_months"
    YEAR = "year"
    UNDECIDED = "undecided"


class Relocation(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    NEGOTIABLE = "negotiable"


class ProfileStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    HIDDEN = "hidden"
    REJECTED = "rejected"


class InterestStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class ReportReason(StrEnum):
    FRAUD = "fraud"
    MARRIED = "married"
    FAKE = "fake"
    INSULT = "insult"
    INDECENT = "indecent"
    OTHER = "other"


class ReportStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReportAction(StrEnum):
    DISMISS = "dismiss"
    HIDE_PROFILE = "hide_profile"
    BAN_USER = "ban_user"


class GuardianStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class LoginTokenKind(StrEnum):
    LOGIN = "login"  # deep link t.me/<bot>?start=login_<token>, confirmed in the bot, polled by the site
    MAGIC = "magic"  # one-time link {SITE_URL}/auth/magic/<token> issued by the bot


class LoginTokenState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CONSUMED = "consumed"
    EXPIRED = "expired"  # also returned for unknown tokens


class MessageOrigin(StrEnum):
    WEB = "web"
    TELEGRAM = "telegram"


class ConversationCloseReason(StrEnum):
    CLOSED_BY_USER = "closed_by_user"
    ACCOUNT_DELETED = "account_deleted"
    USER_BANNED = "user_banned"


class GenderedLabel(NamedTuple):
    male: str
    female: str
    neutral: str


_LABELS: dict[type[StrEnum], dict[StrEnum, str | GenderedLabel]] = {
    Gender: {
        Gender.MALE: "мужчина",
        Gender.FEMALE: "женщина",
    },
    MaritalStatus: {
        MaritalStatus.NEVER_MARRIED: GenderedLabel("не был женат", "не была замужем", "не был(а) в браке"),
        MaritalStatus.DIVORCED: "в разводе",
        MaritalStatus.WIDOWED: GenderedLabel("вдовец", "вдова", "вдовец / вдова"),
    },
    Education: {
        Education.SECONDARY: "среднее",
        Education.VOCATIONAL: "среднее специальное",
        Education.INCOMPLETE_HIGHER: "неоконченное высшее",
        Education.HIGHER: "высшее",
        Education.ACADEMIC_DEGREE: "учёная степень",
        Education.RELIGIOUS: "религиозное",
    },
    Employment: {
        Employment.EMPLOYED: "работаю",
        Employment.BUSINESS: "свой бизнес",
        Employment.STUDENT: "учусь",
        Employment.UNEMPLOYED: "не работаю",
    },
    Madhab: {
        Madhab.HANAFI: "ханафитский",
        Madhab.SHAFII: "шафиитский",
        Madhab.MALIKI: "маликитский",
        Madhab.HANBALI: "ханбалитский",
        Madhab.UNSPECIFIED: "не указан",
    },
    Prayer: {
        Prayer.ALWAYS: "соблюдаю всегда",
        Prayer.MOSTLY: "почти всегда",
        Prayer.SOMETIMES: "иногда",
        Prayer.LEARNING: "учусь",
    },
    NikahTimeline: {
        NikahTimeline.ASAP: "как можно скорее",
        NikahTimeline.SIX_MONTHS: "в течение 6 месяцев",
        NikahTimeline.YEAR: "в течение года",
        NikahTimeline.UNDECIDED: "пока не знаю",
    },
    Relocation: {
        Relocation.READY: GenderedLabel("готов", "готова", "готов(а)"),
        Relocation.NOT_READY: GenderedLabel("не готов", "не готова", "не готов(а)"),
        Relocation.NEGOTIABLE: "обсуждаемо",
    },
    ProfileStatus: {
        ProfileStatus.PENDING: "на модерации",
        ProfileStatus.ACTIVE: "опубликована",
        ProfileStatus.HIDDEN: "скрыта",
        ProfileStatus.REJECTED: "отклонена",
    },
    InterestStatus: {
        InterestStatus.PENDING: "ожидает ответа",
        InterestStatus.ACCEPTED: "принят",
        InterestStatus.DECLINED: "отклонён",
        InterestStatus.CANCELLED: "отозван",
    },
    ReportReason: {
        ReportReason.FRAUD: "мошенничество, просьбы о деньгах",
        ReportReason.MARRIED: "не свободен для брака, многожёнство",
        ReportReason.FAKE: "фейковая анкета",
        ReportReason.INSULT: "оскорбления, грубость",
        ReportReason.INDECENT: "непристойное поведение",
        ReportReason.OTHER: "другое",
    },
    ReportStatus: {
        ReportStatus.OPEN: "открыта",
        ReportStatus.RESOLVED: "рассмотрена",
    },
    ReportAction: {
        ReportAction.DISMISS: "жалоба отклонена",
        ReportAction.HIDE_PROFILE: "анкета скрыта",
        ReportAction.BAN_USER: "пользователь заблокирован",
    },
    GuardianStatus: {
        GuardianStatus.PENDING: "ожидает подтверждения",
        GuardianStatus.ACTIVE: "подключён",
        GuardianStatus.REVOKED: "отключён",
    },
    LoginTokenKind: {
        LoginTokenKind.LOGIN: "вход через бота",
        LoginTokenKind.MAGIC: "ссылка из бота",
    },
    LoginTokenState: {
        LoginTokenState.PENDING: "ожидает подтверждения",
        LoginTokenState.CONFIRMED: "подтверждён",
        LoginTokenState.CONSUMED: "использован",
        LoginTokenState.EXPIRED: "истёк",
    },
    MessageOrigin: {
        MessageOrigin.WEB: "сайт",
        MessageOrigin.TELEGRAM: "Telegram",
    },
    ConversationCloseReason: {
        ConversationCloseReason.CLOSED_BY_USER: "общение завершено",
        ConversationCloseReason.ACCOUNT_DELETED: "собеседник удалил аккаунт",
        ConversationCloseReason.USER_BANNED: "пользователь заблокирован",
    },
}


def label(value: StrEnum | None, gender: Gender | str | None = None) -> str:
    """Russian label for an enum member; gender-aware where the wording differs.

    ``gender`` is the gender of the person the value describes (the profile owner).
    Returns an empty string for None so templates can call it on optional fields.
    """
    if value is None:
        return ""
    text = _LABELS[type(value)][value]
    if isinstance(text, str):
        return text
    if gender is None:
        return text.neutral
    return text.male if Gender(gender) is Gender.MALE else text.female


def choices(enum_cls: type[StrEnum], gender: Gender | str | None = None) -> list[tuple[str, str]]:
    """(value, label) pairs in declaration order, for form selects and bot keyboards."""
    return [(member.value, label(member, gender)) for member in enum_cls]
