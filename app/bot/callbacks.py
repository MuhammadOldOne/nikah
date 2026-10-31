"""Callback data of inline buttons.

Callback data comes from the client and is never trusted: it carries only IDs and enum values,
and handlers re-check every rule through app.services (and admin rights in admin handlers).
"""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

from app.enums import ReportAction, ReportReason

CANCEL = "cancel"  # generic "✖️ Отмена" for any flow
REPORT_SEND = "report_send"  # send a report without a comment


class ProfileAction(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    HIDE = "hide"
    SHOW = "show"
    SAVE = "save"
    RESTART = "restart"
    CONSENT = "consent"
    ADULT = "adult"


class FieldAction(StrEnum):
    SET = "set"
    KEEP = "keep"
    CLEAR = "clear"


class CatalogAction(StrEnum):
    NEXT = "next"
    RESTART = "restart"
    INTEREST = "interest"
    NO_MESSAGE = "nomsg"
    REPORT = "report"
    FILTER = "filter"
    ANY_AGE = "anyage"
    ANY_CITY = "anycity"
    RESET_FILTER = "reset"


class InterestAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class ChatAction(StrEnum):
    OPEN = "open"
    REPLY = "reply"
    CLOSE = "close"
    CLOSE_CONFIRM = "closeok"
    REPORT = "report"


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


class CatalogCb(CallbackData, prefix="cat"):
    action: CatalogAction
    user_id: int = 0


class ReportCb(CallbackData, prefix="rep"):
    user_id: int
    reason: ReportReason


class InterestCb(CallbackData, prefix="int"):
    action: InterestAction
    interest_id: int


class ChatCb(CallbackData, prefix="chat"):
    action: ChatAction
    conversation_id: int


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
