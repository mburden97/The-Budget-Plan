from datetime import date
from decimal import Decimal

import pytest

from budgetapp import goals, planning
from budgetapp import networth as nw

TODAY = date(2026, 9, 11)


@pytest.fixture
def cats(conn):
    return {c.name: c.id for c in planning.list_categories(conn)}


def test_sinking_fund_on_track(conn, cats):
    insurance = planning.add_line_item(
        conn, category_id=cats["Flexible Expenses"], name="Car insurance", amount_cents=72000,
        frequency="annual",
    )  # $60/mo set aside
    fund_id = goals.add_fund(
        conn, name="Car insurance", target_cents=72000, due_date=date(2027, 3, 1),
        saved_cents=12000, line_item_id=insurance,
    )
    [fund] = goals.list_funds(conn)
    assert (fund.remaining_cents, fund.months_left(TODAY)) == (60000, 6)
    assert fund.needed_monthly_cents(TODAY) == 10000
    assert fund.budgeted_monthly_cents == 6000
    assert fund.on_track(TODAY) is False

    goals.update_fund(
        conn, fund_id, name="Car insurance", target_cents=72000, due_date=date(2027, 9, 1),
        saved_cents=12000, line_item_id=insurance,
    )
    [fund] = goals.list_funds(conn)
    assert fund.needed_monthly_cents(TODAY) == 5000 and fund.on_track(TODAY) is True

    goals.delete_fund(conn, fund_id)
    assert goals.list_funds(conn) == []


def test_fund_without_due_date_or_budget_line(conn):
    goals.add_fund(conn, name="Vacation", target_cents=200000)
    [fund] = goals.list_funds(conn)
    assert fund.needed_monthly_cents(TODAY) is None
    assert fund.on_track(TODAY) is None
    assert fund.progress == 0
    with pytest.raises(ValueError):
        goals.add_fund(conn, name=" ", target_cents=1)
    with pytest.raises(ValueError):
        goals.add_fund(conn, name="X", target_cents=0)
    with pytest.raises(ValueError):
        goals.add_fund(conn, name="X", target_cents=1, line_item_id=999)


def test_fully_funded(conn):
    goals.add_fund(conn, name="Tires", target_cents=80000, saved_cents=90000,
                   due_date=date(2026, 10, 1))
    [fund] = goals.list_funds(conn)
    assert fund.remaining_cents == 0 and fund.progress == 1 and fund.on_track(TODAY)


def test_emergency_fund(conn, cats):
    fixed, debt = cats["Fixed Expenses"], cats["Debt Payments"]
    planning.add_line_item(conn, category_id=fixed, name="Rent", amount_cents=150000)
    planning.add_line_item(conn, category_id=debt, name="Car", amount_cents=50000)
    planning.add_line_item(
        conn, category_id=cats["Flexible Expenses"], name="Fun", amount_cents=30000
    )  # not essential by default
    savings = nw.add_account(conn, name="HYSA", type="cash", emergency_fund=True)
    nw.record_balance(conn, savings, 500000)
    nw.add_account(conn, name="Checking", type="cash")

    ef = goals.emergency_fund(conn)
    assert ef.essential_monthly_cents == 200000
    assert ef.months_covered == Decimal("2.5")
    assert (ef.target_months, ef.target_cents, ef.shortfall_cents) == (6, 1200000, 700000)
    assert ef.accounts == ["HYSA"]

    planning.set_essential(conn, cats["Flexible Expenses"], True)
    assert goals.emergency_fund(conn).essential_monthly_cents == 230000

    assert ef.monthly_interest_cents == 0
    nw.update_account(
        conn, savings, name="HYSA", institution="", notes="", include_in_net_worth=True,
        emergency_fund=True, archived=False, apy=Decimal("0.03"),
    )
    assert goals.emergency_fund(conn).monthly_interest_cents == 1233  # $5,000 at 3% APY


def test_fund_transfer(conn):
    checking = nw.add_account(conn, name="Everyday", type="cash")
    savings = nw.add_account(conn, name="Savings", type="cash", emergency_fund=True)
    fund = goals.add_fund(conn, name="Tires", target_cents=80000, saved_cents=1000)
    goals.fund_transfer(conn, fund, 5000, to_account_id=savings, from_account_id=checking)
    goals.fund_transfer(conn, fund, 5000, to_account_id=savings)  # from an untracked account
    goals.fund_transfer(conn, fund, 2500, to_account_id=checking, from_account_id=savings)
    assert goals.list_funds(conn)[0].saved_cents == 8500  # 10 + 50 + 50 - 25

    with pytest.raises(ValueError, match="more than"):
        goals.fund_transfer(conn, fund, 9000, to_account_id=checking, from_account_id=savings)
    with pytest.raises(ValueError, match="into or out of"):
        goals.fund_transfer(conn, fund, 100, to_account_id=checking)
    with pytest.raises(ValueError, match="more than \\$0"):
        goals.fund_transfer(conn, fund, 0, to_account_id=savings)
    with pytest.raises(LookupError):
        goals.fund_transfer(conn, 999, 100, to_account_id=savings)
    assert goals.list_funds(conn)[0].saved_cents == 8500


def test_emergency_fund_leaves_out_sinking_fund_savings(conn, cats):
    fixed = cats["Fixed Expenses"]
    planning.add_line_item(conn, category_id=fixed, name="Rent", amount_cents=100000)
    savings = nw.add_account(conn, name="Savings", type="cash", emergency_fund=True)
    nw.record_balance(conn, savings, 400000)
    fund = goals.add_fund(conn, name="Tires", target_cents=80000, saved_cents=30000)

    ef = goals.emergency_fund(conn)
    assert (ef.held_cents, ef.set_aside_cents, ef.saved_cents) == (400000, 30000, 370000)
    assert ef.months_covered == Decimal("3.7")

    goals.update_fund(conn, fund, name="Tires", target_cents=80000, saved_cents=500000,
                      due_date=None, line_item_id=None)
    assert goals.emergency_fund(conn).saved_cents == 0  # never negative
