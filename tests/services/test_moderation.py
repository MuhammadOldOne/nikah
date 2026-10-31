import pytest
from sqlalchemy import select

from app.enums import (
    ConversationCloseReason,
    Gender,
    MessageOrigin,
    ProfileStatus,
    ReportAction,
    ReportReason,
    ReportStatus,
)
from app.models import Outbox
from app.services import chat, moderation
from app.services.errors import (
    AdminBanDenied,
    AlreadyReported,
    InvalidInput,
    NotFound,
    PermissionDenied,
    ReportAlreadyResolved,
    ReportNotFound,
    ReportToSelf,
    UserBanned,
    UserNotFound,
)
from app.services.moderation import find_stop_words


async def test_create_report(session, make_pair):
    man, woman = await make_pair()
    report = await moderation.create_report(session, woman, man.id, ReportReason.FRAUD, "  просил деньги  ")
    assert report.status == ReportStatus.OPEN and report.comment == "просил деньги"
    event = await session.scalar(select(Outbox).where(Outbox.kind == "report.created"))
    assert event.payload == {
        "report_id": report.id,
        "reporter_id": woman.id,
        "target_user_id": man.id,
        "message_id": None,
        "user_ids": [],
    }
    with pytest.raises(AlreadyReported):
        await moderation.create_report(session, woman, man.id, ReportReason.OTHER)
    loaded = await moderation.get_report(session, report.id)
    assert loaded.target_user.profile.gender == Gender.MALE


async def test_report_rules(session, make_pair, make_user):
    man, woman = await make_pair()
    with pytest.raises(ReportToSelf):
        await moderation.create_report(session, man, man.id, ReportReason.OTHER)
    with pytest.raises(UserNotFound):
        await moderation.create_report(session, man, 99999, ReportReason.OTHER)
    with pytest.raises(InvalidInput):
        await moderation.create_report(session, man, woman.id, ReportReason.OTHER, "а" * 1001)
    with pytest.raises(UserBanned):
        await moderation.create_report(session, await make_user(is_banned=True), woman.id, ReportReason.OTHER)


async def test_report_message_must_belong_to_target_in_shared_chat(session, make_pair, make_conversation, make_profile):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    his = await chat.send_message(session, conversation.id, man, "Скинь деньги", MessageOrigin.WEB)
    hers = await chat.send_message(session, conversation.id, woman, "Нет", MessageOrigin.WEB)
    outsider = (await make_profile(gender=Gender.FEMALE)).user
    with pytest.raises(NotFound):
        await moderation.create_report(session, woman, man.id, ReportReason.FRAUD, message_id=hers.id)
    with pytest.raises(NotFound):
        await moderation.create_report(session, outsider, man.id, ReportReason.FRAUD, message_id=his.id)
    report = await moderation.create_report(session, woman, man.id, ReportReason.FRAUD, message_id=his.id)
    assert (await moderation.get_report(session, report.id)).message.text == "Скинь деньги"


async def test_resolve_requires_admin_and_open_report(session, make_pair, make_user):
    man, woman = await make_pair()
    admin = await make_user(is_admin=True)
    report = await moderation.create_report(session, woman, man.id, ReportReason.OTHER)
    with pytest.raises(PermissionDenied):
        await moderation.resolve_report(session, report.id, woman, ReportAction.DISMISS)
    with pytest.raises(ReportNotFound):
        await moderation.resolve_report(session, 99999, admin, ReportAction.DISMISS)
    resolved = await moderation.resolve_report(session, report.id, admin, ReportAction.DISMISS)
    assert resolved.status == ReportStatus.RESOLVED and resolved.resolution == ReportAction.DISMISS
    assert resolved.resolved_by_id == admin.id and resolved.resolved_at is not None
    with pytest.raises(ReportAlreadyResolved):
        await moderation.resolve_report(session, report.id, admin, ReportAction.BAN_USER)
    assert man.is_banned is False
    assert await moderation.list_reports(session) == []
    assert [r.id for r in await moderation.list_reports(session, ReportStatus.RESOLVED)] == [report.id]


async def test_resolve_hide_profile_rejects_it(session, make_pair, make_user, override_settings):
    man, woman = await make_pair()
    override_settings(ADMIN_TELEGRAM_IDS=[(admin := await make_user()).telegram_id])
    report = await moderation.create_report(session, woman, man.id, ReportReason.FAKE)
    await moderation.resolve_report(session, report.id, admin, ReportAction.HIDE_PROFILE)
    assert man.profile.status == ProfileStatus.REJECTED
    assert man.profile.moderation_note == moderation.HIDE_PROFILE_NOTE


async def test_resolve_ban_user_bans_and_resolves_all_reports(
    session, make_pair, make_profile, make_conversation, make_user
):
    man, woman = await make_pair()
    other_woman = (await make_profile(gender=Gender.FEMALE)).user
    conversation = await make_conversation(man, woman)
    admin = await make_user(is_admin=True)
    first = await moderation.create_report(session, woman, man.id, ReportReason.FRAUD)
    second = await moderation.create_report(session, other_woman, man.id, ReportReason.FRAUD)
    await moderation.resolve_report(session, first.id, admin, ReportAction.BAN_USER)
    await session.refresh(second)
    assert man.is_banned is True
    assert second.status == ReportStatus.RESOLVED and second.resolution == ReportAction.BAN_USER
    await session.refresh(conversation)
    assert conversation.close_reason == ConversationCloseReason.USER_BANNED


async def test_resolve_ban_user_refuses_to_ban_an_admin(session, make_profile, make_user):
    reporter = (await make_profile(gender=Gender.FEMALE)).user
    target_admin = (await make_profile(await make_user(is_admin=True), gender=Gender.MALE)).user
    admin = await make_user(is_admin=True)
    report = await moderation.create_report(session, reporter, target_admin.id, ReportReason.FRAUD)
    with pytest.raises(AdminBanDenied):
        await moderation.resolve_report(session, report.id, admin, ReportAction.BAN_USER)
    assert not target_admin.is_banned
    assert report.status == ReportStatus.OPEN  # other decisions remain possible
    await moderation.resolve_report(session, report.id, admin, ReportAction.DISMISS)
    assert report.status == ReportStatus.RESOLVED


async def test_list_flagged_messages(session, make_pair, make_conversation):
    man, woman = await make_pair()
    conversation = await make_conversation(man, woman)
    await chat.send_message(session, conversation.id, man, "Добрый день", MessageOrigin.WEB)
    bad = await chat.send_message(session, conversation.id, man, "Нужна предоплата", MessageOrigin.WEB)
    flagged = await moderation.list_flagged_messages(session)
    assert [m.id for m in flagged] == [bad.id]
    assert flagged[0].sender.profile.gender == Gender.MALE


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("Переведи мне денег до завтра", "переведи"),
        ("нужен перевод денег срочно", "перевод денег"),
        ("скинь на карту", "скинь на карту"),
        ("Скажи номер своей карты", "номер своей карты"),
        ("4276 1234 5678 9012", "4276 1234 5678 9012"),
        ("4276123456789012", "4276123456789012"),
        ("Зарабатываю на крипте", "крипте"),
        ("Только USDT", "usdt"),
        ("биткоин растёт", "биткоин"),
        ("buy bitcoin now", "bitcoin"),
        ("Выгодные инвестиции", "инвестиции"),
        ("Делаю ставки на спорт", "ставки"),
        ("Нужен займ", "займ"),
        ("Только по предоплате", "предоплате"),
        ("Работаю в вебкам", "вебкам"),
        ("perevod na kartu", "perevod"),
        ("kriptovalyuta", "kriptovalyuta"),
    ],
)
def test_find_stop_words(text: str, marker: str):
    assert marker in find_stop_words(text)


@pytest.mark.parametrize(
    "text",
    [
        "Ассаляму алейкум! Ищу добрую и верующую жену.",
        "Займусь спортом и учёбой",
        "Работаю переводчиком",
        "Мой рост 180, вес 75, родился в 1990 году",
        "Позвоню +7 999 123-45-67",
    ],
)
def test_find_stop_words_ignores_ordinary_text(text: str):
    assert find_stop_words(text) == []


async def test_reports_need_a_known_target_and_are_limited_per_day(session, make_pair, make_profile, make_user):
    from app.services.errors import ReportLimitReached

    man, _ = await make_pair()
    stranger = await make_user()  # no profile, never met: cannot be reported (no card flood)
    with pytest.raises(UserNotFound):
        await moderation.create_report(session, man, stranger.id, ReportReason.OTHER)
    same_gender = (await make_profile(gender=Gender.MALE)).user  # not in his catalog
    with pytest.raises(UserNotFound):
        await moderation.create_report(session, man, same_gender.id, ReportReason.OTHER)

    women = [(await make_profile(gender=Gender.FEMALE)).user for _ in range(moderation.REPORT_DAILY_LIMIT + 1)]
    for woman in women[:-1]:
        await moderation.create_report(session, man, woman.id, ReportReason.FAKE, "к" * 1000)
    with pytest.raises(ReportLimitReached):
        await moderation.create_report(session, man, women[-1].id, ReportReason.FAKE)
