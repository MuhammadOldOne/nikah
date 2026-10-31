"""Form helpers for the profile wizard and the edit page."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from starlette.datastructures import FormData

from app.clock import current_year
from app.enums import Gender, MaritalStatus, Relocation, label
from app.models import Profile
from app.schemas import AGE_MAX, AGE_MIN, PROFILE_OPTIONAL_FIELDS, PROFILE_REQUIRED_FIELDS

PROFILE_FIELDS: tuple[str, ...] = PROFILE_REQUIRED_FIELDS + PROFILE_OPTIONAL_FIELDS
GENDER_VALUES = frozenset(member.value for member in Gender)

# Separate checkboxes on the last wizard step (field -> error message when not ticked).
CONSENT_FIELDS: dict[str, str] = {
    "consent_pd": "Без согласия на обработку данных мы не можем создать анкету.",
    "confirm_age": "Анкету можно создать только с 18 лет.",
    "accept_rules": "Нужно принять правила сообщества и пользовательское соглашение.",
}

# Wizard step of every field: 1 «Основное», 2 «Семья и жизнь», 3 «Пожелания».
FIELD_STEPS: dict[str, int] = {
    "gender": 1,
    "birth_year": 1,
    "city": 1,
    "nationality": 1,
    "height": 1,
    "weight": 1,
    "marital_status": 2,
    "children": 2,
    "education": 2,
    "employment": 2,
    "occupation": 2,
    "madhab": 2,
    "prayer": 2,
    "wishes": 3,
    "nikah_timeline": 3,
    "relocation": 3,
    "about": 3,
    **dict.fromkeys(CONSENT_FIELDS, 3),
}

CITY_SUGGESTIONS: tuple[str, ...] = (
    "Москва",
    "Санкт-Петербург",
    "Казань",
    "Уфа",
    "Махачкала",
    "Грозный",
    "Нальчик",
    "Черкесск",
    "Владикавказ",
    "Магас",
    "Назрань",
    "Дербент",
    "Хасавюрт",
    "Каспийск",
    "Майкоп",
    "Астрахань",
    "Екатеринбург",
    "Челябинск",
    "Тюмень",
    "Новосибирск",
    "Нижний Новгород",
    "Самара",
    "Оренбург",
    "Набережные Челны",
    "Альметьевск",
    "Ростов-на-Дону",
    "Краснодар",
    "Ставрополь",
    "Пятигорск",
    "Волгоград",
    "Сургут",
)


def form_values(form: FormData, fields: tuple[str, ...] = PROFILE_FIELDS) -> dict[str, str]:
    """Submitted text values (files and missing fields become empty strings)."""
    values: dict[str, str] = {}
    for name in fields:
        value = form.get(name)
        values[name] = value if isinstance(value, str) else ""
    return values


def profile_values(profile: Profile) -> dict[str, str]:
    """Current profile as form values (for the edit page)."""
    values: dict[str, str] = {}
    for name in PROFILE_FIELDS:
        value: Any = getattr(profile, name)
        if value is None:
            values[name] = ""
        elif isinstance(value, StrEnum):
            values[name] = value.value
        else:
            values[name] = str(value)
    return values


def first_error_step(errors: dict[str, str]) -> int:
    return min((FIELD_STEPS.get(name, 1) for name in errors), default=1)


def birth_year_options() -> list[int]:
    """From the youngest allowed (18) to the oldest (80)."""
    year = current_year()
    return list(range(year - AGE_MIN, year - AGE_MAX - 1, -1))


def gendered_labels() -> dict[str, dict[str, dict[str, str]]]:
    """Labels whose wording depends on gender, for switching them on the client: field -> value -> forms."""
    result: dict[str, dict[str, dict[str, str]]] = {}
    for field, enum_cls in (("marital_status", MaritalStatus), ("relocation", Relocation)):
        result[field] = {
            member.value: {
                "male": label(member, Gender.MALE),
                "female": label(member, Gender.FEMALE),
                "neutral": label(member),
            }
            for member in enum_cls
        }
    return result


def form_context(values: dict[str, str], errors: dict[str, str], gender: str | None) -> dict[str, Any]:
    """Shared template context of the profile form (wizard and edit page)."""
    return {
        "values": values,
        "errors": errors,
        "gender": gender if gender in GENDER_VALUES else None,
        "years": birth_year_options(),
        "cities": CITY_SUGGESTIONS,
        "glabels": gendered_labels(),
    }
