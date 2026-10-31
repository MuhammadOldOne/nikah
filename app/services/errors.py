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


class ProfileNotFound(NotFound):
    default_message = "Анкета не найдена или недоступна."


class ProfileNotActive(DomainError):
    default_message = "Отклики можно отправлять после того, как ваша анкета будет опубликована."


class ProfileStateError(DomainError):
    default_message = "Это действие недоступно для анкеты в текущем статусе."


class InterestToSelf(DomainError):
    default_message = "Нельзя откликнуться на свою анкету."


class InterestAlreadyExists(DomainError):
    default_message = "Между вами уже есть отклик или переписка."


class InterestDeclinedBefore(DomainError):
    default_message = "Этот человек уже отклонил ваш отклик."


class InterestLimitReached(DomainError):
    default_message = "Лимит откликов на сегодня исчерпан. Попробуйте завтра."


class InterestNotFound(NotFound):
    default_message = "Отклик не найден."


class InterestNotPending(DomainError):
    default_message = "На этот отклик уже ответили или его отозвали."


class ConversationNotFound(NotFound):
    default_message = "Чат не найден."


class ConversationClosed(DomainError):
    default_message = "Общение в этом чате завершено."


class PartnerDeleted(ConversationClosed):
    default_message = "Собеседник удалил аккаунт."


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


class ReportNotFound(NotFound):
    default_message = "Жалоба не найдена."


class ReportAlreadyResolved(DomainError):
    default_message = "Жалоба уже рассмотрена."
