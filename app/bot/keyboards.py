"""Reply and inline keyboards. Button labels that depend on data are passed in by the caller."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aiogram.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts
from app.bot.callbacks import (
    CANCEL,
    GIVE_CONSENT,
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
    FillCb,
    InterestAction,
    InterestCb,
    LoginCb,
    MessageReportCb,
    ProfileAction,
    ProfileCb,
    ReportCb,
    SettingsAction,
    SettingsCb,
    WaliCb,
    WitnessAction,
    WitnessCb,
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


def cancel_button(text: str = "Отмена") -> InlineKeyboardButton:
    return _cb(text, CANCEL)


def cancel_only() -> InlineKeyboardMarkup:
    return _markup([cancel_button()])


def main_menu(*, has_profile: bool, is_admin: bool = False) -> ReplyKeyboardMarkup:
    """The legacy menu extended with the new sections (docs/LEGACY_BOT.md §3)."""
    if has_profile:
        labels = [
            [texts.MENU_BROWSE, texts.MENU_MY_PROFILE],
            [texts.MENU_MY_CHAT, texts.MENU_INTERESTS],
            [texts.MENU_SETTINGS, texts.MENU_SITE, texts.MENU_RULES],
            [texts.MENU_HELP],
        ]
    else:
        labels = [[texts.MENU_CREATE, texts.MENU_SITE, texts.MENU_RULES], [texts.MENU_HELP]]
    if is_admin:
        labels.append([texts.MENU_ADMIN])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in labels],
        resize_keyboard=True,
        is_persistent=True,
    )


def site_url(path: str) -> str:
    return f"{get_settings().SITE_URL}{path}"


def give_consent() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.CONSENT_BUTTON, GIVE_CONSENT)])


# --- Profile ----------------------------------------------------------------------------------


def create_profile() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.MENU_CREATE, ProfileCb(action=ProfileAction.CREATE).pack())])


def rules_agree() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.RULES_AGREE_BUTTON, ProfileCb(action=ProfileAction.RULES_OK).pack())], [cancel_button()])


def field_step(
    field: str,
    choices: Sequence[tuple[str, str]],
    *,
    optional: bool,
    editing: bool,
    has_value: bool,
    can_finish: bool,
    columns: int = 2,
) -> InlineKeyboardMarkup:
    """Buttons of one wizard step: choices (if any), keep/skip/clear, «Готово, остальное позже»
    once the required questions of a new profile are answered, and cancel."""
    rows = _chunks(
        [_cb(text, FieldCb(field=field, action=FieldAction.SET, value=value).pack()) for value, text in choices],
        columns,
    )
    extra: Row = []
    if editing and has_value:
        extra.append(_cb("Оставить как есть", FieldCb(field=field, action=FieldAction.KEEP).pack()))
    if optional:
        text = "Очистить" if editing and has_value else "Пропустить"
        extra.append(_cb(text, FieldCb(field=field, action=FieldAction.CLEAR).pack()))
    finish = (
        [_cb("Готово, остальное позже", FieldCb(field=field, action=FieldAction.FINISH).pack())] if can_finish else []
    )
    return _markup(*rows, extra, finish, [cancel_button()])


def field_picker(fields: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Editing: one button per field, (field, «Поле: текущее значение»)."""
    rows = [[_cb(text, FieldCb(field=field, action=FieldAction.PICK).pack())] for field, text in fields]
    return _markup(*rows, [cancel_button()])


def wizard_summary(*, editing: bool, premoderation: bool) -> InlineKeyboardMarkup:
    if editing:
        save = _cb("Сохранить", ProfileCb(action=ProfileAction.SAVE).pack())
        again = _cb("Изменить ещё", ProfileCb(action=ProfileAction.MORE).pack())
    else:
        submit = "Отправить на модерацию" if premoderation else "Опубликовать"
        save = _cb(submit, ProfileCb(action=ProfileAction.SAVE).pack())
        again = _cb("Заново", ProfileCb(action=ProfileAction.RESTART).pack())
    return _markup([save], [again, cancel_button()])


def consent() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.CONSENT_BUTTON, ProfileCb(action=ProfileAction.CONSENT).pack())], [cancel_button()])


def adult() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.ADULT_BUTTON, ProfileCb(action=ProfileAction.ADULT).pack())], [cancel_button()])


def complete_profile() -> InlineKeyboardMarkup:
    return _markup([_cb(texts.COMPLETE_BUTTON, ProfileCb(action=ProfileAction.COMPLETE).pack())])


def fill_field(field: str, choices: Sequence[tuple[str, str]], columns: int = 2) -> InlineKeyboardMarkup:
    """«Дополнить анкету»: one button per value; a press saves it."""
    return _markup(*_chunks([_cb(text, FillCb(field=field, value=value).pack()) for value, text in choices], columns))


def my_profile(*, can_hide: bool, can_show: bool, incomplete: bool) -> InlineKeyboardMarkup:
    toggle: Row = []
    if can_hide:
        toggle.append(_cb("Скрыть", ProfileCb(action=ProfileAction.HIDE).pack()))
    if can_show:
        toggle.append(_cb("Показать", ProfileCb(action=ProfileAction.SHOW).pack()))
    complete = [_cb(texts.COMPLETE_BUTTON, ProfileCb(action=ProfileAction.COMPLETE).pack())] if incomplete else []
    return _markup(
        complete,
        [_cb("Изменить", ProfileCb(action=ProfileAction.EDIT).pack()), *toggle],
        [_cb("Удалить аккаунт", AccountCb(action=AccountAction.DELETE).pack())],
    )


# --- Browsing and profile views -------------------------------------------------------------------


def browse(page: int, total: int, number: int, *, has_filter: bool) -> InlineKeyboardMarkup:
    """The legacy browsing card: ⬅️ N/M ➡️, «Написать (заинтересоваться)», «Закрыть» — plus a
    report and the age/city filter."""
    nav: Row = []
    if page > 1:
        nav.append(_cb("⬅️", CatalogCb(action=CatalogAction.PREV, number=number, page=page).pack()))
    nav.append(_cb(f"{page}/{total}", CatalogCb(action=CatalogAction.NOOP).pack()))
    if page < total:
        nav.append(_cb("➡️", CatalogCb(action=CatalogAction.NEXT, number=number, page=page).pack()))
    tools = [
        _cb("Пожаловаться", CatalogCb(action=CatalogAction.REPORT, number=number).pack()),
        _cb("Сбросить фильтр" if has_filter else "Фильтр", _filter_data(has_filter)),
    ]
    return _markup(
        nav,
        [_cb("Написать (заинтересоваться)", CatalogCb(action=CatalogAction.INTEREST, number=number).pack())],
        tools,
        [_cb("Закрыть", CatalogCb(action=CatalogAction.CLOSE).pack())],
    )


def _filter_data(has_filter: bool) -> str:
    action = CatalogAction.RESET_FILTER if has_filter else CatalogAction.FILTER
    return CatalogCb(action=action).pack()


def browse_end(*, has_filter: bool) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb("Сначала", CatalogCb(action=CatalogAction.RESTART).pack()),
            _cb("Сбросить фильтр" if has_filter else "Фильтр", _filter_data(has_filter)),
        ]
    )


def filter_age() -> InlineKeyboardMarkup:
    return _markup([_cb("Любой возраст", CatalogCb(action=CatalogAction.ANY_AGE).pack())], [cancel_button()])


def filter_city() -> InlineKeyboardMarkup:
    return _markup([_cb("Любой город", CatalogCb(action=CatalogAction.ANY_CITY).pack())], [cancel_button()])


def interest_message(number: int) -> InlineKeyboardMarkup:
    """Under the question for an interest message: «Без сообщения» names the profile, so it works
    whatever happened to the dialog."""
    no_message = CatalogCb(action=CatalogAction.NO_MESSAGE, number=number).pack()
    return _markup([_cb("Без сообщения", no_message)], [cancel_button()])


def profile_view(
    number: int,
    *,
    can_send_interest: bool,
    incoming_interest_id: int | None,
    open_conversation_id: int | None,
) -> InlineKeyboardMarkup:
    """Buttons under a profile opened by a p_<number> link, depending on the pair's relation."""
    actions: Row = []
    if open_conversation_id is not None:
        actions.append(_cb("Написать", ChatCb(action=ChatAction.REPLY, conversation_id=open_conversation_id).pack()))
    elif incoming_interest_id is not None:
        actions.extend(interest_decision_buttons(incoming_interest_id))
    elif can_send_interest:
        actions.append(
            _cb("Написать (заинтересоваться)", CatalogCb(action=CatalogAction.INTEREST, number=number).pack())
        )
    return _markup(actions, [_cb("Пожаловаться", CatalogCb(action=CatalogAction.REPORT, number=number).pack())])


def report_reasons(number: int) -> InlineKeyboardMarkup:
    buttons = [
        _cb(label(reason).capitalize(), ReportCb(number=number, reason=reason).pack()) for reason in ReportReason
    ]
    return _markup(*_chunks(buttons, 1), [cancel_button()])


def report_comment() -> InlineKeyboardMarkup:
    return _markup([_cb("Отправить без комментария", REPORT_SEND)], [cancel_button()])


# --- Interests ------------------------------------------------------------------------------------


def interest_decision_buttons(interest_id: int) -> Row:
    """«Принять» / «Отклонить» in the legacy callback format (int:acc:<id> / int:dec:<id>)."""
    return [
        _cb("Принять", InterestCb(action=InterestAction.ACCEPT, interest_id=interest_id).pack()),
        _cb("Отклонить", InterestCb(action=InterestAction.DECLINE, interest_id=interest_id).pack()),
    ]


def interest_decision(interest_id: int) -> InlineKeyboardMarkup:
    return _markup(interest_decision_buttons(interest_id))


def outgoing_interests(pending: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup | None:
    """One «Отозвать» button per pending interest: (interest_id, «Анкета №N»)."""
    rows = [
        [_cb(f"Отозвать: {name}", InterestCb(action=InterestAction.CANCEL, interest_id=interest_id).pack())]
        for interest_id, name in pending
    ]
    return _markup(*rows) if rows else None


# --- Chats -----------------------------------------------------------------------------------------


def write(conversation_id: int) -> InlineKeyboardMarkup:
    return _markup([_cb("Написать", ChatCb(action=ChatAction.REPLY, conversation_id=conversation_id).pack())])


def chat_opened(conversation_id: int, *, several_allowed: bool) -> InlineKeyboardMarkup:
    """Under «Чат открыт…»: the legacy «Завершить общение» (and «Написать» when a person may have
    several chats, so the bot knows which one plain text goes to)."""
    close = _cb("Завершить общение", ChatCb(action=ChatAction.CLOSE, conversation_id=conversation_id).pack())
    write_row = (
        [_cb("Написать", ChatCb(action=ChatAction.REPLY, conversation_id=conversation_id).pack())]
        if several_allowed
        else []
    )
    return _markup(write_row, [close])


def relayed_message(message_id: int, conversation_id: int, *, with_reply: bool) -> InlineKeyboardMarkup:
    """Under «Анкета №N пишет:»: the legacy «Пожаловаться» (rep:<message_id>), and «Ответить» when
    the recipient has several open chats."""
    reply = (
        [_cb("Ответить", ChatCb(action=ChatAction.REPLY, conversation_id=conversation_id).pack())] if with_reply else []
    )
    return _markup([*reply, _cb("Пожаловаться", MessageReportCb(message_id=message_id).pack())])


def chats_list(items: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup:
    return _markup(
        *[
            [_cb(text, ChatCb(action=ChatAction.OPEN, conversation_id=conversation_id).pack())]
            for conversation_id, text in items
        ]
    )


def chat_actions(conversation_id: int, *, is_open: bool) -> InlineKeyboardMarkup:
    site = [_url("Открыть на сайте", site_url(f"/chats/{conversation_id}"))]
    if not is_open:
        return _markup(site)
    return _markup(
        [
            _cb("Завершить чат", ChatCb(action=ChatAction.CLOSE, conversation_id=conversation_id).pack()),
            _cb("Пожаловаться", ChatCb(action=ChatAction.REPORT, conversation_id=conversation_id).pack()),
        ],
        site,
    )


def close_confirm(conversation_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_cb("Да, завершить", ChatCb(action=ChatAction.CLOSE_CONFIRM, conversation_id=conversation_id).pack())],
        [cancel_button("Нет")],
    )


def witness_copy(message_id: int, conversation_id: int) -> InlineKeyboardMarkup:
    """Under a copy for the witness (legacy layout and callback data)."""
    return _markup(
        [
            _cb("⚠️ Пометить", WitnessCb(action=WitnessAction.FLAG, target_id=message_id).pack()),
            _cb("✍️ Написать в чат", WitnessCb(action=WitnessAction.SAY, target_id=conversation_id).pack()),
        ],
        [_cb("🚫 Закрыть чат", WitnessCb(action=WitnessAction.CLOSE, target_id=conversation_id).pack())],
    )


def witness_text_prompt() -> ForceReply:
    """The witness's next message must reach the bot also in a group with privacy mode on: a reply
    to the bot's message is always delivered."""
    return ForceReply(input_field_placeholder="Текст для обеих сторон")


# --- Settings and account ----------------------------------------------------------------------------


def settings(*, notify: bool, wali_connected: bool, wali_pending: bool) -> InlineKeyboardMarkup:
    toggle = (
        _cb("Выключить уведомления", SettingsCb(action=SettingsAction.NOTIFY_OFF).pack())
        if notify
        else _cb("Включить уведомления", SettingsCb(action=SettingsAction.NOTIFY_ON).pack())
    )
    invite_text = "Новая ссылка для вали" if wali_connected or wali_pending else "Пригласить вали"
    wali: Row = [_cb(invite_text, SettingsCb(action=SettingsAction.WALI_INVITE).pack())]
    if wali_connected or wali_pending:
        wali.append(_cb("Отключить вали", SettingsCb(action=SettingsAction.WALI_REVOKE).pack()))
    return _markup([toggle], wali)


def open_site(url: str) -> InlineKeyboardMarkup:
    return _markup([_url(texts.SITE_BUTTON, url)])


def delete_confirm() -> InlineKeyboardMarkup:
    return _markup(
        [_cb("Да, удалить всё", AccountCb(action=AccountAction.DELETE_CONFIRM).pack())],
        [cancel_button("Нет, оставить")],
    )


def login_confirm(token: str) -> InlineKeyboardMarkup:
    return _markup([_cb("Да, это я вхожу", LoginCb(token=token).pack())], [cancel_button()])


def wali_confirm(token: str) -> InlineKeyboardMarkup:
    return _markup([_cb("Подтвердить", WaliCb(token=token).pack())], [cancel_button()])


# --- Admin --------------------------------------------------------------------------------------------


def admin_profile(user_id: int, version: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _cb(
                "Одобрить",
                AdminProfileCb(action=AdminProfileAction.APPROVE, user_id=user_id, version=version).pack(),
            ),
            _cb(
                "Отклонить",
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
            _cb("Скрыть анкету", AdminReportCb(action=AdminReportAction.HIDE_PROFILE, report_id=report_id).pack()),
        ],
        [_cb("Заблокировать", AdminReportCb(action=AdminReportAction.BAN, report_id=report_id).pack())],
    )


def admin_ban_confirm(report_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_cb("Да, заблокировать", AdminReportCb(action=AdminReportAction.BAN_CONFIRM, report_id=report_id).pack())],
        [_cb("← Назад", AdminReportCb(action=AdminReportAction.BACK, report_id=report_id).pack())],
    )
