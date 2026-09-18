"""Calendar helpers."""

from __future__ import annotations

import calendar
from datetime import date


def add_months(d: date, months: int) -> date:
    """Same day-of-month N months later, clamped to month end (Jan 31 + 1 -> Feb 28/29)."""
    years, month_index = divmod(d.month - 1 + months, 12)
    return day_in_month(d.year + years, month_index + 1, d.day)


def day_in_month(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def parse_month(text: str | None, default: date) -> date:
    """'2026-09' -> date(2026, 9, 1); anything unparseable falls back to default's month."""
    try:
        year, month = (int(part) for part in (text or "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return month_start(default)


def months_until(start: date, end: date) -> int:
    """Calendar months from start's month to end's month, at least 1."""
    return max(1, (end.year - start.year) * 12 + end.month - start.month)


def month_label(d: date | None) -> str:
    return d.strftime("%b %Y") if d else "–"
