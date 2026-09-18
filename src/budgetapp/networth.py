"""Accounts, balance snapshots and net worth."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from budgetapp.dates import add_months, month_end
from budgetapp.money import round_cents

ACCOUNT_TYPES: dict[str, str] = {
    "cash": "Cash (checking / savings)",
    "brokerage": "Brokerage",
    "retirement": "Retirement (401k / IRA)",
    "crypto": "Crypto (exchange / wallet)",
    "property": "Property / vehicle",
    "other_asset": "Other asset",
    "loan": "Loan",
    "credit_card": "Credit card",
    "other_liability": "Other liability",
}
INVESTMENT_TYPES = ("brokerage", "retirement", "crypto")


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    type: str
    institution: str
    is_liability: bool
    include_in_net_worth: bool
    emergency_fund: bool
    archived: bool
    notes: str
    balance_cents: int | None
    balance_as_of: str | None
    apy: Decimal | None = None  # annual percentage yield, as a fraction

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPES[self.type]

    @property
    def monthly_interest_cents(self) -> int | None:
        """Interest one month earns on the latest balance at this APY (compounded)."""
        if self.apy is None or self.is_liability or not self.balance_cents:
            return None
        monthly_rate = (1 + self.apy) ** (Decimal(1) / 12) - 1
        return round_cents(Decimal(self.balance_cents) * monthly_rate)


@dataclass(frozen=True)
class NetWorth:
    as_of: str
    assets_cents: int
    liabilities_cents: int

    @property
    def net_cents(self) -> int:
        return self.assets_cents - self.liabilities_cents


_ACCOUNT_SQL = """
SELECT a.id, a.name, a.type, a.institution, a.is_liability, a.include_in_net_worth,
       a.emergency_fund, a.archived, a.notes, a.apy,
       s.balance_cents, s.as_of AS balance_as_of
FROM accounts a
LEFT JOIN balance_snapshots s ON s.id = (
    SELECT id FROM balance_snapshots WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1)
"""
_FLAGS = ("is_liability", "include_in_net_worth", "emergency_fund", "archived")


def _account(row: sqlite3.Row) -> Account:
    data = dict(row)
    for flag in _FLAGS:
        data[flag] = bool(data[flag])
    data["apy"] = Decimal(data["apy"]) if data["apy"] else None
    return Account(**data)


# ---------------------------------------------------------------- accounts
def list_accounts(
    conn: sqlite3.Connection, *, include_archived: bool = False, types: tuple[str, ...] = ()
) -> list[Account]:
    sql = _ACCOUNT_SQL + " WHERE (? OR a.archived = 0)"
    params: list = [include_archived]
    if types:
        sql += f" AND a.type IN ({','.join('?' * len(types))})"
        params.extend(types)
    sql += " ORDER BY a.is_liability, a.type, a.name COLLATE NOCASE"
    return [_account(row) for row in conn.execute(sql, params)]


def get_account(conn: sqlite3.Connection, account_id: int) -> Account:
    row = conn.execute(_ACCOUNT_SQL + " WHERE a.id = ?", (account_id,)).fetchone()
    if row is None:
        raise LookupError(f"Account {account_id} not found.")
    return _account(row)


def add_account(
    conn: sqlite3.Connection,
    *,
    name: str,
    type: str,
    institution: str = "",
    notes: str = "",
    emergency_fund: bool = False,
) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Account name is required.")
    if type not in ACCOUNT_TYPES:
        raise ValueError("Unknown account type.")
    cur = conn.execute(
        "INSERT INTO accounts (name, type, institution, notes, emergency_fund) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, type, institution.strip(), notes.strip(), int(emergency_fund)),
    )
    return cur.lastrowid


def update_account(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    name: str,
    institution: str,
    notes: str,
    include_in_net_worth: bool,
    emergency_fund: bool,
    archived: bool,
    apy: Decimal | None = None,
) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("Account name is required.")
    if apy is not None and not Decimal(0) <= apy < Decimal(1):
        raise ValueError("APY must be between 0% and 100%.")
    cur = conn.execute(
        "UPDATE accounts SET name = ?, institution = ?, notes = ?, include_in_net_worth = ?, "
        "emergency_fund = ?, archived = ?, apy = ? WHERE id = ?",
        (
            name,
            institution.strip(),
            notes.strip(),
            int(include_in_net_worth),
            int(emergency_fund),
            int(archived),
            None if apy is None else str(apy),
            account_id,
        ),
    )
    if cur.rowcount == 0:
        raise LookupError(f"Account {account_id} not found.")


def delete_account(conn: sqlite3.Connection, account_id: int) -> None:
    """Deletes the account and (via cascades) its balances, loan details and holdings."""
    if conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount == 0:
        raise LookupError(f"Account {account_id} not found.")


def auto_valued_account_ids(conn: sqlite3.Connection) -> set[int]:
    """Accounts whose balance comes from holdings x prices rather than manual check-ins."""
    return {row[0] for row in conn.execute("SELECT DISTINCT account_id FROM holdings")}


# ---------------------------------------------------------------- balances
def record_balance(
    conn: sqlite3.Connection, account_id: int, balance_cents: int, as_of: date | None = None
) -> None:
    """Record (or overwrite) an account's balance on a date.

    Liabilities are recorded as positive amounts owed.
    """
    day = (as_of or date.today()).isoformat()
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, as_of, balance_cents) VALUES (?, ?, ?) "
        "ON CONFLICT (account_id, as_of) DO UPDATE SET balance_cents = excluded.balance_cents",
        (account_id, day, balance_cents),
    )


def record_transfer(
    conn: sqlite3.Connection,
    *,
    to_account_id: int,
    amount_cents: int,
    on: date,
    from_account_id: int | None = None,
) -> None:
    """Log money moved into a cash account (e.g. checking -> savings): raise its balance and
    lower the source account's, each from its latest balance as of `on`."""
    if amount_cents <= 0:
        raise ValueError("Transfer amount must be more than $0.")
    if from_account_id == to_account_id:
        raise ValueError("Choose two different accounts.")
    moves = [(to_account_id, amount_cents)]
    if from_account_id is not None:
        moves.append((from_account_id, -amount_cents))
    updates = []
    for account_id, delta in moves:  # validate both sides before writing either
        acct = get_account(conn, account_id)
        if acct.type != "cash" or acct.archived:
            raise ValueError(f"{acct.name} isn't a cash account.")
        if acct.balance_as_of and acct.balance_as_of > on.isoformat():
            raise ValueError(f"{acct.name} has a balance recorded after {on}; pick a later date.")
        updates.append((account_id, (acct.balance_cents or 0) + delta))
    for account_id, cents in updates:
        record_balance(conn, account_id, cents, on)


def snapshots(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, as_of, balance_cents FROM balance_snapshots WHERE account_id = ? "
        "ORDER BY as_of DESC",
        (account_id,),
    ).fetchall()


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Delete one balance entry; returns its account id."""
    row = conn.execute(
        "SELECT account_id FROM balance_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Balance entry {snapshot_id} not found.")
    conn.execute("DELETE FROM balance_snapshots WHERE id = ?", (snapshot_id,))
    return row["account_id"]


def stale_accounts(conn: sqlite3.Connection, *, days: int, today: date) -> list[Account]:
    """Manually tracked accounts with no balance recorded in the last `days` days."""
    cutoff = (today - timedelta(days=days)).isoformat()
    auto = auto_valued_account_ids(conn)
    return [
        a
        for a in list_accounts(conn)
        if a.include_in_net_worth
        and a.id not in auto
        and (a.balance_as_of is None or a.balance_as_of < cutoff)
    ]


# ---------------------------------------------------------------- net worth
def net_worth(conn: sqlite3.Connection, as_of: date | None = None) -> NetWorth:
    """Sum each account's most recent balance on or before as_of."""
    day = (as_of or date.today()).isoformat()
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN a.is_liability = 0 THEN s.balance_cents END), 0) AS assets,
            COALESCE(SUM(CASE WHEN a.is_liability = 1 THEN s.balance_cents END), 0) AS liabilities
        FROM accounts a
        JOIN balance_snapshots s ON s.account_id = a.id
        WHERE a.include_in_net_worth = 1
          AND s.as_of = (SELECT MAX(as_of) FROM balance_snapshots
                         WHERE account_id = a.id AND as_of <= :day)
        """,
        {"day": day},
    ).fetchone()
    return NetWorth(day, row["assets"], row["liabilities"])


def history(conn: sqlite3.Connection, *, today: date, max_points: int = 36) -> list[NetWorth]:
    """Net worth at each month end since the first recorded balance, plus today."""
    first = conn.execute("SELECT MIN(as_of) FROM balance_snapshots").fetchone()[0]
    if not first:
        return []
    points = []
    point = month_end(date.fromisoformat(first))
    while point < today:
        points.append(point)
        point = month_end(add_months(point.replace(day=1), 1))
    points.append(today)
    return [net_worth(conn, p) for p in points[-max_points:]]
