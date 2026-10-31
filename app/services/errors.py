"""Typed domain errors. ``str(error)`` / ``error.message`` is a Russian, user-facing text that
the website and the bot show as is, so both channels report identical messages.

Services raise these *before* writing anything, so a caller that catches one may still commit.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for business-rule violations."""

    default_message = "Действие недоступно."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidInput(DomainError):
    """Input failed validation; the message comes from app.schemas."""

    default_message = "Проверьте введённые данные."


class NotFound(DomainError):
    default_message = "Не найдено."


class PermissionDenied(DomainError):
    default_message = "Недостаточно прав."


class UserNotFound(NotFound):
    default_message = "Пользователь не найден."


class UserBanned(DomainError):
    default_message = "Ваш аккаунт заблокирован модератором."


class AdminBanDenied(DomainError):
    default_message = (
        "Администратора нельзя заблокировать. Сначала снимите права: "
        "scripts/make_admin.py --revoke или уберите его из ADMIN_TELEGRAM_IDS."
    )


class ConsentRequired(DomainError):
    default_message = "Нужно дать согласие на обработку персональных данных."


class ProfileRequired(DomainError):
    default_message = "Сначала заполните анкету."


class ProfileAlreadyExists(DomainError):
    default_message = "У вас уже есть анкета."


class ProfileSubmissionLimitReached(DomainError):
    default_message = "Анкету слишком часто отправляли на проверку. Сохранить изменения можно будет через час."


class ProfileNotFound(NotFound):
    default_message = "Анкета не найдена или недоступна."


class ProfileUnderReview(ProfileNotFound):
    """A counterpart's profile whose current content is not approved (edited and re-submitted, or
    rejected by a moderator): its content is not shown to anyone but the owner."""

    default_message = "Анкета временно недоступна: её проверяет модератор."


class ProfileNotActive(DomainError):
    """The user's own profile is not published. With PREMODERATION on, browsing the catalog,
    opening other profiles (except counterparts) and sending interests need an ACTIVE profile; the
    subclasses say why it is not active."""

    default_message = "Отклики можно отправлять после того, как ваша анкета будет опубликована."


class ProfilePending(ProfileNotActive):
    default_message = "Ваша анкета ещё не одобрена. Смотреть анкеты и отправлять отклики можно после модерации."


class ProfileRejected(ProfileNotActive):
    default_message = (
        "Ваша анкета отклонена модератором. Исправьте её — после проверки откроются просмотр анкет и отклики."
    )


class ProfileHidden(ProfileNotActive):
    default_message = "Ваша анкета скрыта. Покажите её снова, чтобы смотреть анкеты и отправлять отклики."


class ProfileStateError(DomainError):
    default_message = "Это действие недоступно для анкеты в текущем статусе."


class InterestToSelf(DomainError):
    default_message = "Нельзя откликнуться на свою анкету."


class InterestAlreadyExists(DomainError):
    default_message = "Между вами уже есть отклик или переписка."


class InterestSentBefore(DomainError):
    """The sender's earlier interest to this person was accepted (or declined): one per direction,
    as in the legacy bot; only a withdrawn (cancelled) interest may be sent again."""

    default_message = "Вы уже отправляли отклик этой анкете. Повторный отклик невозможен."


class InterestDeclinedBefore(InterestSentBefore):
    default_message = "Этот человек уже отклонил ваш отклик."


class InterestBlockedByWitness(DomainError):
    default_message = "Свидетель завершил ваш чат с этой анкетой, новый отклик невозможен. Вопросы — к админу."


class InterestLimitReached(DomainError):
    default_message = "Лимит откликов на сегодня исчерпан. Попробуйте завтра."


class InterestNotFound(NotFound):
    default_message = "Отклик не найден."


class InterestNotPending(DomainError):
    default_message = "На этот отклик уже ответили или его отозвали."


class OpenConversationLimit(DomainError):
    default_message = "У одного из участников уже есть открытый чат. Завершите его сначала."


class MessageRateLimited(DomainError):
    default_message = "Вы отправляете сообщения слишком часто. Подождите минуту и попробуйте снова."


class ConversationNotFound(NotFound):
    default_message = "Чат не найден."


class ConversationClosed(DomainError):
    default_message = "Общение в этом чате завершено."


class PartnerDeleted(ConversationClosed):
    default_message = "Собеседник удалил аккаунт."


class MessageNotFound(NotFound):
    default_message = "Сообщение не найдено."


class WitnessOnly(PermissionDenied):
    default_message = "Только для свидетеля."


class TokenInvalid(DomainError):
    default_message = "Ссылка недействительна или устарела. Попробуйте ещё раз."


class GuardianInviteInvalid(DomainError):
    default_message = "Приглашение недействительно или устарело. Попросите новую ссылку."


class GuardianSelf(DomainError):
    default_message = "Нельзя стать вали для самого себя."


class ReportToSelf(DomainError):
    default_message = "Нельзя пожаловаться на себя."


class AlreadyReported(DomainError):
    default_message = "Вы уже отправили жалобу на этого пользователя. Модератор её рассмотрит."


class ReportLimitReached(DomainError):
    default_message = "Вы отправили много жалоб за сутки. Модераторы их рассмотрят, новую можно будет отправить позже."


class ReportNotFound(NotFound):
    default_message = "Жалоба не найдена."


class ReportAlreadyResolved(DomainError):
    default_message = "Жалоба уже рассмотрена."


class LegacyImportDone(DomainError):
    default_message = "Перенос данных старого бота уже выполнен: повторный перенос невозможен."
