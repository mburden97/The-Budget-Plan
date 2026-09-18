"""Savings goals: sinking funds for irregular bills, and the emergency fund."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from budgetapp import networth, planning, settings
from budgetapp.dates import months_until


@dataclass(frozen=True)
class SinkingFund:
    id: int
    name: str
    target_cents: int
    due_date: date | None
    saved_cents: int
    line_item_id: int | None
    line_item_name: str | None
    line_item_amount_cents: int | None
    line_item_frequency: str | None
    notes: str
    budget_mode: str = "average"

    @property
    def remaining_cents(self) -> int:
        return max(0, self.target_cents - self.saved_cents)

    @property
    def progress(self) -> Decimal:
        return Decimal(min(self.saved_cents, self.target_cents)) / Decimal(self.target_cents)

    @property
    def budgeted_monthly_cents(self) -> int | None:
        if self.line_item_amount_cents is None or self.line_item_frequency is None:
            return None
        return planning.monthly_equivalent(
            self.line_item_amount_cents, self.line_item_frequency, self.budget_mode
        )

    def months_left(self, today: date) -> int | None:
        return months_until(today, self.due_date) if self.due_date else None

    def needed_monthly_cents(self, today: date) -> int | None:
        """Monthly saving needed to be fully funded by the due date (rounded up)."""
        if self.remaining_cents == 0:
            return 0
        months = self.months_left(today)
        return None if months is None else -(-self.remaining_cents // months)

    def on_track(self, today: date) -> bool | None:
        """Whether the linked budget line sets aside enough; None if it can't be judged."""
        if self.remaining_cents == 0:
            return True
        needed, budgeted = self.needed_monthly_cents(today), self.budgeted_monthly_cents
        if needed is None or budgeted is None:
            return None
        return budgeted >= needed


@dataclass(frozen=True)
class EmergencyFund:
    target_months: int
    essential_monthly_cents: int
    saved_cents: int  # flagged account balances minus what sinking funds have saved there
    accounts: list[str]
    monthly_interest_cents: int = 0  # what the flagged accounts earn at their APY
    held_cents: int = 0  # the flagged accounts' balances
    set_aside_cents: int = 0  # sinking-fund savings, assumed to sit in those accounts

    @property
    def target_cents(self) -> int:
        return self.essential_monthly_cents * self.target_months

    @property
    def months_covered(self) -> Decimal | None:
        if self.essential_monthly_cents <= 0:
            return None
        return Decimal(self.saved_cents) / Decimal(self.essential_monthly_cents)

    @property
    def progress(self) -> Decimal:
        if self.target_cents <= 0:
            return Decimal(0)
        return min(Decimal(1), Decimal(max(self.saved_cents, 0)) / Decimal(self.target_cents))

    @property
    def shortfall_cents(self) -> int:
        return max(0, self.target_cents - self.saved_cents)


# ---------------------------------------------------------------- sinking funds
_FUND_SQL = """
SELECT f.id, f.name, f.target_cents, f.due_date, f.saved_cents, f.line_item_id,
       li.name AS line_item_name, li.amount_cents AS line_item_amount_cents,
       li.frequency AS line_item_frequency, f.notes
FROM sinking_funds f LEFT JOIN line_items li ON li.id = f.line_item_id
"""


def list_funds(conn: sqlite3.Connection) -> list[SinkingFund]:
    rows = conn.execute(
        _FUND_SQL + " ORDER BY f.due_date IS NULL, f.due_date, f.name COLLATE NOCASE"
    )
    mode = planning.budget_mode(conn)
    funds = []
    for row in rows:
        due = date.fromisoformat(row["due_date"]) if row["due_date"] else None
        funds.append(SinkingFund(**(dict(row) | {"due_date": due, "budget_mode": mode})))
    return funds


def add_fund(
    conn: sqlite3.Connection,
    *,
    name: str,
    target_cents: int,
    due_date: date | None = None,
    saved_cents: int = 0,
    line_item_id: int | None = None,
    notes: str = "",
) -> int:
    name = _clean(conn, name, target_cents, saved_cents, line_item_id)
    cur = conn.execute(
        "INSERT INTO sinking_funds (name, target_cents, due_date, saved_cents, line_item_id, "
        "notes) VALUES (?, ?, ?, ?, ?, ?)",
        (name, target_cents, _iso(due_date), saved_cents, line_item_id, notes.strip()),
    )
    return cur.lastrowid


def update_fund(
    conn: sqlite3.Connection,
    fund_id: int,
    *,
    name: str,
    target_cents: int,
    due_date: date | None,
    saved_cents: int,
    line_item_id: int | None,
    notes: str = "",
) -> None:
    name = _clean(conn, name, target_cents, saved_cents, line_item_id)
    cur = conn.execute(
        "UPDATE sinking_funds SET name = ?, target_cents = ?, due_date = ?, saved_cents = ?, "
        "line_item_id = ?, notes = ? WHERE id = ?",
        (name, target_cents, _iso(due_date), saved_cents, line_item_id, notes.strip(), fund_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"Sinking fund {fund_id} not found.")


def delete_fund(conn: sqlite3.Connection, fund_id: int) -> None:
    if conn.execute("DELETE FROM sinking_funds WHERE id = ?", (fund_id,)).rowcount == 0:
        raise LookupError(f"Sinking fund {fund_id} not found.")


def fund_transfer(
    conn: sqlite3.Connection,
    fund_id: int,
    amount_cents: int,
    *,
    to_account_id: int,
    from_account_id: int | None = None,
) -> None:
    """Credit a sinking fund for money moved into emergency-fund savings, or debit it for
    money moved out of them (e.g. to pay the bill it was saved for)."""
    if amount_cents <= 0:
        raise ValueError("Transfer amount must be more than $0.")
    into = networth.get_account(conn, to_account_id).emergency_fund
    out_of = (
        from_account_id is not None
        and networth.get_account(conn, from_account_id).emergency_fund
    )
    if into == out_of:
        raise ValueError(
            "Money for a sinking fund has to go into or out of your emergency-fund savings."
        )
    row = conn.execute(
        "SELECT name, saved_cents FROM sinking_funds WHERE id = ?", (fund_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Sinking fund {fund_id} not found.")
    saved = row["saved_cents"] + (amount_cents if into else -amount_cents)
    if saved < 0:
        raise ValueError(f"That's more than “{row['name']}” has saved.")
    conn.execute("UPDATE sinking_funds SET saved_cents = ? WHERE id = ?", (saved, fund_id))


def _clean(
    conn: sqlite3.Connection,
    name: str,
    target_cents: int,
    saved_cents: int,
    line_item_id: int | None,
) -> str:
    """Validate fund fields; returns the cleaned name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Fund name is required.")
    if len(name) > 100:
        raise ValueError("Fund name must be 100 characters or fewer.")
    if target_cents <= 0:
        raise ValueError("Target must be more than $0.")
    if saved_cents < 0:
        raise ValueError("Saved amount can't be negative.")
    if line_item_id is not None and not conn.execute(
        "SELECT 1 FROM line_items WHERE id = ?", (line_item_id,)
    ).fetchone():
        raise ValueError("Unknown budget line.")
    return name


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


# ---------------------------------------------------------------- emergency fund
def emergency_fund(conn: sqlite3.Connection) -> EmergencyFund:
    """Essential monthly spending (categories marked essential) vs. flagged account balances.

    Sinking-fund savings are assumed to sit in the same accounts, so they're subtracted:
    that money is spoken for.
    """
    essential = sum(
        group.monthly_cents
        for group in planning.grouped(conn)
        if group.category.essential and group.category.kind != "income"
    )
    accounts = [
        a for a in networth.list_accounts(conn) if a.emergency_fund and not a.is_liability
    ]
    held = sum(a.balance_cents or 0 for a in accounts)
    set_aside = sum(fund.saved_cents for fund in list_funds(conn))
    return EmergencyFund(
        target_months=settings.get_int(conn, "emergency_fund_months", 6),
        essential_monthly_cents=essential,
        saved_cents=max(0, held - set_aside),
        accounts=[a.name for a in accounts],
        monthly_interest_cents=sum(a.monthly_interest_cents or 0 for a in accounts),
        held_cents=held,
        set_aside_cents=set_aside,
    )
