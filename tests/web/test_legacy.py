"""Website rules from the legacy bot integration (docs/LEGACY_BOT.md §2, §6): people are known by
profile number only, browsing needs a published profile, migrated profiles are completed without
re-moderation, migrated accounts give consent first, and the branding of «Никах Истина»."""

import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.enums import Education, Employment, Gender, MaritalStatus, MessageOrigin, ProfileStatus
from app.models import Outbox
from app.services import chat, guardians, interests, profiles, users
from app.services.errors import ConsentRequired
from app.services.events import EventKind
from tests.web.conftest import csrf_of, profile_form


@pytest.fixture
async def high_user_ids(session):
    """Users created afterwards get ids 7_770_001, 7_770_002, …: easy to spot in a page."""
    await session.execute(text("SELECT setval(pg_get_serial_sequence('users', 'id'), 7770000)"))
    await session.commit()


async def _incomplete_profile(make_profile, **fields):
    """A profile as migrated from the legacy bot: the four new required fields are empty."""
    values = {"marital_status": None, "children": None, "education": None, "employment": None} | fields
    return await make_profile(**values)


# --- Identity: «Анкета №N — Брат/Сестра», /p/{number}, no internal ids ----------------------------


async def test_no_internal_user_ids_in_pages_or_json(
    client, dev_login, high_user_ids, make_profile, make_user, session
):
    brother = await make_profile(gender=Gender.MALE)
    sister = await make_profile(gender=Gender.FEMALE)
    other = await make_profile(gender=Gender.MALE)
    man, woman = brother.user, sister.user
    conversation = await _open_chat(session, man, woman)
    await interests.send_interest(session, other.user, woman.id, "Ассаляму алейкум")
    await chat.send_message(session, conversation.id, man, "Салам", MessageOrigin.WEB)
    link = await guardians.create_invite(session, woman)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], await make_user())
    await session.commit()
    assert min(brother.user_id, sister.user_id, other.user_id) > 7_770_000

    await dev_login(client, woman)
    pages = [
        "/",
        "/catalog",
        f"/p/{brother.number}",
        f"/p/{sister.number}",
        "/interests",
        "/interests?tab=out",
        "/chats",
        f"/chats/{conversation.id}",
        f"/chats/{conversation.id}/messages?after_id=0",
        "/me",
        "/me/edit",
        "/settings",
        "/nav/badges",
    ]
    for path in pages:
        response = await client.get(path)
        assert response.status_code == 200, path
        assert "777000" not in response.text, path
        assert "/u/" not in response.text, path
    profile_page = (await client.get(f"/p/{brother.number}")).text
    assert f"Анкета №{brother.number} — Брат" in profile_page
    incoming = (await client.get("/interests")).text
    assert f'href="/p/{other.number}"' in incoming and f"Анкета №{other.number} — Брат" in incoming
    chats = (await client.get("/chats")).text
    assert f"Анкета №{brother.number} — Брат" in chats
    chat_page = (await client.get(f"/chats/{conversation.id}")).text
    assert f'<a href="/p/{brother.number}">Анкета №{brother.number} — Брат</a>' in chat_page
    me = (await client.get("/me")).text
    assert f"Анкета №{sister.number} — Сестра" in me and f'class="nav-num">№{sister.number}<' in me


async def test_catalog_cards_link_to_numbers(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.FEMALE)
    brother = await make_profile(gender=Gender.MALE)
    await dev_login(client, me.user)
    catalog = (await client.get("/catalog")).text
    assert f'<a href="/p/{brother.number}">Анкета №{brother.number} — Брат, ' in catalog


async def _open_chat(session, man, woman):
    interest = await interests.send_interest(session, man, woman.id)
    conversation = await interests.accept(session, interest.id, woman)
    await session.commit()
    return conversation


async def test_profile_address_is_the_number(client, dev_login, make_pair):
    man, woman = await make_pair()
    await dev_login(client, man)
    page = await client.get(f"/p/{woman.profile.number}")
    assert page.status_code == 200
    assert f"<title>Анкета №{woman.profile.number} — Сестра — Никах Истина</title>" in page.text
    assert (await client.get(f"/u/{woman.id}")).status_code == 404
    assert (await client.get(f"/u/{woman.id}/interest")).status_code == 404


async def test_wizard_has_no_name_field_and_birth_years_18_to_80(client, dev_login, make_user):
    from app.clock import current_year

    await dev_login(client, await make_user())
    html = (await client.get("/profile/new")).text
    assert 'name="name"' not in html and "Имя или псевдоним" not in html
    assert f'<option value="{current_year() - 80}">' in html and f'<option value="{current_year() - 81}">' not in html
    edit_html = html
    assert "по номеру анкеты" in edit_html


# --- Gating: browsing needs an ACTIVE profile (PREMODERATION) -------------------------------------


@pytest.mark.parametrize(
    ("status", "fields", "expected"),
    [
        (ProfileStatus.PENDING, {}, ["Ваша анкета на модерации", "Ваша анкета ещё не одобрена"]),
        (
            ProfileStatus.REJECTED,
            {"moderation_note": "Уберите рекламу из раздела «о себе»."},
            ["Ваша анкета отклонена", "Уберите рекламу из раздела «о себе».", 'href="/me/edit"'],
        ),
        (ProfileStatus.HIDDEN, {}, ["Ваша анкета скрыта", 'action="/me/show"', "Показать анкету"]),
    ],
)
async def test_gate_pages_instead_of_the_catalog(client, dev_login, make_profile, status, fields, expected):
    me = await make_profile(gender=Gender.MALE, status=status, **fields)
    sister = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    for path in ("/catalog", f"/p/{sister.number}", "/p/99999"):
        response = await client.get(path)
        assert response.status_code == 200, path
        for marker in expected:
            assert marker in response.text, (path, marker)
        assert f"Анкета №{sister.number}" not in response.text


async def test_gate_reloads_htmx_requests_and_refuses_interests(client, dev_login, make_profile, session):
    me = await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    sister = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    partial = await client.get("/catalog?city=Уфа", headers={"HX-Request": "true"})
    assert partial.status_code == 204 and partial.headers["HX-Redirect"] == "/catalog"

    csrf = await csrf_of(client, "/me")
    response = await client.post(f"/p/{sister.number}/interest", data={"csrf_token": csrf, "message": ""})
    assert response.status_code == 303 and response.headers["location"] == f"/p/{sister.number}"
    page = (await client.get(f"/p/{sister.number}")).text
    assert "Смотреть анкеты и отправлять отклики можно после модерации" in page
    assert await session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "interest.created")) == 0


async def test_pending_user_keeps_counterparts_and_answers_interests(client, dev_login, make_pair, session):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id, "Ассаляму алейкум")
    await profiles.hide(session, woman)
    await session.commit()
    await dev_login(client, woman)
    page = await client.get(f"/p/{man.profile.number}")  # a counterpart stays viewable
    assert page.status_code == 200 and f"Анкета №{man.profile.number} — Брат" in page.text
    listing = (await client.get("/interests")).text
    assert "Отправлять отклики можно после одобрения анкеты" in listing
    assert f"/interests/{interest.id}/accept" in listing  # answering stays possible


async def test_without_premoderation_a_pending_profile_may_browse(client, dev_login, make_profile, override_settings):
    override_settings(PREMODERATION=False)
    me = await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    sister = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    page = await client.get("/catalog")
    assert page.status_code == 200 and f"Анкета №{sister.number}" in page.text


# --- Completing a migrated profile ------------------------------------------------------------


async def test_incomplete_profile_banners_and_unknown_fields(client, dev_login, make_profile):
    me = await _incomplete_profile(make_profile, gender=Gender.FEMALE)
    brother = await _incomplete_profile(make_profile, gender=Gender.MALE)
    await dev_login(client, me.user)
    for path in ("/", "/me"):
        html = (await client.get(path)).text
        assert "Дополнить анкету (1 минута)" in html and 'href="/me/complete"' in html, path
        assert "семейное положение, дети, образование, занятость" in html, path
    card = (await client.get("/catalog")).text
    assert f"Анкета №{brother.number}" in card
    assert 'семейное положение: <span class="unknown">не указано</span>' in card
    assert " образование</li>" not in card
    page = (await client.get(f"/p/{brother.number}")).text
    assert page.count('<span class="unknown">не указано</span>') == 4


async def test_complete_page_asks_only_the_missing_fields(client, dev_login, make_profile):
    me = await _incomplete_profile(make_profile, education=Education.HIGHER)
    await dev_login(client, me.user)
    html = (await client.get("/me/complete")).text
    for name in ("marital_status", "children", "employment"):
        assert f'name="{name}"' in html, name
    for name in ("education", "city", "wishes", "birth_year", "about"):
        assert f'name="{name}"' not in html, name
    assert 'class="chips chips--big"' in html and 'class="chips chips--numbers"' in html
    assert "не требуют повторной модерации" in html


async def test_completing_keeps_the_profile_published(client, dev_login, make_profile, session):
    me = await _incomplete_profile(make_profile, gender=Gender.FEMALE)
    approved_at = me.approved_at
    await dev_login(client, me.user)
    csrf = await csrf_of(client, "/me/complete")

    blank = await client.post("/me/complete", data={"csrf_token": csrf, "marital_status": "divorced"})
    assert blank.status_code == 422 and "Ответьте на все вопросы" in blank.text and "Обязательное поле." in blank.text
    assert 'value="divorced" checked' in blank.text  # the chosen answer is kept

    form = {
        "csrf_token": csrf,
        "marital_status": "divorced",
        "children": "2",
        "education": "religious",
        "employment": "business",
    }
    response = await client.post("/me/complete", data=form)
    assert response.status_code == 303 and response.headers["location"] == "/me"
    profile = await profiles.get_profile(session, me.user_id)
    await session.refresh(profile)
    assert (profile.marital_status, profile.children, profile.education, profile.employment) == (
        MaritalStatus.DIVORCED,
        2,
        Education.RELIGIOUS,
        Employment.BUSINESS,
    )
    assert profile.status == ProfileStatus.ACTIVE and profile.approved_at == approved_at
    submitted = select(func.count()).select_from(Outbox).where(Outbox.kind == EventKind.PROFILE_SUBMITTED.value)
    assert await session.scalar(submitted) == 0
    me_page = (await client.get("/me")).text
    assert "Спасибо, анкета дополнена." in me_page and "Дополнить анкету (1 минута)" not in me_page
    assert (await client.get("/me/complete")).headers["location"] == "/me"


async def test_choice_edit_keeps_status_and_text_edit_goes_to_moderation(client, dev_login, make_profile, session):
    me = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    csrf = await csrf_of(client, "/me/edit")
    same = {"csrf_token": csrf, "city": me.city, "wishes": me.wishes}
    await client.post("/me/edit", data=profile_form(**same, education="religious"))
    profile = await profiles.get_profile(session, me.user_id)
    await session.refresh(profile)
    assert profile.status == ProfileStatus.ACTIVE
    assert "Изменения сохранены." in (await client.get("/me")).text

    await client.post("/me/edit", data=profile_form(**same | {"city": "Уфа"}, education="religious"))
    await session.refresh(profile)
    assert profile.status == ProfileStatus.PENDING
    assert "отправлены на модерацию" in (await client.get("/me")).text


# --- Consent gate for migrated accounts ---------------------------------------------------------


async def test_migrated_user_is_asked_for_consent_first(client, dev_login, make_profile, make_user, session):
    user = await make_user(consent=False)
    user_id = user.id
    profile = await make_profile(user, gender=Gender.FEMALE)
    await dev_login(client, user)
    for path in ("/catalog", "/me", "/interests", "/chats", "/settings", f"/p/{profile.number}", "/me/complete"):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/welcome?next="), path
    htmx = await client.get("/catalog", headers={"HX-Request": "true"})
    assert htmx.status_code == 204 and htmx.headers["HX-Redirect"] == "/welcome"
    for path in ("/rules", "/privacy", "/consent", "/terms", "/"):
        page = await client.get(path)
        assert page.status_code == 200, path
        assert 'class="nav-desktop"' not in page.text and 'class="tabbar"' not in page.text, path
    landing = (await client.get("/")).text
    assert "Нужно ваше согласие" in landing and 'href="/welcome"' in landing

    welcome = await client.get("/welcome?next=/chats")
    assert welcome.status_code == 200
    html = welcome.text
    assert f"Ваша <b>Анкета №{profile.number}</b>, отклики и чаты сохранены" in html
    assert "«сайт + бот»" in html and 'href="/consent"' in html and 'href="/rules"' in html
    assert "религиозных убеждениях" in html and 'action="/me/delete"' in html

    csrf = await csrf_of(client, "/welcome")
    refused = await client.post("/welcome", data={"csrf_token": csrf, "consent_pd": "yes", "next": "/chats"})
    assert refused.status_code == 422 and "Нужно принять правила" in refused.text
    assert (await users.get_by_id(session, user_id)).consent_at is None

    done = await client.post(
        "/welcome", data={"csrf_token": csrf, "consent_pd": "yes", "accept_rules": "yes", "next": "/chats"}
    )
    assert done.status_code == 303 and done.headers["location"] == "/chats"
    session.expire_all()
    assert (await users.get_by_id(session, user_id)).consent_at is not None
    assert "Согласие сохранено" in (await client.get("/chats")).text
    assert (await client.get("/welcome")).headers["location"] == "/catalog"


async def test_consent_gate_allows_logout_and_deletion(client, dev_login, make_profile, make_user, session):
    user = await make_user(consent=False)
    await make_profile(user)
    await dev_login(client, user)
    csrf = await csrf_of(client, "/welcome")
    gated_post = await client.post("/me/hide", data={"csrf_token": csrf})
    assert gated_post.status_code == 303 and gated_post.headers["location"] == "/welcome"
    kept = await client.post("/me/delete", data={"csrf_token": csrf, "back": "/welcome"})
    assert kept.headers["location"] == "/welcome"
    response = await client.post("/me/delete", data={"csrf_token": csrf, "confirm": "yes", "back": "/welcome"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    session.expunge_all()
    assert await users.get_by_id(session, user.id) is None


async def test_new_account_without_profile_goes_to_the_wizard(client, dev_login, make_user):
    await dev_login(client, await make_user(consent=False))
    for path in ("/catalog", "/chats", "/settings", "/welcome"):
        response = await client.get(path)
        assert response.status_code == (303 if path != "/welcome" else 200), path
        if path != "/welcome":
            assert response.headers["location"] == "/profile/new", path
    assert (await client.get("/profile/new")).status_code == 200


async def test_consent_is_not_required_for_admin_pages(client, dev_login, make_user):
    admin = await make_user(consent=False, is_admin=True)
    await dev_login(client, admin)
    assert (await client.get("/admin")).status_code == 200


async def test_consent_required_from_a_service_leads_to_the_consent_page():
    from app.web.main import create_app

    app: FastAPI = create_app()

    @app.get("/_needs_consent")
    async def needs_consent() -> None:
        raise ConsentRequired()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        page = await http.get("/_needs_consent")
        assert page.status_code == 303 and page.headers["location"] == "/welcome?next=%2F_needs_consent"
        htmx = await http.get("/_needs_consent", headers={"HX-Request": "true"})
        assert htmx.status_code == 204 and htmx.headers["HX-Redirect"] == "/welcome"


# --- Chat: witness, contact filter, inline errors, message reports --------------------------------


@pytest.fixture
async def couple(make_pair, session):
    man, woman = await make_pair()
    conversation = await _open_chat(session, man, woman)
    return man, woman, conversation


async def test_witness_notice_and_witness_messages(client, dev_login, couple, make_user, session):
    from app.services import witness

    man, woman, conversation = couple
    admin = await make_user(is_admin=True)
    await chat.send_message(session, conversation.id, woman, "Ва алейкум ассалям", MessageOrigin.TELEGRAM)
    note = await witness.witness_say(session, admin, conversation.id, "Пожалуйста, без личных контактов.")
    await session.commit()
    await dev_login(client, man)
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert "Переписку видит свидетель (махрам/администратор)" in page
    item = re.search(rf'<li class="msg msg--witness" id="msg-{note.id}".*?</li>', page, re.S)
    assert item and "Свидетель" in item.group(0) and "Пожалуйста, без личных контактов." in item.group(0)
    assert f"/messages/{note.id}/report" not in page  # the witness cannot be reported
    listing = (await client.get("/chats")).text
    assert "Свидетель: Пожалуйста, без личных контактов." in listing

    await witness.witness_close(session, admin, conversation.id)
    await session.commit()
    closed = (await client.get(f"/chats/{conversation.id}")).text
    assert "Свидетель завершил этот чат. Если есть вопросы — обратитесь к админу." in closed
    assert 'id="composer"' not in closed


async def test_history_from_earlier_years_shows_the_year(client, dev_login, couple, session):
    """Chats moved from the legacy bot are years old: «01.04.2025 13:05», not a bare «01.04»."""
    man, woman, conversation = couple
    old = await chat.send_message(session, conversation.id, woman, "Из старого бота", MessageOrigin.TELEGRAM)
    stamp = datetime(2025, 4, 1, 10, 5, tzinfo=UTC)
    await session.execute(text("UPDATE messages SET created_at = :at WHERE id = :id"), {"at": stamp, "id": old.id})
    await session.execute(
        text("UPDATE conversations SET last_message_at = :at WHERE id = :id"), {"at": stamp, "id": conversation.id}
    )
    await session.commit()
    await dev_login(client, man)
    page = (await client.get(f"/chats/{conversation.id}")).text
    item = re.search(rf'<li class="msg" id="msg-{old.id}".*?</li>', page, re.S)
    assert item and "01.04.2025 13:05" in item.group(0)
    assert "01.04.2025 13:05" in (await client.get("/chats")).text


async def test_hidden_contacts_notice_and_stored_text(client, dev_login, couple, session):
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    response = await client.post(
        f"/chats/{conversation.id}/messages",
        data={"csrf_token": csrf, "text": "Мой номер +7 912 345-67-89, пишите", "after_id": "0"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 201
    assert json.loads(response.headers["HX-Trigger"])["toast"]["message"] == chat.CONTACTS_HIDDEN_NOTICE
    assert "[скрыто свидетелем]" in response.text and "345-67-89" not in response.text
    assert "часть текста скрыта фильтром" in response.text

    plain = await client.post(
        f"/chats/{conversation.id}/messages", data={"csrf_token": csrf, "text": "Пишите в t.me/brother_umar"}
    )
    assert plain.status_code == 303
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert chat.CONTACTS_HIDDEN_NOTICE in page and "brother_umar" not in page

    clean = await client.post(
        f"/chats/{conversation.id}/messages",
        data={"csrf_token": csrf, "text": "Ассаляму алейкум", "after_id": "0"},
        headers={"HX-Request": "true"},
    )
    assert clean.status_code == 201 and "HX-Trigger" not in clean.headers


async def test_rate_limit_is_shown_next_to_the_composer(client, dev_login, couple, override_settings):
    override_settings(CHAT_RATE_LIMIT_PER_MIN=1)
    man, _, conversation = couple
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    url = f"/chats/{conversation.id}/messages"
    assert (await client.post(url, data={"csrf_token": csrf, "text": "Первое"})).status_code == 303
    second = await client.post(url, data={"csrf_token": csrf, "text": "Второе сообщение"})
    assert second.status_code == 422
    assert re.search(r'id="composer-status" role="alert">Вы отправляете сообщения слишком часто', second.text)
    assert ">Второе сообщение</textarea>" in second.text  # the typed text is kept
    htmx = await client.post(url, data={"csrf_token": csrf, "text": "Третье"}, headers={"HX-Request": "true"})
    assert htmx.status_code == 422 and "toast" in htmx.headers["HX-Trigger"]


async def test_open_chat_limit_is_explained_on_the_interest_card(client, dev_login, couple, make_profile, session):
    _, woman, _ = couple
    other = await make_profile(gender=Gender.MALE)
    interest = await interests.send_interest(session, other.user, woman.id)
    await session.commit()
    await dev_login(client, woman)
    csrf = await csrf_of(client, "/interests")
    expected = "У одного из участников уже есть открытый чат. Завершите его сначала."
    card = await client.post(f"/interests/{interest.id}/accept", headers={"HX-Request": "true", "X-CSRF-Token": csrf})
    assert card.status_code == 200 and f'id="interest-{interest.id}"' in card.text and expected in card.text
    assert "<html" not in card.text
    page = await client.post(f"/interests/{interest.id}/accept", data={"csrf_token": csrf})
    assert page.status_code == 409 and expected in page.text and 'role="alert"' in page.text


async def test_report_a_single_message(client, dev_login, couple, session):
    from app.models import Message, Report

    man, woman, conversation = couple
    theirs = await chat.send_message(session, conversation.id, woman, "Переведите мне деньги", MessageOrigin.WEB)
    mine = await chat.send_message(session, conversation.id, man, "Нет", MessageOrigin.WEB)
    await session.commit()
    await dev_login(client, man)
    page = (await client.get(f"/chats/{conversation.id}")).text
    assert f'href="/chats/{conversation.id}/messages/{theirs.id}/report">Пожаловаться</a>' in page
    assert f"/messages/{mine.id}/report" not in page

    form_page = await client.get(f"/chats/{conversation.id}/messages/{theirs.id}/report")
    assert form_page.status_code == 200 and "Переведите мне деньги" in form_page.text
    assert (await client.get(f"/chats/{conversation.id}/messages/{mine.id}/report")).status_code == 404
    csrf = await csrf_of(client, f"/chats/{conversation.id}")
    url = f"/chats/{conversation.id}/messages/{theirs.id}/report"
    response = await client.post(url, data={"csrf_token": csrf, "reason": "fraud"})
    assert response.status_code == 303 and response.headers["location"] == f"/chats/{conversation.id}"
    report = await session.scalar(select(Report))
    assert (report.message_id, report.target_user_id, report.reporter_id) == (theirs.id, woman.id, man.id)
    flagged = await session.get(Message, theirs.id)
    await session.refresh(flagged)
    assert flagged.flagged and "user report" in flagged.flag_reason
    chat_page = (await client.get(f"/chats/{conversation.id}")).text
    assert "Жалоба на сообщение отправлена свидетелю и модератору" in chat_page
    again = await client.post(url, data={"csrf_token": csrf, "reason": "fraud"})
    assert again.headers["location"] == url
    assert "Вы уже пожаловались на это сообщение" in (await client.get(url)).text


# --- Branding and texts ---------------------------------------------------------------------------


async def test_branding_logo_and_principles(client):
    html = (await client.get("/")).text
    assert "<title>Никах Истина — знакомство для никаха" in html and 'alt="Эмблема «Никах Истина»' in html
    for asset in ("img/logo.png", "img/logo@2x.png", "img/favicon-32.png", "img/apple-touch-icon.png"):
        assert f"/static/{asset}?v=" in html, asset
        assert (await client.get(f"/static/{asset}")).headers["content-type"] == "image/png"
    assert "без имён и фото" in html and "Переписку видит свидетель" in html
    assert "Одна система с ботом @nikah_istina_bot" in html
    css = (await client.get("/static/css/app.css")).text
    for color in ("#558095", "#28495b", "#f1cbbc", "#fdefe3", "#547177"):
        assert color in css, color


async def test_legal_pages_describe_the_witness_and_the_migration(client):
    rules = (await client.get("/rules")).text
    assert rules.count("<li>", rules.index('class="rules-list"'), rules.index("</ol>")) == 7
    assert "Все сообщения видит свидетель (махрам/администратор)" in rules
    privacy = (await client.get("/privacy")).text
    assert "Копии всех сообщений видит свидетель" in privacy and "личный вали" in privacy
    assert "Перенос данных из Telegram-бота" in privacy
    consent = (await client.get("/consent")).text
    assert "свидетелю — копии всех сообщений" in consent and "перенесены на платформу" in consent
    for page in (rules, privacy, consent, (await client.get("/terms")).text):
        assert "Шаблон — требует проверки юристом перед запуском" in page and "«Никах Истина»" in page
