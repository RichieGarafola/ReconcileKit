"""Conservative normalization of identifiers, currency codes, and dates.

Only surrounding whitespace is removed from identifiers. Case, punctuation,
internal whitespace, and leading zeros are preserved so that two different
business identifiers can never be merged by normalization.
"""

from __future__ import annotations

import re
from datetime import date

_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def normalize_identifier(raw: str) -> str:
    """Trim leading/trailing whitespace only. Returns '' for blank values."""
    return raw.strip()


def normalize_currency(raw: str) -> str:
    """Trim whitespace and upper-case a currency code before validation."""
    return raw.strip().upper()


def parse_iso_date(raw: str) -> date | None:
    """Parse a strict YYYY-MM-DD date (surrounding whitespace trimmed); None if invalid."""
    text = raw.strip()
    if not _ISO_DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def comparison_hint_key(identifier: str) -> str:
    """Case- and punctuation-insensitive key used ONLY to explain near misses.

    It is never used to match records; it lets an explanation say that a
    reference differs from an existing invoice only by case or punctuation.
    """
    return re.sub(r"[^0-9a-z]", "", identifier.casefold())
