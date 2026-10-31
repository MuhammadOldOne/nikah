"""Catalog: access rules, visibility, filters, pagination and the HTMX partial."""

from app.clock import current_year
from app.enums import Gender, MaritalStatus, ProfileStatus
from app.services import guardians
from app.text import profile_title


async def test_catalog_requires_a_profile(client, dev_login, make_user):
    await dev_login(client, await make_user())
    response = await client.get("/catalog")
    assert response.status_code == 303 and response.headers["location"] == "/profile/new"


async def test_catalog_shows_only_active_opposite_gender_profiles(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.MALE)
    aisha = await make_profile(gender=Gender.FEMALE)
    khadija = await make_profile(gender=Gender.FEMALE)
    zaynab = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.PENDING)
    sumayya = await make_profile(gender=Gender.FEMALE, status=ProfileStatus.HIDDEN)
    umar = await make_profile(gender=Gender.MALE)
    await dev_login(client, me.user)

    response = await client.get("/catalog")
    assert response.status_code == 200
    html = response.text
    assert "Анкеты сестёр" in html
    assert profile_title(aisha) in html and profile_title(khadija) in html
    for hidden in (zaynab, sumayya, umar):
        assert profile_title(hidden) not in html
    assert "Найдено: 2 анкеты" in html
    assert 'name="robots" content="noindex, nofollow"' in html


async def test_catalog_card_shows_wali_badge_and_short_wishes(client, dev_login, make_profile, session):
    me = await make_profile(gender=Gender.MALE)
    long_wishes = "Ищу благочестивого мужа. " * 20
    sister = await make_profile(gender=Gender.FEMALE, wishes=long_wishes.strip())
    wali = await make_profile(gender=Gender.MALE)
    link = await guardians.create_invite(session, sister.user)
    await guardians.accept_invite(session, link.rsplit("wali_", 1)[1], wali.user)
    await session.commit()
    await dev_login(client, me.user)
    html = (await client.get("/catalog")).text
    assert "👁 Вали подключён" in html
    assert long_wishes.strip() not in html
    assert "Ищу благочестивого мужа." in html


async def test_catalog_filters(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.MALE)
    year = current_year()
    amina = profile_title(await make_profile(gender=Gender.FEMALE, city="Уфа", birth_year=year - 22))
    leyla = profile_title(
        await make_profile(
            gender=Gender.FEMALE, city="Казань", birth_year=year - 35, marital_status=MaritalStatus.DIVORCED
        )
    )
    madina = profile_title(await make_profile(gender=Gender.FEMALE, city="Казань", birth_year=year - 28, children=2))
    await dev_login(client, me.user)

    html = (await client.get("/catalog", params={"city": "казань"})).text
    assert leyla in html and madina in html and amina not in html
    html = (await client.get("/catalog", params={"age_min": "30"})).text
    assert leyla in html and madina not in html
    html = (await client.get("/catalog", params={"has_children": "true"})).text
    assert madina in html and leyla not in html
    html = (await client.get("/catalog", params={"marital_status": "divorced"})).text
    assert leyla in html and amina not in html
    html = (await client.get("/catalog", params={"age_min": "5", "city": "Уфа"})).text
    assert "Часть фильтров не применена" in html and amina in html


async def test_catalog_empty_state(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.FEMALE)
    await dev_login(client, me.user)
    assert "Пока здесь пусто" in (await client.get("/catalog")).text
    assert "По этим фильтрам никого нет" in (await client.get("/catalog?city=Москва")).text


async def test_catalog_pagination_and_htmx_partial(client, dev_login, make_profile):
    me = await make_profile(gender=Gender.MALE)
    for _ in range(14):
        await make_profile(gender=Gender.FEMALE, city="Казань")
    await dev_login(client, me.user)

    first = await client.get("/catalog?city=Казань")
    assert first.text.count('class="card pcard"') == 12
    assert "page=2" in first.text and "city=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C&amp;page=2" in first.text

    partial = await client.get("/catalog?city=Казань&page=2", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert partial.text.lstrip().startswith('<section id="results"')
    assert "<html" not in partial.text
    assert partial.text.count('class="card pcard"') == 2
    assert "HX-Request" in partial.headers["vary"]


async def test_page_beyond_the_last_goes_to_the_last_page(client, dev_login, make_profile):
    man = (await make_profile()).user
    for _ in range(13):  # two pages of 12
        await make_profile(gender=Gender.FEMALE, city="Уфа")
    await dev_login(client, man)

    response = await client.get("/catalog", params={"page": "99", "city": "Уфа"})
    assert response.status_code == 303 and response.headers["location"] == "/catalog?city=%D0%A3%D1%84%D0%B0&page=2"
    htmx = await client.get("/catalog", params={"page": "3"}, headers={"HX-Request": "true"})
    assert htmx.status_code == 204 and htmx.headers["HX-Redirect"] == "/catalog?page=2"
    assert (await client.get("/catalog", params={"page": "5", "city": "Нигде"})).headers["location"] == (
        "/catalog?city=%D0%9D%D0%B8%D0%B3%D0%B4%D0%B5"
    )
    page = await client.get("/catalog", params={"page": "2"})
    assert page.status_code == 200 and "Пока здесь пусто" not in page.text
