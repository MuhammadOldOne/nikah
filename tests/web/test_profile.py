"""Profile wizard, own profile page, editing, hiding and account deletion."""

from app.clock import current_year
from app.enums import Gender, ProfileStatus
from app.models import User
from app.services import profiles, users
from tests.web.conftest import csrf_of, profile_form


async def test_wizard_requires_login(client):
    response = await client.get("/profile/new")
    assert response.status_code == 303 and response.headers["location"] == "/login?next=%2Fprofile%2Fnew"


async def test_wizard_renders_three_steps(client, dev_login, make_user):
    await dev_login(client, await make_user())
    html = (await client.get("/profile/new")).text
    for title in ("Основное", "Семья и жизнь", "Пожелания", "Дополнительно (необязательно)"):
        assert title in html
    for name in ("consent_pd", "confirm_age", "accept_rules"):
        assert f'name="{name}"' in html
    assert 'href="/consent"' in html
    assert f'<option value="{current_year() - 18}">' in html
    assert f'<option value="{current_year() - 17}">' not in html


async def test_wizard_happy_path(client, dev_login, make_user, session):
    user = await make_user(consent=False)
    await dev_login(client, user)
    csrf = await csrf_of(client, "/profile/new")
    response = await client.post("/profile/new", data=profile_form(csrf_token=csrf, prayer="always"))
    assert response.status_code == 303 and response.headers["location"] == "/catalog"

    profile = await profiles.get_profile(session, user.id)
    assert profile is not None and profile.status == ProfileStatus.PENDING
    assert (profile.name, profile.gender, profile.prayer.value) == ("Марьям", Gender.FEMALE, "always")
    refreshed = await users.get_by_id(session, user.id)
    await session.refresh(refreshed)
    assert refreshed.consent_at is not None

    catalog = await client.get("/catalog")
    assert "Анкета отправлена на модерацию" in catalog.text  # flash
    assert "Ваша анкета на модерации" in catalog.text  # banner
    assert (await client.get("/profile/new")).headers["location"] == "/me"


async def test_wizard_without_premoderation_publishes(client, dev_login, make_user, session, override_settings):
    override_settings(PREMODERATION=False)
    user = await make_user()
    await dev_login(client, user)
    csrf = await csrf_of(client, "/profile/new")
    await client.post("/profile/new", data=profile_form(csrf_token=csrf))
    profile = await profiles.get_profile(session, user.id)
    assert profile.status == ProfileStatus.ACTIVE


async def test_wizard_validation_errors_keep_values(client, dev_login, make_user, session):
    user = await make_user()
    await dev_login(client, user)
    csrf = await csrf_of(client, "/profile/new")
    data = profile_form(
        csrf_token=csrf,
        name="Марьям",
        city="",
        wishes="пишите @maryam_k",
        birth_year=str(current_year() - 16),
        consent_pd=None,
        accept_rules=None,
    )
    response = await client.post("/profile/new", data=data)
    assert response.status_code == 422
    html = response.text
    assert "Проверьте анкету" in html
    assert "Обязательное поле." in html  # city
    assert "Уберите ссылки, телефоны" in html  # wishes with a @username
    assert "Анкету можно создать только с 18 лет." in html
    assert "Без согласия на обработку данных" in html
    assert "Нужно принять правила" in html
    assert 'value="Марьям"' in html  # preserved
    assert "пишите @maryam_k</textarea>" in html
    assert 'data-initial-step="1"' in html
    assert 'name="confirm_age" value="yes" required checked' in html
    assert await profiles.get_profile(session, user.id) is None


async def test_wizard_escapes_user_input(client, dev_login, make_user):
    await dev_login(client, await make_user())
    csrf = await csrf_of(client, "/profile/new")
    response = await client.post(
        "/profile/new", data=profile_form(csrf_token=csrf, name="<script>alert(1)</script>", wishes="<b>x</b>")
    )
    assert response.status_code == 422
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


async def test_wizard_rejects_missing_csrf(client, dev_login, make_user):
    await dev_login(client, await make_user())
    response = await client.post("/profile/new", data=profile_form())
    assert response.status_code == 403


async def _login_with_profile(client, dev_login, make_profile, **fields) -> User:
    profile = await make_profile(**fields)
    await dev_login(client, profile.user)
    return profile.user


async def test_me_shows_status_and_moderation_note(client, dev_login, make_profile):
    await _login_with_profile(
        client, dev_login, make_profile, status=ProfileStatus.REJECTED, moderation_note="Уберите рекламу из анкеты."
    )
    html = (await client.get("/me")).text
    assert "Отклонена модератором" in html and "Уберите рекламу из анкеты." in html
    assert "Удалить аккаунт" in html


async def test_edit_profile(client, dev_login, make_profile, session):
    user = await _login_with_profile(client, dev_login, make_profile, gender=Gender.FEMALE)
    page = await client.get("/me/edit")
    assert page.status_code == 200 and 'value="Казань"' in page.text
    csrf = await csrf_of(client, "/me/edit")
    form = profile_form(csrf_token=csrf, city="Уфа", height="165")
    response = await client.post("/me/edit", data=form)
    assert response.status_code == 303 and response.headers["location"] == "/me"
    profile = await profiles.get_profile(session, user.id)
    await session.refresh(profile)
    assert (profile.city, profile.height, profile.status) == ("Уфа", 165, ProfileStatus.PENDING)
    assert "отправлены на модерацию" in (await client.get("/me")).text


async def test_edit_profile_validation_error(client, dev_login, make_profile):
    await _login_with_profile(client, dev_login, make_profile)
    csrf = await csrf_of(client, "/me/edit")
    response = await client.post("/me/edit", data=profile_form(csrf_token=csrf, height="300"))
    assert response.status_code == 422
    assert "Рост — от 140 до 220 см." in response.text


async def test_hide_and_show_profile(client, dev_login, make_profile, session):
    user = await _login_with_profile(client, dev_login, make_profile)
    csrf = await csrf_of(client, "/me")
    assert (await client.post("/me/hide", data={"csrf_token": csrf})).status_code == 303
    profile = await profiles.get_profile(session, user.id)
    await session.refresh(profile)
    assert profile.status == ProfileStatus.HIDDEN
    await client.post("/me/show", data={"csrf_token": csrf})
    await session.refresh(profile)
    assert profile.status == ProfileStatus.ACTIVE


async def test_delete_account_requires_confirmation(client, dev_login, make_profile, session):
    user = await _login_with_profile(client, dev_login, make_profile)
    csrf = await csrf_of(client, "/me")
    response = await client.post("/me/delete", data={"csrf_token": csrf, "back": "/me"})
    assert response.headers["location"] == "/me"
    assert await users.get_by_id(session, user.id) is not None

    response = await client.post("/me/delete", data={"csrf_token": csrf, "confirm": "yes"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    session.expunge_all()
    assert await users.get_by_id(session, user.id) is None
    assert "Аккаунт и все ваши данные удалены" in (await client.get("/")).text
    assert (await client.get("/me")).status_code == 303


async def test_delete_account_without_profile_from_settings(client, dev_login, make_user, session):
    user = await make_user()
    await dev_login(client, user)
    csrf = await csrf_of(client, "/settings")
    response = await client.post("/me/delete", data={"csrf_token": csrf, "confirm": "yes", "back": "/settings"})
    assert response.headers["location"] == "/"
    session.expunge_all()
    assert await users.get_by_id(session, user.id) is None


async def test_wizard_with_invalid_gender_shows_an_error(client, dev_login, make_user):
    await dev_login(client, await make_user())
    csrf = await csrf_of(client, "/profile/new")
    response = await client.post("/profile/new", data=profile_form(csrf_token=csrf, gender="other"))
    assert response.status_code == 422
    assert "Выберите значение из списка." in response.text


async def test_counterpart_with_an_edit_under_review_is_not_shown(client, login_client, make_pair, session):
    from app.schemas import ProfileUpdate
    from app.services import interests

    man, woman = await make_pair()
    await interests.send_interest(session, man, woman.id)
    await profiles.update_profile(session, man, ProfileUpdate(about="Непроверенный текст"))
    await session.commit()
    login_client(client, woman)

    response = await client.get(f"/u/{man.id}")
    assert response.status_code == 404
    assert "Анкета временно недоступна: её проверяет модератор." in response.text
    assert "Непроверенный текст" not in response.text
    assert "Непроверенный текст" not in (await client.get("/interests")).text


async def test_edit_refused_by_the_submission_limit_keeps_the_typed_text(client, dev_login, make_profile, session):
    from app.schemas import ProfileUpdate

    profile = await make_profile(gender=Gender.FEMALE)
    for number in range(profiles.SUBMISSIONS_PER_HOUR):
        await profiles.update_profile(session, profile.user, ProfileUpdate(about=f"Правка {number}"))
    await session.commit()
    await dev_login(client, profile.user)
    csrf = await csrf_of(client, "/me/edit")
    form = profile_form(csrf_token=csrf, about="Новый текст, который жалко потерять")

    response = await client.post("/me/edit", data=form)

    assert response.status_code == 422
    assert "Анкету слишком часто отправляли на проверку" in response.text
    assert "Новый текст, который жалко потерять" in response.text
