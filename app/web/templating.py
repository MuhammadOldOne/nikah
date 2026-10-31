"""Shared Jinja2 environment. Render with ``templates.TemplateResponse(request, "x.html", {...})``.

Globals: ``csrf_token()``, ``label(value, gender=None)``, ``choices(EnumCls, gender=None)``,
``settings``, ``age(birth_year)``, ``plural(n, one, few, many)`` (the word only:
``{{ n }} {{ plural(n, 'анкета', 'анкеты', 'анкет') }}``), ``static_url(path)`` (cache-busted),
``pop_flashes()``, ``gendered(gender, male, female, neutral)``, ``enums`` (the enum classes),
``is_admin(user)``, ``profile_title(profile)`` («Анкета №512»), ``gender_word(gender)`` («Брат»/«Сестра»).
Filters: ``age``, ``plural`` (same as the global: ``n|plural(...)``), ``msk`` (``dt|msk`` ->
"31.10.2026 14:05", custom strftime format allowed), ``msk_time`` ("14:05"),
``msk_date`` ("31 октября 2026"), ``msk_day_time`` ("01.04 13:05", with the year when it is not the
current one). All datetimes are shown in Moscow time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, pass_context
from jinja2.runtime import Context

from app import enums
from app.clock import age_from_birth_year, current_year, msk_day_time, to_moscow
from app.config import get_settings
from app.enums import Gender, choices, label
from app.services.users import is_admin
from app.text import gender_word, plural, profile_title
from app.web.deps import get_csrf_token, pop_flashes

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@pass_context
def _csrf_token(context: Context) -> str:
    return get_csrf_token(context["request"])


@pass_context
def _pop_flashes(context: Context) -> list[dict[str, str]]:
    return pop_flashes(context["request"])


@cache
def _static_version(path: str) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]


def static_url(path: str) -> str:
    """URL of a static file with a content hash, so browsers may cache it for a long time."""
    return f"/static/{path}?v={_static_version(path)}"


def gendered(gender: Gender | str | None, male: str, female: str, neutral: str | None = None) -> str:
    """Pick the wording for the gender of the person described (neutral when unknown)."""
    if gender is None or gender == "":
        return neutral if neutral is not None else male
    return male if Gender(gender) is Gender.MALE else female


def format_msk(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return to_moscow(value).strftime(fmt) if value is not None else ""


def format_msk_time(value: datetime | None) -> str:
    return format_msk(value, "%H:%M")


def format_msk_day_time(value: datetime | None) -> str:
    return msk_day_time(value) if value is not None else ""


def format_msk_date(value: datetime | None) -> str:
    if value is None:
        return ""
    local = to_moscow(value)
    return f"{local.day} {_MONTHS_GENITIVE[local.month - 1]} {local.year}"


env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals.update(
    csrf_token=_csrf_token,
    label=label,
    choices=choices,
    settings=get_settings(),
    age=age_from_birth_year,
    plural=plural,
    static_url=static_url,
    pop_flashes=_pop_flashes,
    gendered=gendered,
    enums=enums,
    current_year=current_year,
    is_admin=is_admin,
    profile_title=profile_title,
    gender_word=gender_word,
)
env.filters.update(
    age=age_from_birth_year,
    plural=plural,
    msk=format_msk,
    msk_time=format_msk_time,
    msk_date=format_msk_date,
    msk_day_time=format_msk_day_time,
)

templates = Jinja2Templates(env=env)
