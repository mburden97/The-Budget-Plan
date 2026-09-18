from decimal import Decimal

import pytest

from budgetapp import planning


def _cats(conn):
    return {c.name: c.id for c in planning.list_categories(conn)}


def test_seed_categories(conn):
    assert list(_cats(conn)) == [
        "Income",
        "Fixed Expenses",
        "Flexible Expenses",
        "Savings & Investing",
        "Debt Payments",
    ]


@pytest.mark.parametrize(
    ("amount", "frequency", "monthly"),
    [
        (120000, "annual", 10000),
        (100000, "biweekly", 216667),  # 26 paychecks / 12
        (5000, "weekly", 21667),
        (30000, "quarterly", 10000),
        (123456, "monthly", 123456),
    ],
)
def test_monthly_equivalent(amount, frequency, monthly):
    assert planning.monthly_equivalent(amount, frequency) == monthly


@pytest.mark.parametrize(
    ("amount", "frequency", "monthly", "extra_per_year"),
    [
        (200000, "biweekly", 400000, 400000),  # 2 checks a month; 2 extra checks a year
        (5500, "weekly", 22000, 22000),  # 4 weeks a month; 4 extra weeks a year
        (150000, "monthly", 150000, 0),
        (120000, "annual", 10000, 0),  # yearly bills are still spread out
    ],
)
def test_two_paycheck_month(amount, frequency, monthly, extra_per_year):
    assert planning.monthly_equivalent(amount, frequency, "two_paycheck") == monthly
    assert planning.extra_per_year_cents(amount, frequency, "two_paycheck") == extra_per_year
    assert planning.extra_per_year_cents(amount, frequency, "average") == 0


def test_mode_setting_drives_totals(conn):
    from budgetapp import settings

    c = _cats(conn)
    planning.add_line_item(
        conn, category_id=c["Income"], name="Pay", amount_cents=200000, frequency="biweekly"
    )
    planning.add_line_item(
        conn, category_id=c["Fixed Expenses"], name="Groceries", amount_cents=5500,
        frequency="weekly",
    )
    assert planning.budget_mode(conn) == "two_paycheck"  # default
    summary = planning.summarize(planning.grouped(conn))
    assert (summary.income_cents, summary.expense_cents) == (400000, 22000)
    assert summary.windfall_per_year_cents == 400000 - 22000

    settings.put(conn, "budget_mode", "average")
    summary = planning.summarize(planning.grouped(conn))
    assert (summary.income_cents, summary.expense_cents) == (433333, 23833)
    assert summary.windfall_per_year_cents == 0


def test_summary_and_left_to_assign(conn):
    c = _cats(conn)
    planning.add_line_item(conn, category_id=c["Income"], name="Pay", amount_cents=500000)
    planning.add_line_item(conn, category_id=c["Fixed Expenses"], name="Rent", amount_cents=200000)
    planning.add_line_item(
        conn, category_id=c["Flexible Expenses"], name="Gifts", amount_cents=120000,
        frequency="annual",
    )
    planning.add_line_item(
        conn, category_id=c["Savings & Investing"], name="IRA", amount_cents=50000
    )
    planning.add_line_item(conn, category_id=c["Debt Payments"], name="Car", amount_cents=40000)
    summary = planning.summarize(planning.grouped(conn))
    assert summary.income_cents == 500000
    assert summary.expense_cents == 210000
    assert summary.outflow_cents == 300000
    assert summary.unallocated_cents == 200000
    assert summary.savings_rate == Decimal("0.1")


def test_update_and_delete(conn):
    c = _cats(conn)
    item = planning.add_line_item(
        conn, category_id=c["Fixed Expenses"], name="Rent", amount_cents=1
    )
    planning.update_line_item(
        conn, item, category_id=c["Flexible Expenses"], name="Rent ", amount_cents=175000,
        frequency="monthly",
    )
    [flex] = [g for g in planning.grouped(conn) if g.category.name == "Flexible Expenses"]
    assert [(i.name, i.amount_cents) for i in flex.items] == [("Rent", 175000)]
    planning.delete_line_item(conn, item)
    with pytest.raises(LookupError):
        planning.delete_line_item(conn, item)


@pytest.mark.parametrize(
    "overrides",
    [{"name": "  "}, {"amount_cents": -1}, {"frequency": "hourly"}, {"category_id": 999}],
)
def test_add_line_item_validation(conn, overrides):
    fields = {"category_id": _cats(conn)["Income"], "name": "Pay", "amount_cents": 1}
    with pytest.raises(ValueError):
        planning.add_line_item(conn, **(fields | overrides))


def test_categories(conn):
    new = planning.add_category(conn, name="Irregular", kind="expense")
    with pytest.raises(ValueError):
        planning.add_category(conn, name="irregular", kind="expense")
    planning.add_line_item(conn, category_id=new, name="Car insurance", amount_cents=60000)
    with pytest.raises(ValueError):
        planning.delete_category(conn, new)
    conn.execute("DELETE FROM line_items")
    planning.delete_category(conn, new)
    assert "Irregular" not in _cats(conn)
