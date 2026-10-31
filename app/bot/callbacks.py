"""Callback data of inline buttons.

Callback data comes from the client and is never trusted: it carries only IDs, profile numbers
and enum values, and handlers re-check every rule through app.services (and admin rights in admin
and witness handlers). People are referred to by profile number, never by ``users.id``.

Buttons that exist in the legacy bot keep its exact format, so the buttons still on people's
screens keep working (docs/LEGACY_BOT.md §3): ``int:acc:<interest_id>`` / ``int:dec:<id>``
(InterestCb), ``rep:<message_id>`` (MessageReportCb), ``w:flag|say|close:<id>`` (WitnessCb),
``adm:appr|rej:<profile number>`` (LegacyReviewCb), ``chat:close`` (LEGACY_CHAT_CLOSE); the
legacy ``br:*`` and ``reg:*`` buttons are recognised by prefix (handlers.legacy).
"""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

from app.enums import ReportAction, ReportReason

CANCEL = "cancel"  # generic «Отмена» for any flow
REPORT_SEND = "report_send"  # send a report without a comment
GIVE_CONSENT = "give_consent"  # the consent prompt shown to accounts migrated from the legacy bot
LEGACY_CHAT_CLOSE = "chat:close"  # legacy «Завершить общение»: the only open chat
LEGACY_BROWSE_PREFIX = "br:"
LEGACY_REGISTRATION_PREFIX = "reg:"


class ProfileAction(StrEnum):
    CREATE = "create"
    RULES_OK = "rules"  # registration: «Согласен(на) с правилами»
    EDIT = "edit"
    HIDE = "hide"
    SHOW = "show"
    SAVE = "save"
    RESTART = "restart"
    CONSENT = "consent"
    ADULT = "adult"
    MORE = "more"  # editing: change another field
    COMPLETE = "complete"  # «Дополнить анкету (1 минута)»


class FieldAction(StrEnum):
    SET = "set"
    KEEP = "keep"
    CLEAR = "clear"
    PICK = "pick"  # editing: the field to change
    FINISH = "finish"  # creating: skip the remaining optional fields


class CatalogAction(StrEnum):
    PREV = "prev"
    NEXT = "next"
    NOOP = "noop"  # the «N/M» counter
    CLOSE = "close"
    RESTART = "restart"
    INTEREST = "interest"
    NO_MESSAGE = "nomsg"
    REPORT = "report"
    FILTER = "filter"
    ANY_AGE = "anyage"
    ANY_CITY = "anycity"
    RESET_FILTER = "reset"


class InterestAction(StrEnum):
    ACCEPT = "acc"  # legacy format int:acc:<id>
    DECLINE = "dec"  # legacy format int:dec:<id>
    CANCEL = "cancel"


class ChatAction(StrEnum):
    OPEN = "open"
    REPLY = "reply"
    CLOSE = "close"
    CLOSE_CONFIRM = "closeok"
    REPORT = "report"


class WitnessAction(StrEnum):
    FLAG = "flag"  # target: message id
    SAY = "say"  # target: conversation id
    CLOSE = "close"  # target: conversation id


class LegacyReviewAction(StrEnum):
    APPROVE = "appr"
    REJECT = "rej"


class SettingsAction(StrEnum):
    NOTIFY_ON = "on"
    NOTIFY_OFF = "off"
    WALI_INVITE = "invite"
    WALI_REVOKE = "revoke"


class AccountAction(StrEnum):
    DELETE = "delete"
    DELETE_CONFIRM = "deleteok"


class AdminProfileAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PRESET = "preset"
    OTHER = "other"
    BACK = "back"


class AdminReportAction(StrEnum):
    DISMISS = ReportAction.DISMISS.value
    HIDE_PROFILE = ReportAction.HIDE_PROFILE.value
    BAN = "ban"  # asks for confirmation first
    BAN_CONFIRM = ReportAction.BAN_USER.value
    BACK = "back"


class ProfileCb(CallbackData, prefix="me"):
    action: ProfileAction


class FieldCb(CallbackData, prefix="pf"):
    field: str
    action: FieldAction
    value: str = ""


class FillCb(CallbackData, prefix="fill"):
    """«Дополнить анкету»: one press saves one missing field (stateless: survives restarts)."""

    field: str
    value: str


class CatalogCb(CallbackData, prefix="cat"):
    action: CatalogAction
    number: int = 0  # profile number of the card
    page: int = 0  # 1-based position of the card when it was shown


class ReportCb(CallbackData, prefix="rr"):
    number: int
    reason: ReportReason


class MessageReportCb(CallbackData, prefix="rep"):
    """«Пожаловаться» under a relayed chat message (legacy format rep:<message_id>)."""

    message_id: int


class InterestCb(CallbackData, prefix="int"):
    action: InterestAction
    interest_id: int


class ChatCb(CallbackData, prefix="chat"):
    action: ChatAction
    conversation_id: int


class WitnessCb(CallbackData, prefix="w"):
    action: WitnessAction
    target_id: int


class LegacyReviewCb(CallbackData, prefix="adm"):
    """Legacy moderation buttons «Одобрить» / «Отклонить» under cards sent by the old bot."""

    action: LegacyReviewAction
    number: int


class SettingsCb(CallbackData, prefix="set"):
    action: SettingsAction


class AccountCb(CallbackData, prefix="acc"):
    action: AccountAction


class LoginCb(CallbackData, prefix="login"):
    token: str


class WaliCb(CallbackData, prefix="wali"):
    token: str


class AdminProfileCb(CallbackData, prefix="adp"):
    action: AdminProfileAction
    user_id: int
    # Submission time (unix seconds) of the content the admin saw: stale cards cannot approve edits.
    version: int
    preset: int = 0


class AdminReportCb(CallbackData, prefix="adr"):
    action: AdminReportAction
    report_id: int
