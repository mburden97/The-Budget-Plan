"""Upcoming money in and out of the everyday (checking) account.

Bills and paychecks that repeat in the account's recent history are projected over the next
few weeks against its balance, to spot a shortfall before a payment bounces. Loan payments
come from the Loans page instead (the planned payment on each due date), because they're
often paid unevenly. One-off charges and irregular payments can't be predicted, so the
result is a guide, not a promise.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from budgetapp import loans, networth, planning, transactions
from budgetapp.dates import add_months, day_in_month

LOOKBACK_DAYS = 120
MIN_HITS = 3
# label, days apart, accepted gaps
CADENCES = (("weekly", 7, 5, 9), ("every 2 weeks", 14, 12, 16), ("monthly", 30, 20, 38))


@dataclass(frozen=True)
class Recurring:
    label: str
    cadence: str
    gap_days: int
    amount_cents: int  # + money in, - money out
    last: date

    def between(self, start: date, end: date) -> list[date]:
        """Expected dates after `start`, up to and including `end`."""
        dates, k = [], 1
        while True:
            if self.cadence == "monthly":
                month = add_months(self.last.replace(day=1), k)
                when = day_in_month(month.year, month.month, self.last.day)
            else:
                when = self.last + timedelta(days=self.gap_days * k)
            if when > end:
                return dates
            if when > start:
                dates.append(when)
            k += 1


@dataclass(frozen=True)
class Expected:
    when: date
    label: str
    amount_cents: int
    balance_cents: int  # the account's balance after this one


@dataclass(frozen=True)
class Forecast:
    account: str
    start_cents: int  # last recorded balance plus transactions imported since
    balance_as_of: date | None
    counted: int  # transactions after the recorded balance, included in start_cents
    days: int
    events: list[Expected]

    @property
    def low_cents(self) -> int:
        return min([self.start_cents] + [e.balance_cents for e in self.events])

    @property
    def short(self) -> Expected | None:
        """The first expected item that takes the balance below $0."""
        return next((e for e in self.events if e.balance_cents < 0), None)


def everyday_account(conn: sqlite3.Connection, today: date) -> networth.Account | None:
    """The cash account with the most transactions lately, other than emergency savings."""
    row = conn.execute(
        "SELECT a.id FROM accounts a JOIN transactions t ON t.account_id = a.id "
        "WHERE a.type = 'cash' AND a.archived = 0 AND a.emergency_fund = 0 AND t.posted_on >= ? "
        "GROUP BY a.id ORDER BY COUNT(*) DESC, a.id LIMIT 1",
        ((today - timedelta(days=90)).isoformat(),),
    ).fetchone()
    return networth.get_account(conn, row[0]) if row else None


def recurring(conn: sqlite3.Connection, account_id: int, today: date) -> list[Recurring]:
    """Money in or out that repeats weekly, every two weeks or monthly and is still going."""
    rules = transactions.list_rules(conn)
    debt_lines = {
        item.id
        for group in planning.grouped(conn) if group.category.kind == "debt"
        for item in group.items
    }
    groups: dict[tuple[str, bool], list[tuple[date, int]]] = defaultdict(list)
    labels: dict[tuple[str, bool], str] = {}
    for row in conn.execute(
        "SELECT posted_on, description, amount_cents FROM transactions "
        "WHERE account_id = ? AND posted_on > ? AND posted_on <= ? ORDER BY posted_on, id",
        (account_id, (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat()),
    ):
        rule = transactions.match(rules, row["description"])
        if rule and rule.line_item_id in debt_lines:
            continue  # loan payments come from the Loans page
        key = (
            rule.pattern if rule else transactions.suggest_pattern(row["description"]),
            row["amount_cents"] > 0,
        )
        groups[key].append((date.fromisoformat(row["posted_on"]), row["amount_cents"]))
        labels[key] = rule.line_item_name if rule and rule.line_item_name else _title(key[0])

    found = []
    for key, hits in groups.items():
        days = sorted({day for day, _amount in hits})
        if len(days) < MIN_HITS:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
        typical = statistics.median(gaps)
        cadence = next((c for c in CADENCES if c[2] <= typical <= c[3]), None)
        if cadence is None:
            continue
        steady = sum(cadence[2] <= gap <= cadence[3] for gap in gaps) / len(gaps)
        if steady < 0.6 or (today - days[-1]).days > cadence[1] * 1.5 + 5:
            continue  # irregular, or it has stopped
        usual = round(statistics.median(amount for _day, amount in hits[-3:]))
        found.append(Recurring(
            label=labels[key], cadence=cadence[0], gap_days=cadence[1],
            amount_cents=usual, last=days[-1],
        ))
    return found


def forecast(conn: sqlite3.Connection, today: date, days: int = 14) -> Forecast | None:
    account = everyday_account(conn, today)
    if account is None:
        return None
    as_of = date.fromisoformat(account.balance_as_of) if account.balance_as_of else None
    counted, since = 0, 0
    if as_of is not None:
        counted, since = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM transactions "
            "WHERE account_id = ? AND posted_on > ?",
            (account.id, as_of.isoformat()),
        ).fetchone()
    newest = conn.execute(
        "SELECT MAX(posted_on) FROM transactions WHERE account_id = ?", (account.id,)
    ).fetchone()[0]
    # Anything after the last balance and the newest import isn't in the balance yet.
    known = [d for d in (as_of, date.fromisoformat(newest) if newest else None) if d]
    after = max(known, default=today - timedelta(days=1))
    end = today + timedelta(days=days)

    items = [
        (when, series.label, series.amount_cents)
        for series in recurring(conn, account.id, today)
        for when in series.between(after, end)
    ]
    for loan in loans.list_loans(conn):
        if loan.balance_cents > 0 and loan.payment_cents > 0:
            payment = -min(loan.payment_cents, loan.balance_cents)
            items += [(when, loan.name, payment) for when in loans.due_dates(loan, after, end)]
    items.sort(key=lambda item: (item[0], item[2] > 0))  # same day: money out first
    balance = (account.balance_cents or 0) + since
    start = balance
    events = []
    for when, label, amount in items:
        balance += amount
        events.append(Expected(when, label, amount, balance))
    return Forecast(account.name, start, as_of, counted, days, events)


def _title(text: str) -> str:
    return " ".join(word.capitalize() for word in text.split()) if text.isupper() else text
