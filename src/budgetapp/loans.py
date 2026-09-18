"""Loans: payment math, amortization, multi-loan payoff strategies, and storage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal

from budgetapp import networth
from budgetapp.dates import add_months, day_in_month
from budgetapp.money import round_cents

MAX_MONTHS = 1200  # 100 years

STRATEGIES: dict[str, str] = {
    "minimum": "Current payments, no rollover",
    "avalanche": "Avalanche: highest APR first",
    "snowball": "Snowball: smallest balance first",
}
MIN_RULES: dict[str, str] = {
    "fixed": "A fixed amount (the monthly payment)",
    "percent": "A percent of the balance",
    "none": "No minimum",
}
MAX_MIN_PERCENT = Decimal("0.5")


# ================================================================ single-loan math
@dataclass(frozen=True)
class Payment:
    number: int
    due: date | None
    payment_cents: int
    principal_cents: int
    interest_cents: int
    balance_cents: int


@dataclass(frozen=True)
class Payoff:
    months: int
    total_interest_cents: int
    total_paid_cents: int
    payoff_date: date | None


def standard_payment(principal_cents: int, annual_rate: Decimal, term_months: int) -> int:
    """Level monthly payment that retires the loan in term_months (rounded up to a cent)."""
    if term_months <= 0:
        raise ValueError("Term must be at least one month.")
    if principal_cents <= 0:
        return 0
    r = annual_rate / 12
    if r == 0:
        payment = Decimal(principal_cents) / term_months
    else:
        factor = (1 + r) ** term_months
        payment = Decimal(principal_cents) * r * factor / (factor - 1)
    return int(payment.to_integral_value(rounding=ROUND_CEILING))


def amortize(
    principal_cents: int,
    annual_rate: Decimal,
    payment_cents: int,
    *,
    extra_cents: int = 0,
    first_due: date | None = None,
) -> list[Payment]:
    """Month-by-month schedule. Interest accrues monthly on the remaining balance."""
    r = annual_rate / 12
    balance = principal_cents
    schedule: list[Payment] = []
    while balance > 0:
        n = len(schedule) + 1
        if n > MAX_MONTHS:
            raise ValueError("Loan does not pay off within 100 years at this payment.")
        interest = round_cents(Decimal(balance) * r)
        paid = min(payment_cents + extra_cents, balance + interest)
        principal = paid - interest
        if principal <= 0:
            raise ValueError("Payment does not cover the monthly interest.")
        balance -= principal
        due = add_months(first_due, n - 1) if first_due else None
        schedule.append(Payment(n, due, paid, principal, interest, balance))
    return schedule


def payoff(schedule: list[Payment]) -> Payoff:
    return Payoff(
        months=len(schedule),
        total_interest_cents=sum(p.interest_cents for p in schedule),
        total_paid_cents=sum(p.payment_cents for p in schedule),
        payoff_date=schedule[-1].due if schedule else None,
    )


# ================================================================ multi-loan strategies
@dataclass(frozen=True)
class Debt:
    id: int
    name: str
    balance_cents: int
    annual_rate: Decimal
    min_payment_cents: int
    planned_cents: int | None = None  # what is paid each month; the minimum if not given
    min_rule: str = "fixed"  # "fixed" (min_payment_cents), "percent" of the balance, "none"
    min_percent: Decimal | None = None
    promo_payments: int | None = None  # payments left before a promo ends, if it has one

    @property
    def planned(self) -> int:
        return self.min_payment_cents if self.planned_cents is None else self.planned_cents

    def required(self, balance_cents: int) -> int:
        """The minimum due in a month on this balance."""
        if self.min_rule == "none":
            return 0
        if self.min_rule == "percent":
            return round_cents(Decimal(balance_cents) * (self.min_percent or 0))
        return self.min_payment_cents


@dataclass(frozen=True)
class DebtPlan:
    strategy: str
    months: int
    total_interest_cents: int
    payoff_month: dict[int, int]
    promo_missed: tuple[int, ...] = ()  # debts still owing when their promo ended

    @property
    def order(self) -> list[int]:
        """Debt ids in the order they get paid off."""
        return sorted(self.payoff_month, key=lambda debt_id: (self.payoff_month[debt_id], debt_id))


def plan_payoff(
    debts: list[Debt], *, extra_cents: int = 0, strategy: str = "avalanche"
) -> DebtPlan:
    """Simulate paying several debts from one fixed monthly budget.

    The budget is every debt's planned payment plus `extra_cents`, and it doesn't shrink
    when a debt is paid off, so that debt's payment rolls on to the next. Each month:
      1. every debt gets its minimum (fixed, a percent of the balance, or nothing);
      2. a debt with a promo deadline is topped up to the pace that clears it in time.
         A deferred-interest promo charges interest on the whole purchase if a balance is
         left, so until then it is the costliest debt to fall behind on, whatever its APR;
      3. whatever is left goes to the strategy's order: avalanche, highest APR first, or
         snowball, smallest balance first.
    "minimum" is the baseline to compare against: each debt pays its own planned payment
    and nothing rolls over or moves between debts; `extra_cents` is left out.
    """
    if strategy == "avalanche":
        order = sorted(debts, key=lambda d: (-d.annual_rate, d.balance_cents, d.id))
    elif strategy == "snowball":
        order = sorted(debts, key=lambda d: (d.balance_cents, -d.annual_rate, d.id))
    elif strategy == "minimum":
        order = list(debts)
    else:
        raise ValueError(f"Unknown strategy {strategy!r}.")
    pooled = strategy != "minimum"
    budget = sum(d.planned for d in debts) + extra_cents
    promos = sorted(
        (d for d in debts if d.promo_payments is not None), key=lambda d: (d.promo_payments, d.id)
    )

    balance = {d.id: d.balance_cents for d in debts}
    paid_off = {d.id: 0 for d in debts if d.balance_cents <= 0}
    missed = {d.id for d in promos if d.promo_payments == 0 and d.balance_cents > 0}
    paid = dict.fromkeys(balance, 0)  # this month's payments, per debt

    def pay(debt: Debt, amount: int) -> int:
        amount = min(amount, balance[debt.id])
        if amount <= 0:
            return 0
        balance[debt.id] -= amount
        paid[debt.id] += amount
        return amount

    total_interest = 0
    month = 0
    while len(paid_off) < len(debts):
        month += 1
        if month > MAX_MONTHS:
            raise ValueError("A minimum payment is below its monthly interest; never pays off.")
        opening = dict(balance)
        paid.update(dict.fromkeys(paid, 0))
        for d in order:
            if balance[d.id] > 0:
                interest = round_cents(Decimal(balance[d.id]) * d.annual_rate / 12)
                total_interest += interest
                balance[d.id] += interest
        if not pooled:
            for d in order:
                pay(d, max(d.planned, d.required(balance[d.id])))
        else:
            pool = budget
            for d in order:
                pool -= pay(d, d.required(balance[d.id]))
            for d in promos:
                left = d.promo_payments - month + 1
                if left >= 1 and pool > 0:
                    pace = standard_payment(opening[d.id], d.annual_rate, left)
                    pool -= pay(d, min(pool, pace - paid[d.id]))
            for d in order:
                if pool <= 0:
                    break
                pool -= pay(d, pool)
        for d in order:
            if balance[d.id] <= 0 and d.id not in paid_off:
                paid_off[d.id] = month
        for d in promos:
            if d.promo_payments == month and balance[d.id] > 0:
                missed.add(d.id)
    return DebtPlan(strategy, month, total_interest, paid_off, tuple(sorted(missed)))


# ================================================================ storage
@dataclass(frozen=True)
class Loan:
    account_id: int
    name: str
    institution: str
    original_principal_cents: int
    annual_rate: Decimal
    term_months: int | None
    start_date: date | None
    min_payment_cents: int
    extra_payment_cents: int
    payment_day: int | None
    balance_cents: int
    balance_as_of: date | None
    promo_ends_on: date | None = None  # end of a promotional (e.g. 0%) rate, if any
    min_rule: str = "fixed"  # how the required minimum is set: see MIN_RULES
    min_percent: Decimal | None = None  # for "percent": a fraction of the balance

    @property
    def payment_cents(self) -> int:
        """What is paid each month: the monthly payment plus any extra."""
        return self.min_payment_cents + self.extra_payment_cents

    @property
    def required_minimum_cents(self) -> int:
        """The minimum due at today's balance (0 when the loan has none)."""
        return min(self.as_debt().required(self.balance_cents), max(self.balance_cents, 0))

    def next_due(self, today: date) -> date:
        day = self.payment_day or (self.start_date.day if self.start_date else 1)
        due = day_in_month(today.year, today.month, day)
        if due <= today:
            following = add_months(today.replace(day=1), 1)
            due = day_in_month(following.year, following.month, day)
        return due

    def schedule(
        self, *, extra_cents: int | None = None, today: date | None = None
    ) -> list[Payment]:
        extra = self.extra_payment_cents if extra_cents is None else extra_cents
        return amortize(
            self.balance_cents,
            self.annual_rate,
            self.min_payment_cents,
            extra_cents=extra,
            first_due=self.next_due(today or date.today()),
        )

    def as_debt(self, today: date | None = None) -> Debt:
        """This loan for plan_payoff. With `today`, a promo still running counts its payments."""
        promo_payments = None
        if today is not None and self.promo_ends_on is not None and self.promo_ends_on >= today:
            promo_payments = len(due_dates(self, today, self.promo_ends_on))
        return Debt(
            self.account_id,
            self.name,
            self.balance_cents,
            self.annual_rate,
            self.min_payment_cents,
            planned_cents=self.payment_cents,
            min_rule=self.min_rule,
            min_percent=self.min_percent,
            promo_payments=promo_payments,
        )


@dataclass(frozen=True)
class LoanPayment:
    id: int
    paid_on: date
    amount_cents: int
    principal_cents: int | None
    interest_cents: int | None
    notes: str


_LOAN_SQL = """
SELECT l.account_id, a.name, a.institution, l.original_principal_cents, l.annual_rate,
       l.term_months, l.start_date, l.min_payment_cents, l.extra_payment_cents, l.payment_day,
       COALESCE(s.balance_cents, 0) AS balance_cents, s.as_of AS balance_as_of, l.promo_ends_on,
       l.min_rule, l.min_payment_percent AS min_percent
FROM loans l
JOIN accounts a ON a.id = l.account_id
LEFT JOIN balance_snapshots s ON s.id = (
    SELECT id FROM balance_snapshots WHERE account_id = l.account_id ORDER BY as_of DESC LIMIT 1)
"""


def _loan(row: sqlite3.Row) -> Loan:
    data = dict(row)
    data["annual_rate"] = Decimal(data["annual_rate"])
    data["start_date"] = _date_or_none(data["start_date"])
    data["balance_as_of"] = _date_or_none(data["balance_as_of"])
    data["promo_ends_on"] = _date_or_none(data["promo_ends_on"])
    data["min_percent"] = Decimal(data["min_percent"]) if data["min_percent"] else None
    return Loan(**data)


def list_loans(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Loan]:
    rows = conn.execute(
        _LOAN_SQL + " WHERE (? OR a.archived = 0) ORDER BY a.name COLLATE NOCASE",
        (include_archived,),
    )
    return [_loan(row) for row in rows]


def get_loan(conn: sqlite3.Connection, account_id: int) -> Loan:
    row = conn.execute(_LOAN_SQL + " WHERE l.account_id = ?", (account_id,)).fetchone()
    if row is None:
        raise LookupError(f"Loan {account_id} not found.")
    return _loan(row)


def create_loan(
    conn: sqlite3.Connection,
    *,
    name: str,
    balance_cents: int,
    annual_rate: Decimal,
    min_payment_cents: int,
    institution: str = "",
    extra_payment_cents: int = 0,
    original_principal_cents: int | None = None,
    term_months: int | None = None,
    start_date: date | None = None,
    payment_day: int | None = None,
    promo_ends_on: date | None = None,
    as_of: date | None = None,
    min_rule: str = "fixed",
    min_percent: Decimal | None = None,
) -> int:
    _validate_terms(
        annual_rate, min_payment_cents, extra_payment_cents, term_months, payment_day,
        min_rule, min_percent,
    )
    if balance_cents < 0:
        raise ValueError("Balance can't be negative.")
    account_id = networth.add_account(conn, name=name, type="loan", institution=institution)
    conn.execute(
        "INSERT INTO loans (account_id, original_principal_cents, annual_rate, term_months, "
        "start_date, min_payment_cents, extra_payment_cents, payment_day, promo_ends_on, "
        "min_rule, min_payment_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            balance_cents if original_principal_cents is None else original_principal_cents,
            str(annual_rate),
            term_months,
            start_date.isoformat() if start_date else None,
            min_payment_cents,
            extra_payment_cents,
            payment_day,
            promo_ends_on.isoformat() if promo_ends_on else None,
            min_rule,
            str(min_percent) if min_rule == "percent" else None,
        ),
    )
    networth.record_balance(conn, account_id, balance_cents, as_of)
    return account_id


def update_loan(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    name: str,
    institution: str,
    annual_rate: Decimal,
    min_payment_cents: int,
    extra_payment_cents: int,
    original_principal_cents: int | None,
    term_months: int | None,
    start_date: date | None,
    payment_day: int | None,
    promo_ends_on: date | None = None,
    min_rule: str = "fixed",
    min_percent: Decimal | None = None,
) -> None:
    get_loan(conn, account_id)
    _validate_terms(
        annual_rate, min_payment_cents, extra_payment_cents, term_months, payment_day,
        min_rule, min_percent,
    )
    name = (name or "").strip()
    if not name:
        raise ValueError("Loan name is required.")
    conn.execute(
        "UPDATE accounts SET name = ?, institution = ? WHERE id = ?",
        (name, institution.strip(), account_id),
    )
    conn.execute(
        "UPDATE loans SET annual_rate = ?, min_payment_cents = ?, extra_payment_cents = ?, "
        "original_principal_cents = COALESCE(?, original_principal_cents), term_months = ?, "
        "start_date = ?, payment_day = ?, promo_ends_on = ?, min_rule = ?, "
        "min_payment_percent = ? WHERE account_id = ?",
        (
            str(annual_rate),
            min_payment_cents,
            extra_payment_cents,
            original_principal_cents,
            term_months,
            start_date.isoformat() if start_date else None,
            payment_day,
            promo_ends_on.isoformat() if promo_ends_on else None,
            min_rule,
            str(min_percent) if min_rule == "percent" else None,
            account_id,
        ),
    )


# ---------------------------------------------------------------- promotional rates
@dataclass(frozen=True)
class PromoCheck:
    ends_on: date
    payments_left: int  # due dates from the next payment through the promo end
    needed_cents: int  # per payment to clear the balance by then
    planned_cents: int  # what the loan's plan pays (payment + extra)
    ended: bool  # the promo is over and a balance is left

    @property
    def on_track(self) -> bool:
        return not self.ended and self.needed_cents <= self.planned_cents


def promo_check(loan: Loan, today: date) -> PromoCheck | None:
    """Does the planned payment clear the balance before the promo ends? With a
    deferred-interest promo, a balance left on that date can be charged interest back to
    the purchase date. None if the loan has no promo or is paid off."""
    if loan.promo_ends_on is None or loan.balance_cents <= 0:
        return None
    left = len(due_dates(loan, today, loan.promo_ends_on))
    needed = (
        standard_payment(loan.balance_cents, loan.annual_rate, left) if left
        else loan.balance_cents
    )
    return PromoCheck(
        loan.promo_ends_on, left, needed, loan.payment_cents, today > loan.promo_ends_on
    )


def due_dates(loan: Loan, after: date, until: date) -> list[date]:
    """Payment due dates after `after`, up to and including `until`."""
    day = loan.payment_day or (loan.start_date.day if loan.start_date else 1)
    month = loan.next_due(after).replace(day=1)
    dues = []
    while (due := day_in_month(month.year, month.month, day)) <= until:
        dues.append(due)
        month = add_months(month, 1)
    return dues


def record_payment(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    paid_on: date,
    amount_cents: int,
    principal_cents: int | None = None,
    interest_cents: int | None = None,
    notes: str = "",
) -> int:
    """Log a payment and lower the balance by its principal.

    Without a principal/interest split (from the statement), interest is estimated
    as one month at the loan's APR on the current balance.
    """
    loan = get_loan(conn, account_id)
    if amount_cents <= 0:
        raise ValueError("Payment amount must be more than $0.")
    if loan.balance_as_of and paid_on < loan.balance_as_of:
        raise ValueError(
            f"Payment date is before the latest recorded balance ({loan.balance_as_of})."
        )
    if principal_cents is None and interest_cents is None:
        estimate = round_cents(Decimal(loan.balance_cents) * loan.annual_rate / 12)
        interest_cents = min(amount_cents, estimate)
    if principal_cents is None:
        principal_cents = amount_cents - interest_cents
    elif interest_cents is None:
        interest_cents = amount_cents - principal_cents
    split_ok = principal_cents + interest_cents == amount_cents
    if principal_cents < 0 or interest_cents < 0 or not split_ok:
        raise ValueError("Principal plus interest must equal the payment amount.")
    if principal_cents > loan.balance_cents:
        raise ValueError("That payment is more than the remaining balance plus interest.")
    cur = conn.execute(
        "INSERT INTO loan_payments (account_id, paid_on, amount_cents, principal_cents, "
        "interest_cents, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, paid_on.isoformat(), amount_cents, principal_cents, interest_cents, notes),
    )
    networth.record_balance(conn, account_id, loan.balance_cents - principal_cents, paid_on)
    return cur.lastrowid


def payments(conn: sqlite3.Connection, account_id: int) -> list[LoanPayment]:
    rows = conn.execute(
        "SELECT id, paid_on, amount_cents, principal_cents, interest_cents, notes "
        "FROM loan_payments WHERE account_id = ? ORDER BY paid_on DESC, id DESC",
        (account_id,),
    )
    return [
        LoanPayment(**(dict(row) | {"paid_on": date.fromisoformat(row["paid_on"])})) for row in rows
    ]


def delete_latest_payment(conn: sqlite3.Connection, account_id: int, payment_id: int) -> None:
    """Undo the most recent payment (restores the balance it lowered)."""
    history = payments(conn, account_id)
    if not history or history[0].id != payment_id:
        raise ValueError("Only the most recent payment can be removed.")
    latest = history[0]
    conn.execute("DELETE FROM loan_payments WHERE id = ?", (payment_id,))
    loan = get_loan(conn, account_id)
    if latest.principal_cents and loan.balance_as_of == latest.paid_on:
        networth.record_balance(
            conn, account_id, loan.balance_cents + latest.principal_cents, latest.paid_on
        )


def _validate_terms(
    annual_rate: Decimal,
    min_payment_cents: int,
    extra_payment_cents: int,
    term_months: int | None,
    payment_day: int | None,
    min_rule: str = "fixed",
    min_percent: Decimal | None = None,
) -> None:
    if not Decimal(0) <= annual_rate < Decimal(1):
        raise ValueError("APR must be between 0% and 100%.")
    if min_rule not in MIN_RULES:
        raise ValueError("Choose how the minimum payment is set.")
    if min_rule == "fixed" and min_payment_cents <= 0:
        raise ValueError("Minimum payment must be more than $0.")
    if min_rule == "percent" and not (
        min_percent is not None and Decimal(0) < min_percent <= MAX_MIN_PERCENT
    ):
        raise ValueError("The minimum must be more than 0% and at most 50% of the balance.")
    if min_payment_cents < 0:
        raise ValueError("Monthly payment can't be negative.")
    if extra_payment_cents < 0:
        raise ValueError("Extra payment can't be negative.")
    if min_payment_cents + extra_payment_cents <= 0:
        raise ValueError("Enter what you pay each month, so the loan has a payoff date.")
    if term_months is not None and not 1 <= term_months <= MAX_MONTHS:
        raise ValueError("Term must be between 1 and 1200 months.")
    if payment_day is not None and not 1 <= payment_day <= 31:
        raise ValueError("Payment day must be between 1 and 31.")


def _date_or_none(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None
