import pytest
from pydantic import ValidationError

from app.clock import current_year
from app.enums import Education, Gender, Madhab
from app.schemas import (
    CONTACTS_MESSAGE,
    REQUIRED_MESSAGE,
    CatalogFilters,
    ChatMessageIn,
    InterestIn,
    ModerationNoteIn,
    ProfileCreate,
    ProfileUpdate,
    ReportIn,
    first_error,
    humanize_errors,
)
from app.text import clean_line, clean_text, find_contacts, plural


def valid_profile(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "gender": "female",
        "birth_year": current_year() - 25,
        "city": "Казань",
        "marital_status": "never_married",
        "children": 0,
        "education": "higher",
        "employment": "employed",
        "wishes": "Ищу верующего и ответственного мужа.",
    }
    data.update(overrides)
    return data


def errors_of(model: type, data: dict[str, object]) -> dict[str, str]:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(data)
    return humanize_errors(exc_info.value)


def test_minimal_profile_is_valid_and_optional_fields_default_to_none():
    profile = ProfileCreate.model_validate(valid_profile(height="", madhab="", about="   "))
    assert profile.gender is Gender.FEMALE
    assert profile.height is None and profile.madhab is None and profile.about is None


def test_all_required_fields_are_required():
    errors = errors_of(ProfileCreate, {})
    assert set(errors) == {
        "gender",
        "birth_year",
        "city",
        "marital_status",
        "children",
        "education",
        "employment",
        "wishes",
    }
    assert all(message == REQUIRED_MESSAGE for message in errors.values())
    assert errors_of(ProfileCreate, valid_profile(city="  ", education=""))["city"] == REQUIRED_MESSAGE


def test_optional_fields_are_parsed():
    profile = ProfileCreate.model_validate(
        valid_profile(
            height="175",
            weight=70,
            occupation="  инженер ",
            nationality="татарка",
            madhab="hanafi",
            prayer="always",
            nikah_timeline="year",
            relocation="negotiable",
            about="Люблю\r\n\r\n\r\nчитать  ",
        )
    )
    assert profile.height == 175 and profile.weight == 70
    assert profile.occupation == "инженер"
    assert profile.madhab is Madhab.HANAFI
    assert profile.about == "Люблю\n\nчитать"


@pytest.mark.parametrize(
    ("years_ago", "ok"),
    [(17, False), (18, True), (80, True), (81, False)],
)
def test_age_limits(years_ago: int, ok: bool):
    data = valid_profile(birth_year=current_year() - years_ago)
    if ok:
        ProfileCreate.model_validate(data)
    else:
        assert "birth_year" in errors_of(ProfileCreate, data)


@pytest.mark.parametrize(
    ("field", "bad", "good"),
    [
        ("height", 139, 220),
        ("height", 221, 140),
        ("weight", 34, 35),
        ("weight", 201, 200),
        ("children", -1, 0),
        ("children", 16, 15),
    ],
)
def test_numeric_ranges(field: str, bad: int, good: int):
    assert field in errors_of(ProfileCreate, valid_profile(**{field: bad}))
    ProfileCreate.model_validate(valid_profile(**{field: good}))


def test_text_lengths():
    assert "wishes" in errors_of(ProfileCreate, valid_profile(wishes="коротко"))
    assert "wishes" in errors_of(ProfileCreate, valid_profile(wishes="а" * 1001))
    assert "about" in errors_of(ProfileCreate, valid_profile(about="а" * 2001))
    assert "occupation" in errors_of(ProfileCreate, valid_profile(occupation="а" * 101))


@pytest.mark.parametrize(
    "contact",
    [
        "https://example.com",
        "www.example.org",
        "пишите в t.me/fatima",
        "почта fatima@mail.ru",
        "мой ник @fatima_99",
        "звоните +7 (999) 123-45-67",
        "89991234567",
        "сайт fatima.ru",
    ],
)
def test_contacts_are_rejected_in_public_texts(contact: str):
    for field in ("about", "wishes"):
        errors = errors_of(ProfileCreate, valid_profile(**{field: f"Ищу мужа, {contact}"}))
        assert errors[field] == CONTACTS_MESSAGE


def test_city_rules():
    assert ProfileCreate.model_validate(valid_profile(city="ростов-на-Дону")).city == "Ростов-на-Дону"
    assert ProfileCreate.model_validate(valid_profile(city="  Набережные   Челны ")).city == "Набережные Челны"
    assert "city" in errors_of(ProfileCreate, valid_profile(city="Казань!!!"))


def test_invalid_enum_value_has_russian_message():
    errors = errors_of(ProfileCreate, valid_profile(gender="other", education="phd"))
    assert errors == {"gender": "Выберите значение из списка.", "education": "Выберите значение из списка."}
    assert errors_of(ProfileCreate, valid_profile(children="много"))["children"] == "Введите целое число."


def test_profile_update_is_partial_and_rejects_clearing_required_fields():
    update = ProfileUpdate.model_validate({"city": "орёл", "about": "", "gender": "male"})
    assert update.changes() == {"city": "Орёл", "about": None}
    assert errors_of(ProfileUpdate, {"marital_status": ""}) == {"marital_status": REQUIRED_MESSAGE}
    assert errors_of(ProfileUpdate, {"wishes": None}) == {"wishes": REQUIRED_MESSAGE}
    assert ProfileUpdate.model_validate({}).changes() == {}


def test_first_error_prefixes_field_label():
    with pytest.raises(ValidationError) as exc_info:
        ProfileCreate.model_validate(valid_profile(height=100))
    assert first_error(exc_info.value) == "Рост, см: Рост — от 140 до 220 см."


def test_catalog_filters():
    filters = CatalogFilters.model_validate(
        {"age_min": "40", "age_max": "25", "city": " казань ", "has_children": "false", "education": ""}
    )
    assert (filters.age_min, filters.age_max) == (25, 40)
    assert filters.city == "казань"
    assert filters.has_children is False and filters.education is None
    assert CatalogFilters.model_validate({"education": "religious"}).education is Education.RELIGIOUS
    assert "age_min" in errors_of(CatalogFilters, {"age_min": 10})


def test_interest_message():
    assert InterestIn(message="   ").message is None
    assert InterestIn(message=None).message is None
    assert InterestIn(message=" Ас-саляму алейкум ").message == "Ас-саляму алейкум"
    assert "message" in errors_of(InterestIn, {"message": "а" * 501})
    assert errors_of(InterestIn, {"message": "мой телеграм @ahmad_k"})["message"] == CONTACTS_MESSAGE


def test_chat_message():
    assert ChatMessageIn(text="  привет \n").text == "привет"
    assert ChatMessageIn(text="а" * 2000).text == "а" * 2000
    assert "text" in errors_of(ChatMessageIn, {"text": " \n\t "})
    assert "text" in errors_of(ChatMessageIn, {"text": "а" * 2001})
    # Contacts are allowed in chats: both sides have agreed to talk.
    assert ChatMessageIn(text="+7 999 123-45-67").text == "+7 999 123-45-67"


def test_report_and_moderation_note():
    assert ReportIn(reason="fraud", comment="  ").comment is None
    assert "reason" in errors_of(ReportIn, {"reason": "spam"})
    assert "comment" in errors_of(ReportIn, {"reason": "other", "comment": "а" * 1001})
    assert "note" in errors_of(ModerationNoteIn, {"note": "  "})


def test_text_helpers():
    assert clean_line("  a​ b\n c\x00 ") == "a b c"
    assert clean_text(" a \r\n\r\n\r\n b ") == "a\n\nb"
    assert find_contacts("рост 180, вес 75, 1995 г.р., т.е. всё") == []
    assert find_contacts("работала с 2010 по 2015 в Ростове-на-Дону") == []
    assert plural(1, "анкета", "анкеты", "анкет") == "анкета"
    assert plural(3, "анкета", "анкеты", "анкет") == "анкеты"
    assert plural(11, "анкета", "анкеты", "анкет") == "анкет"
    assert plural(22, "анкета", "анкеты", "анкет") == "анкеты"
    assert plural(25, "анкета", "анкеты", "анкет") == "анкет"


def test_zero_width_joiner_is_kept_only_inside_emoji_sequences():
    family, heart_on_fire, coder = "👨‍👩‍👧", "❤️‍🔥", "👨🏽‍💻"
    assert ChatMessageIn(text=f"{family} {heart_on_fire} {coder}").text == f"{family} {heart_on_fire} {coder}"
    # Between letters or digits it would hide contacts from the checks: removed.
    assert clean_line("@na‍me_123") == "@name_123" and find_contacts(clean_line("@na‍me_123"))
    assert find_contacts(clean_text("8‍912‍345‍67‍89")) == ["89123456789"]
    assert clean_line("✋‍ ‍👩 ‮текст") == "✋ 👩 текст"
