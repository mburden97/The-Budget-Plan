from datetime import date
from decimal import Decimal

import pytest

from budgetapp import brokerage, loans, planning, projection
from budgetapp import networth as nw
from budgetapp import settings as prefs

TODAY = date(2026, 1, 10)


@pytest.fixture
def cats(conn):
    return {c.name: c.id for c in planning.list_categories(conn)}


def _account(conn, name, kind, cents, **kwargs):
    account_id = nw.add_account(conn, name=name, type=kind, **kwargs)
    nw.record_balance(conn, account_id, cents, TODAY)
    return account_id


def _loan(conn, balance, rate="0", payment=10000):
    return loans.create_loan(
        conn, name=f"Loan {balance}", balance_cents=balance, annual_rate=Decimal(rate),
        min_payment_cents=payment, as_of=TODAY,
    )


def test_projection_follows_the_plan(conn, cats):
    _account(conn, "Rainy", "cash", 100000, emergency_fund=True)
    _account(conn, "Everyday", "cash", 20000)
    _account(conn, "Taxable", "brokerage", 50000)
    _account(conn, "Card", "credit_card", 20000)
    _loan(conn, 30000)  # $100 a month from Feb 1: paid off Apr 1
    saving = cats["Savings & Investing"]
    planning.add_line_item(conn, category_id=saving, name="Emergency fund", amount_cents=5000)
    planning.add_line_item(conn, category_id=saving, name="Index funds", amount_cents=2000)
    planning.add_line_item(conn, category_id=cats["Income"], name="Pay", amount_cents=7000)

    p = projection.project(conn, today=TODAY, months=4)
    assert (p.to_cash_cents, p.to_invest_cents, p.windfall_cents) == (5000, 2000, 0)
    assert p.unassigned_cents == 0  # the plan assigns every dollar
    assert p.savings_account == "Rainy"
    assert p.start.net_cents == nw.net_worth(conn, TODAY).net_cents == 120000
    rows = [(pt.when, pt.cash_cents, pt.invested_cents, pt.debt_cents) for pt in p.points]
    assert rows == [
        (TODAY, 120000, 50000, 50000),
        (date(2026, 2, 1), 125000, 52000, 40000),
        (date(2026, 3, 1), 130000, 54000, 30000),
        (date(2026, 4, 1), 135000, 56000, 20000),  # loan paid off; the card stays
        # The freed $100 follows the plan's split: $5 of every $7 saved as cash.
        (date(2026, 5, 1), 147143, 60857, 20000),
    ]
    assert (p.debt_free, p.positive, p.stuck) == (date(2026, 4, 1), None, [])

    kept = projection.project(conn, today=TODAY, months=4, redirect_payoffs=False)
    assert kept.end.cash_cents == 140000


def test_turning_positive_and_third_paychecks(conn, cats):
    _account(conn, "Everyday", "cash", 10000)
    _loan(conn, 30000)
    planning.add_line_item(
        conn, category_id=cats["Savings & Investing"], name="Cash buffer", amount_cents=5000
    )
    planning.add_line_item(conn, category_id=cats["Income"], name="Pay", amount_cents=5000)
    p = projection.project(conn, today=TODAY, months=3)
    assert [pt.net_cents for pt in p.points] == [-20000, -5000, 10000, 25000]
    assert p.positive == date(2026, 3, 1)

    prefs.put(conn, "budget_mode", "two_paycheck")
    planning.add_line_item(
        conn, category_id=cats["Income"], name="Pay", amount_cents=100000, frequency="biweekly"
    )
    with_extra = projection.project(conn, today=TODAY, months=1)
    assert with_extra.windfall_cents == 16667  # two extra paychecks a year, per month
    assert with_extra.unassigned_cents == 200000  # the new paycheck isn't assigned to a line yet
    assert with_extra.end.cash_cents == 10000 + 5000 + 200000 + 16667
    assert projection.project(conn, today=TODAY, months=1, windfalls=False).windfall_cents == 0


def test_income_changes_move_the_projection(conn, cats):
    _account(conn, "Everyday", "cash", 0)
    pay = planning.add_line_item(conn, category_id=cats["Income"], name="Pay", amount_cents=300000)
    planning.add_line_item(
        conn, category_id=cats["Fixed Expenses"], name="Rent", amount_cents=250000
    )

    def raise_pay(cents: int) -> projection.Projection:
        planning.update_line_item(
            conn, pay, category_id=cats["Income"], name="Pay", amount_cents=cents,
            frequency="monthly",
        )
        return projection.project(conn, today=TODAY, months=1)

    p = projection.project(conn, today=TODAY, months=1)
    assert (p.unassigned_cents, p.end.cash_cents) == (50000, 50000)  # $500 left over each month
    richer = raise_pay(320000)
    assert (richer.unassigned_cents, richer.end.cash_cents) == (70000, 70000)
    poorer = raise_pay(200000)
    assert (poorer.unassigned_cents, poorer.end.cash_cents) == (-50000, -50000)  # outspends income


def test_interest_growth_and_stuck_loans(conn):
    rainy = _account(conn, "Rainy", "cash", 100000, emergency_fund=True)
    nw.update_account(
        conn, rainy, name="Rainy", institution="", notes="", include_in_net_worth=True,
        emergency_fund=True, archived=False, apy=Decimal("0.03"),
    )
    _account(conn, "Taxable", "brokerage", 100000)
    _loan(conn, 100000, rate="0.24", payment=100)  # never covers its interest

    p = projection.project(conn, today=TODAY, months=12, growth=Decimal("0.12"))
    interest = nw.get_account(conn, rainy).monthly_interest_cents
    assert p.points[1].cash_cents == 100000 + interest
    assert abs(p.end.invested_cents - 112000) <= 12  # 12% a year, compounded monthly
    assert p.stuck == ["Loan 100000"] and p.debt_free is None
    assert p.end.debt_cents == 100000  # held at today's balance
    with pytest.raises(ValueError):
        projection.project(conn, today=TODAY, months=0)


def test_reinvested_dividends_compound_and_payouts_go_to_cash(conn, cats):
    _account(conn, "Everyday", "cash", 0)
    broker = nw.add_account(conn, name="Taxable", type="brokerage")
    brokerage.add_holding(
        conn, account_id=broker, symbol="DRIP", shares=Decimal(1200), quote_source="manual",
        price=Decimal(100), dividend_yield=Decimal("0.05"),  # $120,000 at 5%, reinvested
    )
    brokerage.add_holding(
        conn, account_id=broker, symbol="PAYS", shares=Decimal(800), quote_source="manual",
        price=Decimal(100), dividend_yield=Decimal("0.03"), reinvest=False,  # $80,000 at 3%
    )
    p = projection.project(conn, today=TODAY, months=12)
    assert (p.dividend_yield, p.payout_yield) == (Decimal("0.03"), Decimal("0.012"))

    # Reinvested dividends compound even with no price growth; the rest lands in cash.
    assert p.start.invested_cents == 20000000
    assert abs(p.end.invested_cents - 20600000) <= 20  # 3% of the whole portfolio, a year on
    # 1.2% of it over the year, a little more because the balance it pays on grows.
    assert 240000 < p.end.cash_cents < 244000
    flat = projection.project(conn, today=TODAY, months=12, growth=Decimal("0.04"))
    assert flat.end.invested_cents > p.end.invested_cents  # price growth stacks on top


def test_a_freed_loan_payment_follows_the_plans_split(conn, cats):
    _account(conn, "Everyday", "cash", 0)
    _account(conn, "Taxable", "brokerage", 0)
    _loan(conn, 10000)  # $100 a month, gone after one payment on Feb 1
    saving = cats["Savings & Investing"]
    planning.add_line_item(conn, category_id=saving, name="Emergency fund", amount_cents=2500)
    invest = planning.add_line_item(conn, category_id=saving, name="Index funds", amount_cents=7500)
    planning.add_line_item(conn, category_id=cats["Income"], name="Pay", amount_cents=10000)

    p = projection.project(conn, today=TODAY, months=3)
    february, march = p.points[1], p.points[2]
    assert (february.cash_cents, february.invested_cents) == (2500, 7500)  # nothing freed yet
    # From March the $100 is free: a quarter of it as cash, the rest invested, like the plan.
    assert (march.cash_cents, march.invested_cents) == (2500 + 2500 + 2500, 7500 + 7500 + 7500)

    planning.update_line_item(
        conn, invest, category_id=saving, name="Index funds", amount_cents=0, frequency="monthly"
    )
    cash_only = projection.project(conn, today=TODAY, months=3)
    assert cash_only.end.invested_cents == 0  # no investing lines: it all stays in cash
    assert cash_only.end.cash_cents == p.end.cash_cents + p.end.invested_cents
