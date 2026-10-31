"""Reply and inline keyboards. Button labels that depend on data are passed in by the caller."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts
from app.bot.callbacks import (
    CANCEL,
    REPORT_SEND,
    AccountAction,
    AccountCb,
    AdminProfileAction,
    AdminProfileCb,
    AdminReportAction,
    AdminReportCb,
    CatalogAction,
    CatalogCb,
    ChatAction,
    ChatCb,
    FieldAction,
    FieldCb,
    InterestAction,
    InterestCb,
    LoginCb,
    ProfileAction,
    ProfileCb,
    ReportCb,
    SettingsAction,
    SettingsCb,
    WaliCb,
)
from app.config import get_settings
from app.enums import ReportReason, label

Row = list[InlineKeyboardButton]


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _markup(*rows: Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _chunks(buttons: Sequence[InlineKeyboardButton], size: int) -> list[Row]:
    return [list(buttons[i : i + size]) for i in range(0, len(buttons), size)]


def cancel_button(text: str = "✖️ Отмена") -> InlineKeyboardButton:
    return _cb(text, CANCEL)


def cancel_only() -> InlineKeyboardMarkup:
    return _markup([cancel_button()])


def main_menu() -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(text=text) for text in texts.MENU_BUTTONS]
    return ReplyKeyboardMarkup(
        keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)],
        resize_keyboard=True,
        is_persistent=True,
    )


def site_url(path: str) -> str:
    return f"{get_settings().SITE_URL}{path}"


# --- Profile ----------------------------------------------------------------------------------


def create_profile() -> InlineKeyboardMarkup:
    return _markup([_cb("📝 Заполнить анкету", ProfileCb(action=ProfileAction.CREATE).pack())])


def field_step(
    field: str,
    choices: Sequence[tuple[str, str]],
    *,
    optional: bool,
    editing: bool,
    has_value: bool,
    columns: int = 2,
) -> InlineKeyboardMarkup:
    """Buttons of one wizard step: choices (if any), keep/skip/clear, «Готово» for the optional
    steps of a new profile, and cancel."""
    rows = _chunks(
        [_cb(text, FieldCb(field=field, action=FieldAction.SET, value=value).pack()) for value, text in choices],
        columns,
    )
    extra: Row = []
    if editing and has_value:
        extra.append(_cb("↩️ Оставить как есть", FieldCb(field=field, action=FieldAction.KEEP).pack()))
    if optional:
        text = "🧹 Очистить" if editing and has_value else "⏭ Пропустить"
        extra.append(_cb(text, FieldCb(field=field, action=FieldAction.CLEAR).pack()))
    finish = (
        [_cb("✅ Готово, остальное позже", FieldCb(field=field, action=FieldAction.FINISH).pack())]
        if optional and not editing
        else []
    )
    return _markup(*rows, extra, finish, [cancel_button()])


def field_picker(fields: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Editing: one button per field, (field, «Поле: текущее значение»)."""
    rows = [[_cb(text, FieldCb(field=field, action=FieldAction.PICK).pack())] for field, text in fields]
    return _markup(*rows, [cancel_button()])


def wizard_summary(*, editing: bool) -> InlineKeyboardMarkup:
    again = (
        _cb("✏️ Изменить ещё", ProfileCb(action=ProfileAction.MORE).pack())
        if editing
        else _cb("✏️ Заново", ProfileCb(action=ProfileAction.RESTART).pack())
    )
    return _markup([_cb("✅ Сохранить", ProfileCb(action=ProfileAction.SAVE).pack()), again], [cancel_button()])


def consent() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.CONSENT_BUTTON, ProfileCb(action=ProfileAction.CONSENT).pack())], [cancel_button()])


def adult() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.ADULT_BUTTON, ProfileCb(action=ProfileAction.ADULT).pack())], [cancel_button()])


def my_profile(*, can_hide: bool, can_show: bool) -> InlineKeyboardMarkup:
    toggle: Row = []
    if can_hide:
        toggle.append(_cb("🙈 Скрыть", ProfileCb(action=ProfileAction.HIDE).pack()))
    if can_show:
        toggle.append(_cb("👁 Показать", ProfileCb(action=ProfileAction.SHOW).pack()))
    return _markup(
        [_cb("✏️ Изменить", ProfileCb(action=ProfileAction.EDIT).pack()), *toggle],
        [_cb("🗑 Удалить аккаунт", AccountCb(action=AccountAction.DELETE).pack())],
    )


# --- Catalog and profile views ------------------------------------------------------------------


def catalog_card(user_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb("💌 Откликнуться", CatalogCb(action=CatalogAction.INTEREST, user_id=user_id).pack()),
            _cb("➡️ Дальше", CatalogCb(action=CatalogAction.NEXT).pack()),
        ],
        [
            _cb("⚠️ Пожаловаться", CatalogCb(action=CatalogAction.REPORT, user_id=user_id).pack()),
            _cb("⚙️ Фильтр", CatalogCb(action=CatalogAction.FILTER).pack()),
        ],
    )


def catalog_end(*, has_filter: bool) -> InlineKeyboardMarkup:
    reset = [_cb("🧹 Сбросить фильтр", CatalogCb(action=CatalogAction.RESET_FILTER).pack())] if has_filter else []
    return _markup(
        [
            _cb("🔄 Сначала", CatalogCb(action=CatalogAction.RESTART).pack()),
            _cb("⚙️ Фильтр", CatalogCb(action=CatalogAction.FILTER).pack()),
        ],
        reset,
    )


def filter_age() -> InlineKeyboardMarkup:
    return _markup([_cb("Любой возраст", CatalogCb(action=CatalogAction.ANY_AGE).pack())], [cancel_button()])


def filter_city() -> InlineKeyboardMarkup:
    return _markup([_cb("Любой город", CatalogCb(action=CatalogAction.ANY_CITY).pack())], [cancel_button()])


def interest_message() -> InlineKeyboardMarkup:
    return _markup([_cb("Без сообщения", CatalogCb(action=CatalogAction.NO_MESSAGE).pack())], [cancel_button()])


def profile_view(
    user_id: int,
    *,
    can_send_interest: bool,
    incoming_interest_id: int | None,
    open_conversation_id: int | None,
) -> InlineKeyboardMarkup:
    """Buttons under a profile opened by a p_<id> link, depending on the pair's relation."""
    actions: Row = []
    if open_conversation_id is not None:
        actions.append(_cb("✍️ Написать", ChatCb(action=ChatAction.REPLY, conversation_id=open_conversation_id).pack()))
    elif incoming_interest_id is not None:
        actions.extend(incoming_interest_buttons(incoming_interest_id))
    elif can_send_interest:
        actions.append(_cb("💌 Откликнуться", CatalogCb(action=CatalogAction.INTEREST, user_id=user_id).pack()))
    return _markup(actions, [_cb("⚠️ Пожаловаться", CatalogCb(action=CatalogAction.REPORT, user_id=user_id).pack())])


def report_reasons(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        _cb(label(reason).capitalize(), ReportCb(user_id=user_id, reason=reason).pack()) for reason in ReportReason
    ]
    return _markup(*_chunks(buttons, 1), [cancel_button()])


def report_comment() -> InlineKeyboardMarkup:
    return _markup([_cb("Отправить без комментария", REPORT_SEND)], [cancel_button()])


# --- Interests ------------------------------------------------------------------------------------


def incoming_interest_buttons(interest_id: int) -> Row:
    return [
        _cb("✅ Принять", InterestCb(action=InterestAction.ACCEPT, interest_id=interest_id).pack()),
        _cb("❌ Отклонить", InterestCb(action=InterestAction.DECLINE, interest_id=interest_id).pack()),
    ]


def incoming_interest(interest_id: int, from_user_id: int) -> InlineKeyboardMarkup:
    return _markup(
        incoming_interest_buttons(interest_id),
        [_url("👤 Анкета на сайте", site_url(f"/u/{from_user_id}"))],
    )


def outgoing_interests(pending: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup | None:
    """One «Отозвать» button per pending interest: (interest_id, person label)."""
    rows = [
        [_cb(f"↩️ Отозвать: {name}", InterestCb(action=InterestAction.CANCEL, interest_id=interest_id).pack())]
        for interest_id, name in pending
    ]
    return _markup(*rows) if rows else None


# --- Chats -----------------------------------------------------------------------------------------


def write(conversation_id: int) -> InlineKeyboardMarkup:
    return _markup([_cb("✍️ Написать", ChatCb(action=ChatAction.REPLY, conversation_id=conversation_id).pack())])


def message_notification(conversation_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb("↩️ Ответить", ChatCb(action=ChatAction.REPLY, conversation_id=conversation_id).pack()),
            _url("🌐 Открыть чат", site_url(f"/chats/{conversation_id}")),
        ]
    )


def chats_list(items: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup:
    return _markup(
        *[
            [_cb(text, ChatCb(action=ChatAction.OPEN, conversation_id=conversation_id).pack())]
            for conversation_id, text in items
        ]
    )


def chat_actions(conversation_id: int, *, is_open: bool) -> InlineKeyboardMarkup:
    site = [_url("🌐 Открыть на сайте", site_url(f"/chats/{conversation_id}"))]
    if not is_open:
        return _markup(site)
    return _markup(
        [
            _cb("🚪 Завершить общение", ChatCb(action=ChatAction.CLOSE, conversation_id=conversation_id).pack()),
            _cb("⚠️ Пожаловаться", ChatCb(action=ChatAction.REPORT, conversation_id=conversation_id).pack()),
        ],
        site,
    )


def close_confirm(conversation_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_cb("Да, завершить", ChatCb(action=ChatAction.CLOSE_CONFIRM, conversation_id=conversation_id).pack())],
        [cancel_button("Нет")],
    )


# --- Settings and account ----------------------------------------------------------------------------


def settings(*, notify: bool, wali_connected: bool, wali_pending: bool) -> InlineKeyboardMarkup:
    toggle = (
        _cb("🔕 Выключить уведомления", SettingsCb(action=SettingsAction.NOTIFY_OFF).pack())
        if notify
        else _cb("🔔 Включить уведомления", SettingsCb(action=SettingsAction.NOTIFY_ON).pack())
    )
    invite_text = "🔗 Новая ссылка для вали" if wali_connected or wali_pending else "👁 Пригласить вали"
    wali: Row = [_cb(invite_text, SettingsCb(action=SettingsAction.WALI_INVITE).pack())]
    if wali_connected or wali_pending:
        wali.append(_cb("Отключить вали", SettingsCb(action=SettingsAction.WALI_REVOKE).pack()))
    return _markup([toggle], wali)


def open_site(url: str) -> InlineKeyboardMarkup:
    return _markup([_url(texts.MENU_SITE, url)])


def delete_confirm() -> InlineKeyboardMarkup:
    return _markup(
        [_cb("Да, удалить всё", AccountCb(action=AccountAction.DELETE_CONFIRM).pack())],
        [cancel_button("Нет, оставить")],
    )


def login_confirm(token: str) -> InlineKeyboardMarkup:
    return _markup([_cb("✅ Да, это я вхожу", LoginCb(token=token).pack())], [cancel_button()])


def wali_confirm(token: str) -> InlineKeyboardMarkup:
    return _markup([_cb("✅ Подтвердить", WaliCb(token=token).pack())], [cancel_button()])


# --- Admin --------------------------------------------------------------------------------------------


def admin_profile(user_id: int, version: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb(
                "✅ Одобрить",
                AdminProfileCb(action=AdminProfileAction.APPROVE, user_id=user_id, version=version).pack(),
            ),
            _cb(
                "❌ Отклонить",
                AdminProfileCb(action=AdminProfileAction.REJECT, user_id=user_id, version=version).pack(),
            ),
        ]
    )


def admin_reject_reasons(user_id: int, version: int) -> InlineKeyboardMarkup:
    presets = [
        _cb(
            title,
            AdminProfileCb(action=AdminProfileAction.PRESET, user_id=user_id, version=version, preset=index).pack(),
        )
        for index, (title, _note) in enumerate(texts.ADMIN_REJECT_PRESETS)
    ]
    return _markup(
        *_chunks(presets, 1),
        [_cb("Другое", AdminProfileCb(action=AdminProfileAction.OTHER, user_id=user_id, version=version).pack())],
        [_cb("← Назад", AdminProfileCb(action=AdminProfileAction.BACK, user_id=user_id, version=version).pack())],
    )


def admin_report(report_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb("Отклонить жалобу", AdminReportCb(action=AdminReportAction.DISMISS, report_id=report_id).pack()),
            _cb("🙈 Скрыть анкету", AdminReportCb(action=AdminReportAction.HIDE_PROFILE, report_id=report_id).pack()),
        ],
        [_cb("⛔ Заблокировать", AdminReportCb(action=AdminReportAction.BAN, report_id=report_id).pack())],
    )


def admin_ban_confirm(report_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_cb("⛔ Да, заблокировать", AdminReportCb(action=AdminReportAction.BAN_CONFIRM, report_id=report_id).pack())],
        [_cb("← Назад", AdminReportCb(action=AdminReportAction.BACK, report_id=report_id).pack())],
    )
