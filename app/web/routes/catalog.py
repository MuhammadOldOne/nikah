"""Catalog of profiles (/catalog): filters in GET parameters, HTMX-enhanced, works without JS."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError

from app.schemas import CatalogFilters, humanize_errors
from app.services import guardians, profiles
from app.services.errors import ProfileNotActive
from app.web.deps import ProfileUserDep, RedirectTo, SessionDep, is_partial
from app.web.gates import profile_gate
from app.web.render import render, render_partial

router = APIRouter()

PER_PAGE = 12
FILTER_FIELDS = ("age_min", "age_max", "city", "marital_status", "has_children", "education")
MAX_PAGE = 10_000


def parse_filters(params: dict[str, str]) -> tuple[CatalogFilters, dict[str, str]]:
    """Valid filters plus messages for the invalid ones (which are ignored)."""
    raw = {name: params.get(name, "") for name in FILTER_FIELDS}
    try:
        return CatalogFilters.model_validate(raw), {}
    except ValidationError as exc:
        errors = humanize_errors(exc)
    cleaned = {name: value for name, value in raw.items() if name not in errors}
    return CatalogFilters.model_validate(cleaned), errors


def parse_page(value: str | None) -> int:
    try:
        return min(max(int(value or 1), 1), MAX_PAGE)
    except ValueError:
        return 1


def page_numbers(page: int, pages: int) -> list[int | None]:
    """Compact pagination: 1 … 4 5 [6] 7 8 … 20 (None marks a gap)."""
    shown = sorted({1, pages, *range(max(page - 2, 1), min(page + 2, pages) + 1)})
    result: list[int | None] = []
    for number in shown:
        if result and number - (result[-1] or 0) > 1:
            result.append(None)
        result.append(number)
    return result


@router.get("/catalog")
async def catalog(request: Request, session: SessionDep, user: ProfileUserDep) -> Response:
    params = dict(request.query_params)
    filters, filter_errors = parse_filters(params)
    page = parse_page(params.get("page"))
    try:
        items, total = await profiles.list_catalog(session, user, filters, page=page, per_page=PER_PAGE)
    except ProfileNotActive as exc:  # e.g. the profile is still on moderation: say why
        return await profile_gate(request, session, user, exc)
    pages = max(math.ceil(total / PER_PAGE), 1)
    active = {name: params[name].strip() for name in FILTER_FIELDS if params.get(name, "").strip()}
    if page > pages:  # e.g. an old ?page=N after profiles left: go to the last page, same filters
        last = {**active, "page": str(pages)} if pages > 1 else active
        raise RedirectTo(f"/catalog?{urlencode(last)}" if last else "/catalog")
    context: dict[str, Any] = {
        "items": items,
        "total": total,
        "page": page,
        "pages": pages,
        "page_numbers": page_numbers(min(page, pages), pages),
        "filters": active,
        "filter_errors": filter_errors,
        "query_base": urlencode(active),
        "wali_ids": await guardians.users_with_active_guardian(session, [p.user_id for p in items]),
        "own_profile": user.profile,
    }
    if is_partial(request):
        response = render_partial(request, "catalog/_results.html", context)
    else:
        response = await render(request, "catalog/index.html", context, session=session, user=user)
    response.headers["Vary"] = "HX-Request"
    return response
