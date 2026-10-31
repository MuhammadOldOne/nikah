"""Text normalization, contact detection and display helpers shared by schemas and services."""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from app.enums import Gender

# Zero-width and bidi control characters used to smuggle hidden text.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿‪‫‬‭‮"))
_ZWJ = "\u200d"
_SPACES = re.compile(r"[ \t\f\v  -   　]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def _joins_emoji(value: str, index: int) -> bool:
    """Whether the zero-width joiner at ``index`` glues an emoji sequence (👨‍👩‍👧, ❤️‍🔥)."""
    before, after = value[index - 1 : index], value[index + 1 : index + 2]
    emoji_before = before == "\ufe0f" or (before != "" and unicodedata.category(before) in ("So", "Sk"))
    return emoji_before and after != "" and unicodedata.category(after) == "So"


def _strip_controls(value: str, *, keep_newlines: bool) -> str:
    """Drop control and invisible characters. A zero-width joiner survives only inside an emoji
    sequence: between letters or digits it would hide contacts and stop words from the checks."""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for index, ch in enumerate(value):
        if ch == _ZWJ:
            if _joins_emoji(value, index):
                kept.append(ch)
        elif ord(ch) not in _INVISIBLE and (
            (keep_newlines and ch == "\n") or ch in " \t" or unicodedata.category(ch)[0] != "C"
        ):
            kept.append(ch)
    return "".join(kept)


def clean_line(value: str) -> str:
    """Single-line text: drop control characters, collapse all whitespace, trim."""
    value = _strip_controls(value, keep_newlines=True).replace("\n", " ")
    return _SPACES.sub(" ", value).strip()


def clean_text(value: str) -> str:
    """Multi-line text: drop control characters, trim lines, at most one empty line in a row."""
    value = _strip_controls(value, keep_newlines=True)
    lines = [_SPACES.sub(" ", line).strip() for line in value.split("\n")]
    return _MANY_NEWLINES.sub("\n\n", "\n".join(lines)).strip()


# --- Contact detection ----------------------------------------------------------------------
#
# One engine for both uses: profile/interest texts reject contacts (find_contacts), chat messages
# get them replaced by CONTACT_PLACEHOLDER (hide_contacts). Matching runs on a folded copy of the
# text (compatibility forms such as fullwidth or circled characters, any Unicode digit as ASCII,
# lowercase) and the matches are mapped back to the original characters.

CONTACT_PLACEHOLDER = "[скрыто свидетелем]"

_TLDS = (
    "ru|рф|su|com|net|org|info|biz|me|io|co|app|dev|site|online|store|shop|xyz|top|club|pro|link|"
    "live|life|tk|ua|by|kz|uz|tj|kg|az|am|ge|de|uk|us|tv|cc|ly|gg|to|ws|ink|space|website|рус|ру|ком|орг"
)
# "site точка ру", "site dot com", "site[.]ru", "site(.)ru"
_WORD_DOT = r"(?:\s*(?:\(|\[)\s*(?:\.|точка|dot)\s*(?:\)|\])\s*|\s+(?:точка|тчк|dot)\s+)"
_PLAIN_DOT = r"\s?\.\s?"
_SEPARATORS = r"[\s().\-‐‑‒–—―−/_*]"
_CONTACT_PATTERNS = (
    # Links with a scheme or www.
    re.compile(r"(?:https?|ftp|tg)\s*:\s*/\s*/\s*\S+|\bwww\s?\.\s?\S+"),
    # Messenger short links, also spaced or in Cyrillic look-alikes: t.me/…, т.ме/…, telegram.me/…
    re.compile(r"(?<![\w.])(?:t|т|telegram|телеграм)\s*\.\s*(?:me|ме|dog)\b(?:\s*/\s*\S+)?"),
    # E-mail addresses, also "ivan собака mail точка ru" and "ivan (at) mail (dot) ru".
    re.compile(
        r"(?<![\w.+-])[\w.+-]+\s*(?:@|\(\s*at\s*\)|\[\s*at\s*\]|\s(?:at|собака)\s)\s*[\w-]+"
        rf"(?:(?:\.|{_WORD_DOT})[\w-]+)*(?:\.|{_WORD_DOT})(?:[a-z]{{2,}}|рф|ру)\b"
    ),
    # Domains: written plainly (any script), latin ones with spaced dots, and "dot" as a word.
    re.compile(rf"(?<![\w@.-])[\w-]+(?:\.[\w-]+)*\.(?:{_TLDS})\b(?:[/?#]\S*)?"),
    re.compile(
        rf"(?<![\w@.-])[a-z0-9][a-z0-9-]+(?:{_PLAIN_DOT}[a-z0-9-]{{2,}})*{_PLAIN_DOT}(?:{_TLDS})\b(?:[/?#]\S*)?"
    ),
    re.compile(rf"(?<![\w@.-])[\w-]{{2,}}{_WORD_DOT}(?:{_TLDS})\b(?:[/?#]\S*)?"),
    # @usernames (Telegram, Instagram, …), also "@ name".
    re.compile(r"(?<![\w@.])@\s?(?=[a-z0-9_.]*[a-z])[a-z0-9_][a-z0-9_.]{2,}"),
    # Local phone numbers: 123-45-67.
    re.compile(r"(?<!\d)\d{3}[ -]\d{2}[ -]\d{2}(?!\d)"),
)
# Any run of digits joined by separators (spaces, brackets, dots, dashes): a phone number when it
# holds 10 or more digits (+7 912 345-67-89, 8 9 1 2 …), counted after dates are set aside.
_DIGIT_RUN = re.compile(rf"\+?\d(?:{_SEPARATORS}{{0,3}}\d)*")
_DATE = re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[./](?:0?[1-9]|1[0-2])[./](?:\d{4}|\d{2})(?!\d)")
_PHONE_MIN_DIGITS = 10
_ONLY_SEPARATORS = re.compile(r"[\W_]*")


def _folded(value: str) -> tuple[str, list[int]]:
    """``value`` folded for matching, and for every folded character the index of its source."""
    chars: list[str] = []
    sources: list[int] = []
    for index, ch in enumerate(value):
        digit = None if ch.isascii() else unicodedata.digit(ch, None)
        piece = str(digit) if digit is not None else unicodedata.normalize("NFKC", ch).lower()
        chars.extend(piece)
        sources.extend([index] * len(piece))
    return "".join(chars), sources


def _contact_spans(value: str) -> list[tuple[int, int]]:
    """Sorted, merged (start, end) spans of ``value`` that hold contacts."""
    folded, sources = _folded(value)
    found: list[tuple[int, int]] = []
    for pattern in _CONTACT_PATTERNS:
        found.extend(match.span() for match in pattern.finditer(folded))
    without_dates = _DATE.sub(lambda match: "x" * len(match.group(0)), folded)
    for match in _DIGIT_RUN.finditer(without_dates):
        if sum(ch.isdigit() for ch in match.group(0)) >= _PHONE_MIN_DIGITS:
            found.append(match.span())
    spans: list[tuple[int, int]] = []
    for start, end in sorted((sources[start], sources[end - 1] + 1) for start, end in found if end > start):
        # Overlapping matches, or contacts separated only by spaces/punctuation, become one span.
        if spans and (start <= spans[-1][1] or _ONLY_SEPARATORS.fullmatch(value, spans[-1][1], start)):
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def find_contacts(value: str) -> list[str]:
    """Links, e-mails, @usernames and phone numbers found in ``value`` (deduplicated)."""
    return list(dict.fromkeys(value[start:end].strip() for start, end in _contact_spans(value)))


def hide_contacts(value: str) -> tuple[str, bool]:
    """``value`` with every contact replaced by CONTACT_PLACEHOLDER, and whether anything was hidden.

    Ordinary numbers stay (age, years, dates, times, height, prices below ten digits)."""
    spans = _contact_spans(value)
    if not spans:
        return value, False
    parts: list[str] = []
    position = 0
    for start, end in spans:
        parts.extend((value[position:start], CONTACT_PLACEHOLDER))
        position = end
    parts.append(value[position:])
    return "".join(parts), True


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


class _Numbered(Protocol):
    number: int | None


def profile_title(profile: _Numbered) -> str:
    """Public name of a profile: «Анкета №512» (people are known by profile number only);
    «Новая анкета» for a draft that has no number yet."""
    return f"Анкета №{profile.number}" if profile.number is not None else "Новая анкета"


def gender_word(gender: Gender | str) -> str:
    """«Брат» / «Сестра», as the legacy bot and its users say."""
    return "Брат" if Gender(gender) is Gender.MALE else "Сестра"
