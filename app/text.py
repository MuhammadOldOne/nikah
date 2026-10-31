"""Text normalization and contact detection shared by schemas and services."""

from __future__ import annotations

import re
import unicodedata

# Zero-width and bidi control characters used to smuggle hidden text.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿‪‫‬‭‮"))
_SPACES = re.compile(r"[ \t\f\v  -   　]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def _strip_controls(value: str, *, keep_newlines: bool) -> str:
    value = unicodedata.normalize("NFC", value).translate(_INVISIBLE)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in value if (keep_newlines and ch == "\n") or ch in " \t" or unicodedata.category(ch)[0] != "C"
    )


def clean_line(value: str) -> str:
    """Single-line text: drop control characters, collapse all whitespace, trim."""
    value = _strip_controls(value, keep_newlines=True).replace("\n", " ")
    return _SPACES.sub(" ", value).strip()


def clean_text(value: str) -> str:
    """Multi-line text: drop control characters, trim lines, at most one empty line in a row."""
    value = _strip_controls(value, keep_newlines=True)
    lines = [_SPACES.sub(" ", line).strip() for line in value.split("\n")]
    return _MANY_NEWLINES.sub("\n\n", "\n".join(lines)).strip()


_TLDS = (
    "ru|рф|su|com|net|org|info|biz|me|io|co|app|dev|site|online|store|shop|xyz|top|club|pro|link|"
    "live|life|tk|ua|by|kz|uz|tj|kg|az|am|ge|de|uk|us|tv|cc|ly|gg|to|ws|ink|space|website"
)
_CONTACT_PATTERNS = (
    re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE),
    re.compile(r"\b(?:t\.me|telegram\.me|wa\.me|vk\.me|instagr\.am)/\S*", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE),
    re.compile(rf"\b[\w-]{{2,}}\.(?:{_TLDS})\b(?:/\S*)?", re.IGNORECASE),
    re.compile(r"(?<![\w@])@[a-z0-9_]{3,}", re.IGNORECASE),
    # Phone numbers: 10+ digits with optional separators, or the local xxx-xx-xx form.
    re.compile(r"(?<!\d)\+?\d(?:[\s\-().]{0,3}\d){9,}"),
    re.compile(r"(?<!\d)\d{3}[\s-]\d{2}[\s-]\d{2}(?!\d)"),
)


def find_contacts(value: str) -> list[str]:
    """Links, e-mails, @usernames and phone numbers found in ``value`` (deduplicated)."""
    found: dict[str, None] = {}
    for pattern in _CONTACT_PATTERNS:
        found.update((match.group(0).strip(), None) for match in pattern.finditer(value))
    return list(found)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: plural(5, "анкета", "анкеты", "анкет") -> "анкет"."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def normalize_city_key(value: str) -> str:
    """Case- and ё-insensitive key used to compare cities (mirrors the SQL expression in services)."""
    return clean_line(value).lower().replace("ё", "е")
