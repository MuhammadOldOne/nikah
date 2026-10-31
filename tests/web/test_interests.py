"""Profile page rules, interests (send / accept / decline / cancel) and reports."""

from sqlalchemy import select

from app.enums import Gender, InterestStatus, ProfileStatus
from app.models import Conversation, Interest, Report
from app.services import interests
from tests.web.conftest import csrf_of


async def test_profile_page_404_rules(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.MALE)
    sister = await make_profile(gender=Gender.FEMALE)
    brother = await make_profile(gender=Gender.MALE)
    pending = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    hidden = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.HIDDEN)
    await dev_login(client, me.user)

    page = await client.get(f"/p/{sister.number}")
    assert page.status_code == 200 and f"Анкета №{sister.number} — Сестра" in page.text and "Откликнуться" in page.text
    for other in (brother, pending, hidden):
        response = await client.get(f"/p/{other.number}")
        assert response.status_code == 404
        assert "Страница не найдена" in response.text
    assert (await client.get("/p/999999")).status_code == 404
    assert (await client.get("/p/abc")).status_code == 404
    own = await client.get(f"/p/{me.number}")
    assert own.status_code == 200 and "Это ваша анкета" in own.text
    assert (await client.get(f"/u/{sister.user_id}")).status_code == 404  # internal ids are not addresses


async def test_pending_viewer_cannot_open_other_profiles(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.MALE, status=ProfileStatus.PENDING)
    sister = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    response = await client.get(f"/p/{sister.number}")
    assert response.status_code == 200  # a page that explains, not an error
    assert "Ваша анкета на модерации" in response.text and "Ваша анкета ещё не одобрена" in response.text
    assert f"Анкета №{sister.number}" not in response.text
    assert f'action="/p/{sister.number}/interest"' not in response.text


async def test_send_interest_and_accept_opens_chat(client, app, dev_login, make_profile, session):
    import httpx

    brother = await make_profile(gender=Gender.MALE)
    sister = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, brother.user)
    csrf = await csrf_of(client, f"/p/{sister.number}")
    response = await client.post(
        f"/p/{sister.number}/interest", data={"csrf_token": csrf, "message": "Ассаляму алейкум!"}
    )
    assert response.status_code == 303 and response.headers["location"] == f"/p/{sister.number}"
    page = (await client.get(f"/p/{sister.number}")).text
    assert "Отклик отправлен" in page and f"Отклик отправлен: Анкета №{sister.number}" in page
    interest = await session.scalar(select(Interest))
    assert interest.message == "Ассаляму алейкум!" and interest.status == InterestStatus.PENDING

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as her:
        await dev_login(her, sister.user)
        incoming = await her.get("/interests")
        assert "Ассаляму алейкум!" in incoming.text and "Принять и начать чат" in incoming.text
        nav = await her.get("/catalog")
        assert 'data-badge="interests">1<' in nav.text
        her_csrf = await csrf_of(her, "/interests")
        accepted = await her.post(f"/interests/{interest.id}/accept", data={"csrf_token": her_csrf})
        conversation = await session.scalar(select(Conversation))
        assert accepted.status_code == 303 and accepted.headers["location"] == f"/chats/{conversation.id}"
        chat_page = await her.get(f"/chats/{conversation.id}")
        assert chat_page.status_code == 200 and "Отклик принят" in chat_page.text
        assert f"чат с анкетой №{brother.number} открыт" in chat_page.text

    assert "Перейти в чат" in (await client.get(f"/p/{sister.number}")).text


async def test_accept_via_htmx_uses_hx_redirect(client, dev_login, make_pair, session):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()
    await dev_login(client, woman)
    csrf = await csrf_of(client, "/interests")
    response = await client.post(
        f"/interests/{interest.id}/accept", headers={"HX-Request": "true", "X-CSRF-Token": csrf}
    )
    assert response.status_code == 204 and response.headers["HX-Redirect"].startswith("/chats/")


async def test_decline_and_cancel_with_htmx_return_cards(client, app, dev_login, make_pair, make_profile, session):
    man, woman = await make_pair()
    other = (await make_profile(gender=Gender.MALE)).user
    declined = await interests.send_interest(session, man, woman.id)
    cancelled = await interests.send_interest(session, other, woman.id)
    await session.commit()

    await dev_login(client, woman)
    csrf = await csrf_of(client, "/interests")
    headers = {"HX-Request": "true", "X-CSRF-Token": csrf}
    response = await client.post(f"/interests/{declined.id}/decline", headers=headers)
    assert response.status_code == 200
    assert f'id="interest-{declined.id}"' in response.text and "Вы отклонили этот отклик" in response.text
    assert "<html" not in response.text

    again = await client.post(f"/interests/{declined.id}/decline", headers=headers)
    assert again.headers["HX-Reswap"] == "none" and "toast" in again.headers["HX-Trigger"]

    await dev_login(client, other)
    csrf = await csrf_of(client, "/interests?tab=out")
    response = await client.post(
        f"/interests/{cancelled.id}/cancel", headers={"HX-Request": "true", "X-CSRF-Token": csrf}
    )
    assert "Отклик отозван" in response.text
    await session.refresh(cancelled)
    assert cancelled.status == InterestStatus.CANCELLED


async def test_decline_without_js_redirects(client, dev_login, make_pair, session):
    man, woman = await make_pair()
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()
    await dev_login(client, woman)
    csrf = await csrf_of(client, "/interests")
    response = await client.post(f"/interests/{interest.id}/decline", data={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["location"] == "/interests"
    assert "Отклик отклонён" in (await client.get("/interests")).text


async def test_cannot_act_on_someone_elses_interest(client, dev_login, make_pair, make_profile, session):
    man, woman = await make_pair()
    stranger = (await make_profile(gender=Gender.FEMALE)).user
    interest = await interests.send_interest(session, man, woman.id)
    await session.commit()
    await dev_login(client, stranger)
    csrf = await csrf_of(client, "/interests")
    response = await client.post(f"/interests/{interest.id}/accept", data={"csrf_token": csrf})
    assert response.headers["location"] == "/interests"
    assert "Отклик не найден" in (await client.get("/interests")).text
    await session.refresh(interest)
    assert interest.status == InterestStatus.PENDING


async def test_interest_errors_are_shown(client, dev_login, make_pair, override_settings):
    override_settings(INTEREST_DAILY_LIMIT=1)
    man, woman = await make_pair()
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/p/{woman.profile.number}")
    draft = "Ассаляму алейкум! Расскажу о себе и своей семье подробно. Мой номер 89991234567"
    response = await client.post(f"/p/{woman.profile.number}/interest", data={"csrf_token": csrf, "message": draft})
    # The page comes back with the error next to the field and the typed message kept.
    assert response.status_code == 422
    assert 'id="interest-error" role="alert">Уберите ссылки, телефоны' in response.text
    assert f'aria-invalid="true">{draft}</textarea>' in response.text


async def test_report_from_profile_page(client, dev_login, make_pair, session):
    man, woman = await make_pair()
    number = woman.profile.number
    await dev_login(client, man)
    csrf = await csrf_of(client, f"/p/{number}")
    no_reason = await client.post(f"/p/{number}/report", data={"csrf_token": csrf, "reason": ""})
    assert no_reason.headers["location"] == f"/p/{number}"
    response = await client.post(
        f"/p/{number}/report", data={"csrf_token": csrf, "reason": "fraud", "comment": "Просит перевести деньги"}
    )
    assert response.status_code == 303 and response.headers["location"] == "/catalog"
    report = await session.scalar(select(Report))
    assert report.target_user_id == woman.id and report.comment == "Просит перевести деньги"
    assert f"Жалоба на анкету №{number} отправлена" in (await client.get("/catalog")).text
    assert (await client.get(f"/p/{number}")).status_code == 404  # hidden from each other now
