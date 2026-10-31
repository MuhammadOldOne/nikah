"""Scam markers. Matching texts are flagged for moderators, never blocked (spec section 4)."""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        # Money transfers and bank cards
        r"\bперев[оеи]\w*\s+(?:\w+\s+){0,2}?(?:деньг|денег|средств|сумм)\w*",
        r"\bпереведи(?:те)?\b",
        r"\bскин(?:ь|ьте|уть)\s+(?:\w+\s+)?(?:деньг\w*|денег|на\s+карт\w*)",
        r"\bномер\w*\s+(?:\w+\s+)?карт\w*",
        r"\bна\s+карт[уыеа]\b",
        r"\bреквизит\w*",
        r"\bпомог\w*\s+(?:\w+\s+)?деньг\w*",
        r"\bодолж\w*",
        r"\bпредоплат\w*",
        r"\bвестерн\s+юнион\w*|\bwestern\s+union\b|\bmoneygram\b|\bзолот\w+\s+корон\w*",
        r"\bperev(?:od|edi|esti)\w*",
        r"\bpredoplat\w*",
        # Card-number-like digit groups (16–19 digits)
        r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?:[ -]?\d{1,3})?(?!\d)",
        # Crypto, investments, gambling, loans
        r"\bкрипт\w*",
        r"\b(?:crypto|kript)\w*",
        r"\busdt\b|\btether\b",
        r"\bбитко[ий]н\w*|\bbitcoin\w*|\bbitkoin\w*|\bbtc\b",
        r"\bинвест\w*|\binvest\w*",
        r"\bфорекс\b|\bforex\b|\bтрейдинг\w*|\btrading\b",
        r"\bставк\w*|\bstavk\w*|\bбукмекер\w*|\bказино\b|\bcasino\b",
        r"\bза[йе]м(?:ы|а|ов|ом|ами|у)?\b|\bмикроза[йе]м\w*|\bza[ij]m\w*",
        r"\bпассивн\w+\s+доход\w*",
        r"\bзаработ\w*\s+(?:в\s+)?(?:интернет|онлайн|сети)\w*",
        # Adult scams
        r"\bвебкам\w*|\bweb\s?cam\w*",
    )
)


def find_stop_words(text: str) -> list[str]:
    """Scam markers found in ``text`` (lowercased matches, deduplicated, in pattern order)."""
    normalized = text.lower().replace("ё", "е")
    found: dict[str, None] = {}
    for pattern in _PATTERNS:
        for match in pattern.finditer(normalized):
            found[match.group(0).strip()] = None
    return list(found)


def flag_reason(*texts: str | None) -> str | None:
    """Comma-separated stop words found in any of ``texts`` (for ``flag_reason`` columns), or None."""
    found: dict[str, None] = {}
    for text in texts:
        if text:
            found.update(dict.fromkeys(find_stop_words(text)))
    return ", ".join(found)[:500] if found else None
