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
    for path in ("/catalog", "/interests", "/chats", "/me", "/settings", "/profile/new", "/u/1"):
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


async def test_method_not_allowed_page(client):
    response = await client.get("/logout")
    assert response.status_code == 405 and "Действие недоступно" in response.text
