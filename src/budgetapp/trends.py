"""Spending trends: actual money out per budget category (or line) per month."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from budgetapp.dates import add_months, month_start

GROUPINGS = {"category": "By category", "line": "By budget line"}


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    values: list[int]  # cents spent per month; refunds reduce it

    @property
    def total(self) -> int:
        return sum(self.values)

    @property
    def average(self) -> int:
        return round(self.total / len(self.values)) if self.values else 0

    @property
    def change(self) -> Decimal | None:
        """Last three months against the three before them, as a fraction."""
        if len(self.values) < 6:
            return None
        prior, recent = sum(self.values[-6:-3]), sum(self.values[-3:])
        if prior <= 0:
            return None
        return (Decimal(recent) - Decimal(prior)) / Decimal(prior)


@dataclass(frozen=True)
class Trends:
    months: list[date]
    series: list[Series]  # biggest total first
    income: list[int]

    @property
    def totals(self) -> list[int]:
        return [sum(s.values[i] for s in self.series) for i in range(len(self.months))]


def spending_trends(
    conn: sqlite3.Connection, *, end: date, months: int = 12, by: str = "category"
) -> Trends:
    """Monthly spending for the `months` months ending with `end`'s month.

    Spending is every non-income budget line (expenses, savings and debt payments) plus
    uncategorized money out. Transfers marked not budgeted are left out.
    """
    if by not in GROUPINGS:
        raise ValueError("Unknown grouping.")
    first = add_months(month_start(end), -(months - 1))
    month_list = [add_months(first, i) for i in range(months)]
    index = {f"{d.year}-{d.month:02d}": i for i, d in enumerate(month_list)}
    rows = conn.execute(
        """
        SELECT substr(t.posted_on, 1, 7) AS month, t.amount_cents, t.line_item_id,
               li.name AS line_name, c.id AS category_id, c.name AS category_name, c.kind
        FROM allocations t
        LEFT JOIN line_items li ON li.id = t.line_item_id
        LEFT JOIN categories c ON c.id = li.category_id
        WHERE t.excluded = 0 AND t.posted_on >= ? AND t.posted_on < ?
        """,
        (first.isoformat(), add_months(first, months).isoformat()),
    )
    buckets: dict[str, tuple[str, list[int]]] = {}
    income = [0] * months
    for row in rows:
        i = index[row["month"]]
        if row["kind"] == "income":
            income[i] += row["amount_cents"]
            continue
        if row["line_item_id"] is None:
            if row["amount_cents"] >= 0:
                continue  # uncategorized money in isn't spending
            key, label = "uncategorized", "Uncategorized"
        elif by == "line":
            key, label = f"line:{row['line_item_id']}", row["line_name"]
        else:
            key, label = f"category:{row['category_id']}", row["category_name"]
        buckets.setdefault(key, (label, [0] * months))[1][i] -= row["amount_cents"]
    series = [Series(key, label, values) for key, (label, values) in buckets.items()]
    series.sort(key=lambda s: (-s.total, s.label))
    return Trends(month_list, series, income)
