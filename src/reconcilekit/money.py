"""Exact money handling: strict decimal parsing into integer cents.

Binary floating point is never used. Amounts are parsed with ``Decimal`` only
after a strict textual check, then stored and summed as integer cents.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Plain positive decimal: digits, optional point followed by 1-2 digits.
_AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")
_EXCESS_DECIMALS_RE = re.compile(r"^[0-9]+\.[0-9]{3,}$")
_NON_FINITE = {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "snan"}

# Largest accepted amount: 999,999,999,999.99 (fits comfortably in BIGINT cents).
MAX_AMOUNT_CENTS = 99_999_999_999_999


class AmountError(ValueError):
    """Raised with an actionable, user-facing message for an unusable amount."""


def parse_amount_cents(raw: str) -> int:
    """Parse a positive USD amount with at most two decimal places into cents.

    Surrounding whitespace is trimmed. Everything else must already be a plain
    decimal number: no currency symbols, thousands separators, signs, exponents,
    or excess precision. Values are never rounded.
    """
    text = raw.strip()
    if text == "":
        raise AmountError("Amount is blank; blank amounts are invalid (never treated as zero).")
    lowered = text.lower()
    if lowered in _NON_FINITE:
        raise AmountError(f"'{text}' is not a finite number.")
    if text.startswith("-"):
        raise AmountError(f"'{text}' is negative; refunds and credits are outside v1 scope.")
    if "e" in lowered and re.fullmatch(r"[+]?[0-9.]+e[+-]?[0-9]+", lowered):
        raise AmountError(f"'{text}' uses exponent notation; write the plain decimal amount.")
    if _EXCESS_DECIMALS_RE.match(text):
        raise AmountError(
            f"'{text}' has more than two decimal places; amounts are not rounded automatically."
        )
    if not _AMOUNT_RE.match(text):
        raise AmountError(
            f"'{text}' is not a plain decimal amount such as 1234.50 "
            "(no currency symbols, thousands separators, or signs)."
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guarantees validity
        raise AmountError(f"'{text}' is not a valid decimal amount.") from exc
    cents = int(value * 100)
    if Decimal(cents) != value * 100:  # pragma: no cover - guaranteed by regex
        raise AmountError(f"'{text}' cannot be represented exactly in cents.")
    if cents <= 0:
        raise AmountError(f"'{text}' is zero; only positive amounts are in scope.")
    if cents > MAX_AMOUNT_CENTS:
        raise AmountError(f"'{text}' exceeds the maximum supported amount.")
    return cents


def format_cents(cents: int) -> str:
    """Format integer cents as an exact two-decimal string, e.g. 123456 -> '1234.56'."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def format_usd(cents: int) -> str:
    """Display format with thousands separators, e.g. -133500 -> '-$1,335.00'."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"
