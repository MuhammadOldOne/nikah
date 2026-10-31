"""Public pages, error pages and security headers."""

from app.enums import Gender, ProfileStatus


async def test_landing_renders_for_anonymous_visitors(client, make_profile):
    await make_profile(gender=Gender.MALE)
    await make_profile(gender=Gender.FEMALE)
    await make_profile(gender=Gender.FEMALE)
    await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)

    response = await client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "Знакомство для никаха" in html
    assert "Создать анкету за 2 минуты" in html
    assert 'href="/profile/new"' in html
    assert "Перейти к анкетам" not in html
    # Live counters: only published profiles.
    assert '<div class="counter-card__num">1</div>' in html
    assert '<div class="counter-card__num">2</div>' in html
    assert "https://t.me/nikah_istina_bot" in html
    assert 'content="index, follow"' in html
    assert "set-cookie" not in response.headers  # no session for anonymous visitors


async def test_landing_promises_premoderation_only_when_it_is_on(client, override_settings):
    assert "Модератор проверит её перед публикацией" in (await client.get("/")).text
    override_settings(PREMODERATION=False)
    html = (await client.get("/")).text
    assert "Модератор проверит" not in html and "Каждая анкета проверяется" not in html
    assert "Модератор рассматривает жалобы" in html


async def test_landing_for_logged_in_user_with_profile(client, login_client, make_profile):
    profile = await make_profile()
    login_client(client, profile.user)
    html = (await client.get("/")).text
    assert "Перейти к анкетам" in html
    assert 'href="/catalog"' in html


async def test_security_headers_and_no_store(client):
    response = await client.get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self';" in csp and "unsafe-inline" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


async def test_static_files_are_served_with_cache_headers(client):
    response = await client.get("/static/css/app.css?v=1")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    for path in ("/static/vendor/htmx.min.js", "/static/img/avatar-male.svg", "/static/img/avatar-female.svg"):
        assert (await client.get(path)).status_code == 200


async def test_pages_reference_no_external_resources(client):
    html = (await client.get("/")).text
    assert "<script" in html
    for marker in ("cdn.", "googleapis", "unpkg", "jsdelivr", "style=", "onclick"):
        assert marker not in html


async def test_robots_txt(client):
    response = await client.get("/robots.txt")
    assert response.status_code == 200
    body = response.text
    assert "Disallow: /\n" in body
    for allowed in (
        "Allow: /$",
        "Allow: /login",
        "Allow: /privacy",
        "Allow: /consent",
        "Allow: /terms",
        "Allow: /rules",
    ):
        assert allowed in body


async def test_legal_pages_show_lawyer_note(client):
    for path, title in (
        ("/privacy", "Политика конфиденциальности"),
        ("/consent", "Согласие на обработку персональных данных"),
        ("/terms", "Пользовательское соглашение"),
        ("/rules", "Правила сообщества"),
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert title in response.text
        assert "Шаблон — требует проверки юристом перед запуском" in response.text
        assert "[ФИО / ИНН / адрес оператора]" in response.text


async def test_styled_404_page(client):
    response = await client.get("/no-such-page")
    assert response.status_code == 404
    assert "Страница не найдена" in response.text
    assert 'name="robots" content="noindex, nofollow"' in response.text


async def test_private_pages_redirect_anonymous_users_to_login(client):
    for path in (
        "/catalog",
        "/interests",
        "/chats",
        "/me",
        "/me/complete",
        "/settings",
        "/profile/new",
        "/p/500",
        "/welcome",
    ):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


async def test_unhandled_errors_render_a_styled_500_page():
    import httpx

    from app.web.main import create_app

    app = create_app()

    @app.get("/_boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/_boom")
    assert response.status_code == 500
    assert "Что-то пошло не так" in response.text and "boom" not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_unhandled_errors_are_answered_once_and_logged_without_personal_data(caplog):
    import httpx

    from app.web.main import create_app

    app = create_app()
    secret = "-".join(("SECRET", "ABOUT", "TEXT"))  # not a literal: tracebacks quote source lines

    @app.get("/_boom")
    async def boom() -> None:
        raise RuntimeError(f"[parameters: ('Мариям', 'hanafi', {secret!r})]")

    # raise_app_exceptions=True (default): the error must not escape to the server, which would
    # log it again with its message.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        response = await http.get("/_boom", headers={"HX-Request": "true"})
    assert response.status_code == 500 and secret not in response.text
    logged = "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)
    assert "unhandled error on GET /_boom: RuntimeError" in logged
    assert secret not in logged and not any(record.exc_info for record in caplog.records)


async def test_database_errors_do_not_quote_bound_values(session, make_user):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.models import User

    existing = await make_user()
    session.add(User(telegram_id=existing.telegram_id, first_name="SECRET-NAME"))
    with pytest.raises(IntegrityError) as caught:
        await session.flush()
    assert "SECRET-NAME" not in str(caught.value)


async def test_out_of_range_ids_are_bad_requests(client, login_client, make_profile, caplog):
    profile = await make_profile()
    login_client(client, profile.user)
    huge = "99999999999999999999"
    for path in (f"/p/{huge}", f"/chats/{huge}", f"/chats/1/messages?after_id={huge}", "/p/2147483648"):
        response = await client.get(path)
        assert response.status_code == 400, path
        assert "Неверный запрос" in response.text
    assert (await client.get("/p/2147483647")).status_code == 404
    assert (await client.get("/chats/9223372036854775807")).status_code == 404
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_auth_query_strings_are_not_written_to_the_access_log():
    import logging

    from app.web.security import AccessLogRedaction

    def line(path: str) -> str:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", path, "1.1", 303),
            None,
        )
        AccessLogRedaction().filter(record)
        return record.getMessage()

    assert line("/auth/telegram?id=1&auth_date=2&hash=abc") == '1.2.3.4:5 - "GET /auth/telegram HTTP/1.1" 303'
    assert "tok" not in line("/auth/magic/tok123") and "/auth/magic/" in line("/auth/magic/tok123")
    assert "token" not in line("/auth/poll?token=xyz&next=%2F")
    assert line("/catalog?city=Уфа").endswith('"GET /catalog?city=Уфа HTTP/1.1" 303')


async def test_method_not_allowed_page(client):
    response = await client.get("/logout")
    assert response.status_code == 405 and "Действие недоступно" in response.text
