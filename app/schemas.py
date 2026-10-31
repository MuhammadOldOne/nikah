"""Input validation shared by the website and the bot.

All validation messages are Russian and user-facing. Convert a ``pydantic.ValidationError``
with ``humanize_errors`` (field -> message, for forms) or ``first_error`` (one line, for the bot).
"""

from __future__ import annotations

import re
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.clock import current_year
from app.enums import (
    Education,
    Employment,
    Gender,
    Madhab,
    MaritalStatus,
    NikahTimeline,
    Prayer,
    Relocation,
    ReportReason,
)
from app.text import clean_line, clean_text, find_contacts, plural

AGE_MIN, AGE_MAX = 18, 80
HEIGHT_MIN, HEIGHT_MAX = 140, 220
WEIGHT_MIN, WEIGHT_MAX = 35, 200
CHILDREN_MIN, CHILDREN_MAX = 0, 15
NAME_MIN, NAME_MAX = 2, 40
CITY_MIN, CITY_MAX = 2, 60
OCCUPATION_MAX = 100
NATIONALITY_MAX = 50
WISHES_MIN, WISHES_MAX = 10, 1000
ABOUT_MAX = 2000
INTEREST_MESSAGE_MAX = 500
CHAT_MESSAGE_MAX = 2000
REPORT_COMMENT_MAX = 1000
MODERATION_NOTE_MAX = 500

PROFILE_REQUIRED_FIELDS: tuple[str, ...] = (
    "gender",
    "name",
    "birth_year",
    "city",
    "marital_status",
    "children",
    "education",
    "employment",
    "wishes",
)
PROFILE_OPTIONAL_FIELDS: tuple[str, ...] = (
    "height",
    "weight",
    "occupation",
    "nationality",
    "madhab",
    "prayer",
    "nikah_timeline",
    "relocation",
    "about",
)
FIELD_LABELS: dict[str, str] = {
    "gender": "Пол",
    "name": "Имя или псевдоним",
    "birth_year": "Год рождения",
    "city": "Город",
    "marital_status": "Семейное положение",
    "children": "Количество детей",
    "education": "Образование",
    "employment": "Занятость",
    "wishes": "Пожелания к супругу",
    "height": "Рост, см",
    "weight": "Вес, кг",
    "occupation": "Род занятий",
    "nationality": "Национальность",
    "madhab": "Мазхаб",
    "prayer": "Намаз",
    "nikah_timeline": "Когда готов(а) к никаху",
    "relocation": "Готовность к переезду",
    "about": "О себе",
}

REQUIRED_MESSAGE = "Обязательное поле."
CONTACTS_MESSAGE = (
    "Уберите ссылки, телефоны, e-mail и @юзернеймы: обмен контактами возможен только в чате после взаимного согласия."
)
_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '’\-][^\W\d_]+)*$")
_CITY_RE = re.compile(r"^[^\W\d_]+(?:(?:[ '’\-]|\. ?)[^\W\d_]+)*\.?$")


def _fail(message: str) -> NoReturn:
    raise PydanticCustomError("invalid", message)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_no_contacts(value: str) -> str:
    if find_contacts(value):
        _fail(CONTACTS_MESSAGE)
    return value


def _chars(n: int) -> str:
    return f"{n} {plural(n, 'символ', 'символа', 'символов')}"


def _check_length(value: str, *, min_len: int = 1, max_len: int) -> str:
    if len(value) < min_len:
        _fail(f"Слишком коротко: минимум {_chars(min_len)}.")
    if len(value) > max_len:
        _fail(f"Слишком длинно: максимум {_chars(max_len)}, сейчас {len(value)}.")
    return value


def _check_range(value: int, low: int, high: int, message: str) -> int:
    if not low <= value <= high:
        _fail(message)
    return value


def birth_year_bounds() -> tuple[int, int]:
    """(oldest, youngest) allowed birth years for today's date."""
    year = current_year()
    return year - AGE_MAX, year - AGE_MIN


class _ProfileValidators(BaseModel):
    """Field rules shared by ProfileCreate and ProfileUpdate."""

    model_config = ConfigDict(extra="ignore")

    @field_validator(*PROFILE_REQUIRED_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _required(cls, value: Any) -> Any:
        if _is_blank(value):
            raise PydanticCustomError("required", REQUIRED_MESSAGE)
        return value

    @field_validator(*PROFILE_OPTIONAL_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if _is_blank(value) else value

    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _check_length(clean_line(value), min_len=NAME_MIN, max_len=NAME_MAX)
        if not _NAME_RE.fullmatch(value):
            _fail("Имя может содержать только буквы, пробелы и дефис.")
        return value

    @field_validator("city", check_fields=False)
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _check_length(clean_line(value), min_len=CITY_MIN, max_len=CITY_MAX)
        _check_no_contacts(value)
        if not _CITY_RE.fullmatch(value):
            _fail("Название города может содержать только буквы, пробелы, точку и дефис.")
        return value[0].upper() + value[1:]

    @field_validator("birth_year", check_fields=False)
    @classmethod
    def _birth_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        oldest, youngest = birth_year_bounds()
        if value > youngest:
            _fail(f"Анкету можно создать только с {AGE_MIN} лет.")
        if value < oldest:
            _fail("Проверьте год рождения.")
        return value

    @field_validator("children", check_fields=False)
    @classmethod
    def _children(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _check_range(
            value, CHILDREN_MIN, CHILDREN_MAX, f"Количество детей — от {CHILDREN_MIN} до {CHILDREN_MAX}."
        )

    @field_validator("height", check_fields=False)
    @classmethod
    def _height(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _check_range(value, HEIGHT_MIN, HEIGHT_MAX, f"Рост — от {HEIGHT_MIN} до {HEIGHT_MAX} см.")

    @field_validator("weight", check_fields=False)
    @classmethod
    def _weight(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _check_range(value, WEIGHT_MIN, WEIGHT_MAX, f"Вес — от {WEIGHT_MIN} до {WEIGHT_MAX} кг.")

    @field_validator("occupation", "nationality", check_fields=False)
    @classmethod
    def _short_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        max_len = OCCUPATION_MAX if info.field_name == "occupation" else NATIONALITY_MAX
        value = clean_line(value)
        return _check_no_contacts(_check_length(value, max_len=max_len)) if value else None

    @field_validator("wishes", check_fields=False)
    @classmethod
    def _wishes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_no_contacts(_check_length(clean_text(value), min_len=WISHES_MIN, max_len=WISHES_MAX))

    @field_validator("about", check_fields=False)
    @classmethod
    def _about(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_text(value)
        return _check_no_contacts(_check_length(value, max_len=ABOUT_MAX)) if value else None


class ProfileCreate(_ProfileValidators):
    gender: Gender
    name: str
    birth_year: int
    city: str
    marital_status: MaritalStatus
    children: int
    education: Education
    employment: Employment
    wishes: str
    height: int | None = None
    weight: int | None = None
    occupation: str | None = None
    nationality: str | None = None
    madhab: Madhab | None = None
    prayer: Prayer | None = None
    nikah_timeline: NikahTimeline | None = None
    relocation: Relocation | None = None
    about: str | None = None


class ProfileUpdate(_ProfileValidators):
    """Partial update: only fields that were passed are changed (``changes()``).

    Gender cannot be changed after the profile is created. Passing an empty value for a
    required field is an error; for an optional field it clears the value.
    """

    name: str | None = None
    birth_year: int | None = None
    city: str | None = None
    marital_status: MaritalStatus | None = None
    children: int | None = None
    education: Education | None = None
    employment: Employment | None = None
    wishes: str | None = None
    height: int | None = None
    weight: int | None = None
    occupation: str | None = None
    nationality: str | None = None
    madhab: Madhab | None = None
    prayer: Prayer | None = None
    nikah_timeline: NikahTimeline | None = None
    relocation: Relocation | None = None
    about: str | None = None

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class CatalogFilters(BaseModel):
    """Catalog filters; blank values mean "any". Ages are swapped if given in reverse order."""

    model_config = ConfigDict(extra="ignore")

    age_min: int | None = None
    age_max: int | None = None
    city: str | None = None
    marital_status: MaritalStatus | None = None
    has_children: bool | None = None
    education: Education | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if _is_blank(value) else value

    @field_validator("age_min", "age_max")
    @classmethod
    def _age(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _check_range(value, AGE_MIN, AGE_MAX, f"Возраст — от {AGE_MIN} до {AGE_MAX} лет.")

    @field_validator("city")
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_length(clean_line(value), max_len=CITY_MAX) or None

    @model_validator(mode="after")
    def _order_ages(self) -> CatalogFilters:
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            self.age_min, self.age_max = self.age_max, self.age_min
        return self


class InterestIn(BaseModel):
    """Optional short message attached to an interest; contacts are not allowed before consent."""

    message: str | None = None

    @field_validator("message", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if _is_blank(value) else value

    @field_validator("message")
    @classmethod
    def _message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_text(value)
        return _check_no_contacts(_check_length(value, max_len=INTEREST_MESSAGE_MAX)) if value else None


class ChatMessageIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = clean_text(value)
        if not value:
            _fail("Сообщение не может быть пустым.")
        return _check_length(value, max_len=CHAT_MESSAGE_MAX)


class ReportIn(BaseModel):
    reason: ReportReason
    comment: str | None = None

    @field_validator("comment", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if _is_blank(value) else value

    @field_validator("comment")
    @classmethod
    def _comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_text(value)
        return _check_length(value, max_len=REPORT_COMMENT_MAX) if value else None


class ModerationNoteIn(BaseModel):
    """Reason shown to the user when a moderator rejects their profile."""

    note: str

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        value = clean_text(value)
        if not value:
            _fail("Укажите причину.")
        return _check_length(value, max_len=MODERATION_NOTE_MAX)


_BUILTIN_MESSAGES: dict[str, str] = {
    "missing": REQUIRED_MESSAGE,
    "enum": "Выберите значение из списка.",
    "literal_error": "Выберите значение из списка.",
    "int_parsing": "Введите целое число.",
    "int_type": "Введите целое число.",
    "int_from_float": "Введите целое число.",
    "bool_parsing": "Неверное значение.",
    "bool_type": "Неверное значение.",
    "string_type": "Введите текст.",
}
_CUSTOM_TYPES = frozenset({"invalid", "required"})


def humanize_errors(exc: ValidationError) -> dict[str, str]:
    """Map a ValidationError to {field: Russian message}, first error per field.

    Errors not tied to a field are returned under the key ``"__all__"``.
    """
    result: dict[str, str] = {}
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else "__all__"
        if field in result:
            continue
        if error["type"] in _CUSTOM_TYPES:
            result[field] = error["msg"]
        else:
            result[field] = _BUILTIN_MESSAGES.get(error["type"], "Неверное значение.")
    return result


def first_error(exc: ValidationError) -> str:
    """One-line message for the bot: "<Field label>: <message>"."""
    field, message = next(iter(humanize_errors(exc).items()))
    label = FIELD_LABELS.get(field)
    return f"{label}: {message}" if label else message
