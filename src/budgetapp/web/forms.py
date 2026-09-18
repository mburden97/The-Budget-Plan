"""Form field parsing. Every helper raises ValueError with a message fit to show the user."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from budgetapp.money import parse_money, parse_percent


def _raw(form: Mapping, key: str) -> str:
    return (form.get(key) or "").strip()


def text(form: Mapping, key: str, label: str, *, required: bool = False, max_len: int = 100) -> str:
    value = _raw(form, key)
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > max_len:
        raise ValueError(f"{label} must be {max_len} characters or fewer.")
    return value


def money(
    form: Mapping, key: str, label: str, *, required: bool = True, allow_negative: bool = False
) -> int | None:
    raw = _raw(form, key)
    if not raw:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    return parse_money(raw, allow_negative=allow_negative, label=label)


def percent(form: Mapping, key: str, label: str) -> Decimal:
    return parse_percent(_raw(form, key), label=label)


def integer(
    form: Mapping,
    key: str,
    label: str,
    *,
    required: bool = False,
    lo: int | None = None,
    hi: int | None = None,
) -> int | None:
    raw = _raw(form, key)
    if not raw:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label} must be a whole number.") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return value


def day(
    form: Mapping, key: str, label: str, *, required: bool = True, default: date | None = None
) -> date | None:
    raw = _raw(form, key)
    if not raw:
        if default is not None:
            return default
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} must be a date (YYYY-MM-DD).") from None


def checkbox(form: Mapping, key: str) -> bool:
    return form.get(key) in ("on", "1", "true")
