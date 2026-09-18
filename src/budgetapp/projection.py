"""Projected net worth: today's balances rolled forward month by month at the current plan.

Assumptions (the Trends page spells them out):
- The budget plan's savings lines are saved every month. Lines about investing go to the
  investments (growing at a rate the user picks, 0% by default); the rest go to cash, into
  the emergency-fund savings account, earning its APY.
- Dividends come from the holdings' yields: reinvested ones (a DRIP) compound with the
  investments on top of the growth rate, and the rest land in cash. The growth rate the user
  picks is price growth only, so dividends aren't counted twice.
- Income the plan hasn't assigned to a line ("Left to assign") piles up as cash; when the plan
  assigns more than it earns, that shortfall comes out of cash. So changing income here or in
  Settings moves the projection straight away.
- Loans follow their payment schedules. Once one is paid off, its payment can count as extra
  savings, split between cash and investments in the same proportions as the plan's own
  savings lines (all cash if the plan has no investing lines).
- In two-paycheck mode the yearly 3rd-paycheck months can count, averaged per month.
- Cards, other assets and other debts stay where they are.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from budgetapp import brokerage, loans, networth, planning
from budgetapp.dates import add_months, month_start
from budgetapp.money import round_cents

_INVESTING = re.compile(r"invest|stock|crypto|brokerage|retire|401k|roth|\bira\b", re.I)
_CASH = re.compile(r"emergency|cash|buffer|hysa|checking", re.I)


@dataclass(frozen=True)
class Point:
    when: date  # today for the first point, then the 1st of each following month
    cash_cents: int
    invested_cents: int
    other_assets_cents: int
    debt_cents: int

    @property
    def assets_cents(self) -> int:
        return self.cash_cents + self.invested_cents + self.other_assets_cents

    @property
    def net_cents(self) -> int:
        return self.assets_cents - self.debt_cents


@dataclass(frozen=True)
class Projection:
    points: list[Point]
    to_cash_cents: int  # planned saving into cash, per month
    dividend_yield: Decimal  # a year of reinvested dividends, as a fraction of investments
    payout_yield: Decimal  # a year of dividends taken as cash, same basis
    to_invest_cents: int  # planned investing, per month
    unassigned_cents: int  # Left to assign, kept as cash (negative: the plan overspends income)
    windfall_cents: int  # 3rd paychecks averaged per month (0 when left out)
    savings_account: str | None  # where cash savings land
    savings_apy: Decimal | None
    growth: Decimal  # yearly investment growth assumed
    debt_free: date | None  # last loan payment (None: no loans, or one never pays off)
    positive: date | None  # first month net worth goes above $0, if it starts at or below
    stuck: list[str]  # loans whose payment doesn't cover the interest (held as they are)

    @property
    def start(self) -> Point:
        return self.points[0]

    @property
    def end(self) -> Point:
        return self.points[-1]


def split_savings(groups: list[planning.Group]) -> tuple[int, int]:
    """The plan's monthly savings as (to cash, to investments), judged by line and category name."""
    cash = invest = 0
    for group in groups:
        if group.category.kind != "savings":
            continue
        for item in group.items:
            investing = _INVESTING.search(item.name) or (
                not _CASH.search(item.name) and _INVESTING.search(group.category.name)
            )
            if investing:
                invest += item.monthly_cents
            else:
                cash += item.monthly_cents
    return cash, invest


def _monthly_rate(yearly: Decimal | None) -> Decimal:
    return (1 + yearly) ** (Decimal(1) / 12) - 1 if yearly else Decimal(0)


def project(
    conn: sqlite3.Connection,
    *,
    today: date,
    months: int = 24,
    growth: Decimal = Decimal(0),
    windfalls: bool = True,
    redirect_payoffs: bool = True,
) -> Projection:
    if months < 1:
        raise ValueError("Project at least one month.")
    accounts = [a for a in networth.list_accounts(conn, include_archived=True)
                if a.include_in_net_worth]
    included = {a.id for a in accounts}
    all_loans = [loan for loan in loans.list_loans(conn, include_archived=True)
                 if loan.account_id in included]
    loan_ids = {loan.account_id for loan in all_loans}
    owing = [loan for loan in all_loans if loan.balance_cents > 0]

    cash_accounts = [a for a in accounts if a.type == "cash" and not a.is_liability]
    cash = {a.id: a.balance_cents or 0 for a in cash_accounts}
    rates = {a.id: _monthly_rate(a.apy) for a in cash_accounts}
    target = next((a for a in cash_accounts if a.emergency_fund), None) or max(
        cash_accounts, key=lambda a: a.apy or 0, default=None
    )
    invested = sum(a.balance_cents or 0 for a in accounts if a.type in networth.INVESTMENT_TYPES)
    other_assets = sum(
        a.balance_cents or 0 for a in accounts
        if not a.is_liability and a.type != "cash" and a.type not in networth.INVESTMENT_TYPES
    )
    other_debt = sum(
        a.balance_cents or 0 for a in accounts if a.is_liability and a.id not in loan_ids
    )

    groups = planning.grouped(conn)
    summary = planning.summarize(groups)
    payouts = brokerage.dividends(brokerage.list_holdings(conn))
    to_cash, to_invest = split_savings(groups)
    unassigned = summary.unallocated_cents
    windfall = round(summary.windfall_per_year_cents / 12) if windfalls else 0

    schedules: dict[int, list[loans.Payment]] = {}
    stuck: list[str] = []
    for loan in owing:
        try:
            schedules[loan.account_id] = loan.schedule(today=today)
        except ValueError:
            stuck.append(loan.name)

    def owed(when: date) -> int:
        total = other_debt
        for loan in owing:
            paid = [p for p in schedules.get(loan.account_id, []) if p.due and p.due <= when]
            total += paid[-1].balance_cents if paid else loan.balance_cents
        return total

    def freed(when: date) -> tuple[int, int]:
        """Payments of loans paid off before `when`, split (to cash, to investments).

        The split follows the plan's own savings lines: someone putting two thirds of their
        saving into the market would do the same with the money a payoff frees up.
        """
        if not redirect_payoffs:
            return 0, 0
        amount = sum(
            loan.payment_cents for loan in owing
            if schedules.get(loan.account_id) and schedules[loan.account_id][-1].due < when
        )
        planned = to_cash + to_invest
        if amount <= 0 or planned <= 0 or to_invest <= 0:
            return amount, 0
        invest = round_cents(Decimal(amount) * Decimal(to_invest) / Decimal(planned))
        return amount - invest, invest

    growth_rate = _monthly_rate(growth + payouts.reinvest_yield)  # price growth plus a DRIP
    payout_rate = _monthly_rate(payouts.payout_yield)
    pooled = 0  # savings with nowhere to land (no cash account yet)
    points = [Point(today, sum(cash.values()), invested, other_assets, owed(today))]
    for i in range(1, months + 1):
        when = add_months(month_start(today), i)
        for account_id, rate in rates.items():
            if rate and cash[account_id] > 0:
                cash[account_id] += round_cents(Decimal(cash[account_id]) * rate)
        if growth_rate and invested > 0:
            invested += round_cents(Decimal(invested) * growth_rate)
        freed_cash, freed_invest = freed(when)
        saving = to_cash + unassigned + windfall + freed_cash
        if payout_rate and invested > 0:
            saving += round_cents(Decimal(invested) * payout_rate)  # dividends taken as cash
        if target is not None:
            cash[target.id] += saving
        else:
            pooled += saving
        invested += to_invest + freed_invest
        points.append(
            Point(when, sum(cash.values()) + pooled, invested, other_assets, owed(when))
        )

    debt_free = None
    if owing and not stuck:
        debt_free = max(schedule[-1].due for schedule in schedules.values())
    positive = None
    if points[0].net_cents <= 0:
        positive = next((p.when for p in points[1:] if p.net_cents > 0), None)
    return Projection(
        points=points,
        to_cash_cents=to_cash,
        dividend_yield=payouts.reinvest_yield,
        payout_yield=payouts.payout_yield,
        to_invest_cents=to_invest,
        unassigned_cents=unassigned,
        windfall_cents=windfall,
        savings_account=target.name if target else None,
        savings_apy=target.apy if target else None,
        growth=growth,
        debt_free=debt_free,
        positive=positive,
        stuck=stuck,
    )
