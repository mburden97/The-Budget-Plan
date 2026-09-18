"""Money helpers.

All currency is stored and computed as integer cents. Floats are never used for
money. Rates and share counts are kept as Decimal (stored as TEXT in SQLite).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_ABS_CENTS = 10**14  # $1 trillion; guards against typos and overflow


def round_cents(value: Decimal) -> int:
    """Round a Decimal amount of cents to a whole cent (half-up)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_money(text: str, *, allow_negative: bool = False, label: str = "Amount") -> int:
    """Parse user input like "1,234.56", "$50" or "(12.30)" into integer cents."""
    cleaned = (text or "").strip().replace(",", "").replace("$", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting-style negative
        cleaned = "-" + cleaned[1:-1]
    if not cleaned:
        raise ValueError(f"{label} is required.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, e.g. 1234.56.") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be a number, e.g. 1234.56.")
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{label}: use at most two decimal places.")
    if value < 0 and not allow_negative:
        raise ValueError(f"{label} can't be negative.")
    cents = int(value * 100)
    if abs(cents) >= MAX_ABS_CENTS:
        raise ValueError(f"{label} is too large.")
    return cents


def parse_percent(text: str, *, label: str = "Rate") -> Decimal:
    """Parse a percentage like "5.25" or "5.25%" into a fraction (Decimal("0.0525"))."""
    cleaned = (text or "").strip().rstrip("%").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, e.g. 5.25.") from None
    if not value.is_finite() or value < 0 or value > 100:
        raise ValueError(f"{label} must be between 0 and 100.")
    return value / 100


def format_percent(fraction: Decimal | float | None, places: int = 2) -> str:
    """Decimal("0.0525") -> "5.25%"."""
    if fraction is None:
        return "–"
    return f"{Decimal(str(fraction)) * 100:.{places}f}%"


def format_quantity(value: Decimal) -> str:
    """Share counts / small prices without scientific notation: Decimal("2E+1") -> "20"."""
    return f"{value.normalize():,f}"


def percent_input(fraction: Decimal | None) -> str:
    """Decimal("0.0525") -> "5.25" for an editable field."""
    if fraction is None:
        return ""
    text = f"{fraction * 100:.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_money(cents: int) -> str:
    """Format cents for display: 123456 -> "$1,234.56", -500 -> "-$5.00"."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


def cents_to_input(cents: int) -> str:
    """Format cents for an editable form field: 123456 -> "1234.56"."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{rem:02d}"
