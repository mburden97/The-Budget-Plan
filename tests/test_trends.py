from datetime import date
from decimal import Decimal

import pytest

from budgetapp import planning, trends
from budgetapp import transactions as tx
from budgetapp.charts import sparkline, stacked_bars


def _add(conn, day, description, cents, **kwargs):
    return tx.add_transaction(
        conn, posted_on=day, description=description, amount_cents=cents, **kwargs
    )


def test_spending_trends(conn):
    cats = {c.name: c.id for c in planning.list_categories(conn)}
    pay = planning.add_line_item(conn, category_id=cats["Income"], name="Pay", amount_cents=1)
    groceries = planning.add_line_item(
        conn, category_id=cats["Flexible Expenses"], name="Groceries", amount_cents=1
    )
    rent = planning.add_line_item(
        conn, category_id=cats["Fixed Expenses"], name="Rent", amount_cents=1
    )
    _add(conn, date(2026, 7, 5), "PAYROLL", 300000, line_item_id=pay)
    _add(conn, date(2026, 7, 6), "KROGER", -10000, line_item_id=groceries)
    _add(conn, date(2026, 8, 6), "KROGER", -12000, line_item_id=groceries)
    _add(conn, date(2026, 8, 7), "KROGER REFUND", 2000, line_item_id=groceries)
    _add(conn, date(2026, 8, 1), "RENT", -150000, line_item_id=rent)
    _add(conn, date(2026, 8, 9), "MYSTERY", -500)
    _add(conn, date(2026, 8, 10), "TRANSFER", -99999, excluded=True)
    _add(conn, date(2026, 8, 11), "VENMO IN", 700)
    _add(conn, date(2026, 5, 1), "TOO EARLY", -1, line_item_id=groceries)

    result = trends.spending_trends(conn, end=date(2026, 8, 20), months=2)
    assert result.months == [date(2026, 7, 1), date(2026, 8, 1)]
    assert {s.label: s.values for s in result.series} == {
        "Fixed Expenses": [0, 150000],
        "Flexible Expenses": [10000, 10000],
        "Uncategorized": [0, 500],
    }
    assert result.series[0].label == "Fixed Expenses"  # biggest first
    assert result.income == [300000, 0]
    assert result.totals == [10000, 160500]

    by_line = trends.spending_trends(conn, end=date(2026, 8, 20), months=2, by="line")
    assert {s.label for s in by_line.series} == {"Rent", "Groceries", "Uncategorized"}
    with pytest.raises(ValueError):
        trends.spending_trends(conn, end=date(2026, 8, 20), by="merchant")


def test_trends_follow_splits(conn):
    flexible = {c.name: c.id for c in planning.list_categories(conn)}["Flexible Expenses"]
    first = planning.add_line_item(conn, category_id=flexible, name="A", amount_cents=1)
    second = planning.add_line_item(conn, category_id=flexible, name="B", amount_cents=1)
    txn = _add(conn, date(2026, 8, 3), "SHOP", -1000, line_item_id=first)
    tx.add_split(conn, txn, line_item_id=second, amount_cents=300)
    result = trends.spending_trends(conn, end=date(2026, 8, 20), months=1, by="line")
    assert {s.label: s.values for s in result.series} == {"A": [700], "B": [300]}


def test_series_average_and_change():
    series = trends.Series("k", "Label", [100, 100, 100, 200, 200, 200])
    assert (series.total, series.average, series.change) == (900, 150, Decimal(1))
    assert trends.Series("k", "Label", [100, 200]).change is None
    assert trends.Series("k", "Label", [0, 0, 0, 5, 5, 5]).change is None


def test_trend_charts():
    months = [date(2026, 7, 1), date(2026, 8, 1)]
    svg = str(stacked_bars(months, [("Rent", [150000, 150000]), ("Food", [20000, -5])]))
    assert svg.startswith("<svg") and 'class="series-0"' in svg and 'class="series-1"' in svg
    assert svg.count("<rect") == 3  # the negative value is skipped
    assert str(stacked_bars([], [])) == ""
    assert "polyline" in str(sparkline([1, 3, 2]))
    assert str(sparkline([0, 0])) == ""
