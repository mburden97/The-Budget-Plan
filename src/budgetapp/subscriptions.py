"""Subscriptions: itemize the recurring charges in subscription budget lines, and spot
likely subscriptions still filed under other lines."""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from budgetapp import planning, transactions
from budgetapp.money import round_cents

# A budget line counts as a subscriptions line if its name says so.
_SUBSCRIPTION_LINE = re.compile(r"subscri|membership", re.I)
# Card processors put their own prefix before the merchant ("GOOGLE *", "ABC*", "FS *").
_PROCESSOR = re.compile(r"^[A-Z0-9]{1,6} ?\*\s*")

# label, typical days between charges, accepted range of gaps, charges per year
_CADENCES = (
    ("Weekly", 7, 5, 10, 52),
    ("Monthly", 30, 25, 35, 12),
    ("Quarterly", 91, 80, 100, 4),
    ("Twice a year", 182, 165, 200, 2),
    ("Yearly", 365, 330, 400, 1),
)


@dataclass(frozen=True)
class Charge:
    posted_on: date
    amount_cents: int  # money spent; a refund is negative
    account: str | None


@dataclass(frozen=True)
class Subscription:
    name: str
    pattern: str  # text that identifies its charges (a rule's pattern or the merchant)
    charges: list[Charge] = field(default_factory=list)  # oldest first
    line_name: str | None = None  # where it is filed (for candidates)

    @property
    def last(self) -> Charge:
        return self.charges[-1]

    @property
    def is_credit(self) -> bool:
        """Only money back, e.g. a card's monthly statement credit for a membership."""
        return all(c.amount_cents < 0 for c in self.charges)

    @property
    def _basis(self) -> list[Charge]:
        """The charges that set the price and rhythm: payments, or the credits of a credit."""
        if self.is_credit:
            return self.charges
        return [c for c in self.charges if c.amount_cents > 0]

    @property
    def typical_cents(self) -> int:
        amounts = [c.amount_cents for c in self._basis]
        return Counter(amounts).most_common(1)[0][0] if amounts else self.last.amount_cents

    @property
    def cadence(self) -> tuple | None:
        days = sorted({c.posted_on for c in self._basis})
        if len(days) < 2:
            return None
        gap = statistics.median((b - a).days for a, b in zip(days, days[1:], strict=False))
        return next((c for c in _CADENCES if c[2] <= gap <= c[3]), None)

    @property
    def cadence_label(self) -> str:
        if self.cadence:
            return self.cadence[0]
        return "One-off" if len(self._basis) == 1 else "Irregular"

    @property
    def monthly_cents(self) -> int | None:
        cadence = self.cadence
        if cadence is None:
            return None
        return round_cents(Decimal(self.typical_cents) * cadence[4] / 12)

    @property
    def month_count(self) -> int:
        return len({(c.posted_on.year, c.posted_on.month) for c in self.charges})

    @property
    def accounts(self) -> list[str]:
        return sorted({c.account for c in self.charges if c.account})

    def is_active(self, today: date) -> bool:
        """Charged recently enough that the next charge is still expected."""
        cadence = self.cadence
        window = cadence[1] * 1.5 + 5 if cadence else 45
        return (today - self.last.posted_on).days <= window

    def next_due(self, today: date) -> date | None:
        cadence = self.cadence
        if cadence is None or not self.is_active(today):
            return None
        due = self.last.posted_on + timedelta(days=cadence[1])
        while due < today:
            due += timedelta(days=cadence[1])
        return due

    def spent_since(self, start: date) -> int:
        return sum(c.amount_cents for c in self.charges if c.posted_on >= start)


def subscription_lines(conn: sqlite3.Connection) -> list[planning.LineItem]:
    return [
        item
        for group in planning.grouped(conn)
        if group.category.kind != "income"
        for item in group.items
        if _SUBSCRIPTION_LINE.search(item.name)
    ]


_TXN_SQL = """
SELECT t.posted_on, t.description, t.amount_cents, t.line_item_id, li.name AS line_name,
       a.name AS account_name
FROM allocations t
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN line_items li ON li.id = t.line_item_id
WHERE t.excluded = 0 AND t.amount_cents != 0
"""


def itemize(conn: sqlite3.Connection) -> list[Subscription]:
    """One entry per subscription in the subscription lines, grouped by the rule that
    files its charges (so a merchant's different spellings stay together)."""
    line_ids = {item.id for item in subscription_lines(conn)}
    if not line_ids:
        return []
    rules = [r for r in transactions.list_rules(conn) if r.line_item_id in line_ids]
    charges: dict[str, list[Charge]] = defaultdict(list)
    spellings: dict[str, Counter] = defaultdict(Counter)
    placeholders = ",".join("?" * len(line_ids))
    for row in conn.execute(
        _TXN_SQL
        + f" AND t.line_item_id IN ({placeholders}) ORDER BY t.posted_on, t.transaction_id",
        tuple(line_ids),
    ):
        merchant = transactions.suggest_pattern(row["description"])
        rule = transactions.match(rules, row["description"])
        key = (rule.pattern if rule else merchant).upper()
        charges[key].append(_charge(row))
        spellings[key][merchant] += 1
    return [
        Subscription(_display(spellings[key].most_common(1)[0][0]), key, found)
        for key, found in charges.items()
    ]


def candidates(conn: sqlite3.Connection) -> list[Subscription]:
    """Charges outside the subscription lines that repeat about monthly at a steady amount."""
    line_ids = {item.id for item in subscription_lines(conn)}
    charges: dict[str, list[Charge]] = defaultdict(list)
    filed: dict[str, str | None] = {}
    for row in conn.execute(
        _TXN_SQL + " AND t.amount_cents < 0 ORDER BY t.posted_on, t.transaction_id"
    ):
        if row["line_item_id"] in line_ids:
            continue
        key = transactions.suggest_pattern(row["description"]).upper()
        if key.startswith("ZELLE"):
            continue  # payments to people, not services
        charges[key].append(_charge(row))
        filed[key] = row["line_name"]
    found = []
    for key, rows in charges.items():
        months = {(c.posted_on.year, c.posted_on.month) for c in rows}
        if len(months) < 3 or len(rows) / len(months) > 1.3:
            continue
        amounts = [c.amount_cents for c in rows]
        typical = Counter(amounts).most_common(1)[0][0]
        steady = sum(abs(a - typical) <= max(100, typical // 20) for a in amounts) / len(amounts)
        if steady >= 0.6 and typical < 30000:
            found.append(Subscription(_display(key), key, rows, filed[key]))
    return sorted(found, key=lambda s: -s.typical_cents)


def release(conn: sqlite3.Connection, pattern: str, line_item_id: int) -> int:
    """Take a merchant out of the subscription lines: move its charges filed there to
    `line_item_id` and drop the rules that send it to a subscription line."""
    line_ids = [item.id for item in subscription_lines(conn)]
    if not line_ids:
        return 0
    needle = " ".join(pattern.split()).lower()
    if not needle:
        raise ValueError("Nothing to match.")
    for rule in transactions.list_rules(conn):
        if rule.line_item_id in line_ids and rule.pattern.lower() == needle:
            transactions.delete_rule(conn, rule.id)
        elif rule.split_line_item_id in line_ids and rule.split_note.lower() == needle:
            conn.execute(
                "UPDATE category_rules SET split_line_item_id = ? WHERE id = ?",
                (line_item_id, rule.id),
            )
    marks = ",".join("?" * len(line_ids))
    moved = conn.execute(
        f"UPDATE transactions SET line_item_id = ? WHERE line_item_id IN ({marks}) "
        "AND instr(lower(description), ?) > 0",
        (line_item_id, *line_ids, needle),
    ).rowcount
    moved += conn.execute(  # split parts, matched on their label (or the charge's text)
        f"UPDATE transaction_splits SET line_item_id = ? WHERE line_item_id IN ({marks}) "
        "AND instr(lower(CASE WHEN note != '' THEN note ELSE (SELECT description FROM "
        "transactions t WHERE t.id = transaction_id) END), ?) > 0",
        (line_item_id, *line_ids, needle),
    ).rowcount
    return moved


@dataclass(frozen=True)
class Cancellation:
    pattern: str  # an itemized subscription's key, upper case
    name: str
    cancelled_on: date


@dataclass(frozen=True)
class LateCharge:
    """A charge from a cancelled subscription that posted after the cancel date."""

    pattern: str
    subscription: str
    posted_on: date
    amount_cents: int  # money spent
    account: str | None
    transaction_id: int


def list_cancelled(conn: sqlite3.Connection) -> list[Cancellation]:
    rows = conn.execute(
        "SELECT pattern, name, cancelled_on FROM cancelled_subscriptions "
        "ORDER BY cancelled_on DESC, name COLLATE NOCASE"
    )
    return [
        Cancellation(row["pattern"], row["name"], date.fromisoformat(row["cancelled_on"]))
        for row in rows
    ]


def cancel(conn: sqlite3.Connection, pattern: str, name: str, on: date) -> None:
    """Mark a subscription cancelled as of `on`; marking it again moves the date."""
    pattern = " ".join(pattern.split()).upper()
    if len(pattern) < 3:
        raise ValueError("Nothing to match.")
    conn.execute(
        "INSERT INTO cancelled_subscriptions (pattern, name, cancelled_on) VALUES (?, ?, ?) "
        "ON CONFLICT (pattern) DO UPDATE SET cancelled_on = excluded.cancelled_on",
        (pattern, (name.strip() or pattern)[:80], on.isoformat()),
    )


def uncancel(conn: sqlite3.Connection, pattern: str) -> None:
    conn.execute(
        "DELETE FROM cancelled_subscriptions WHERE pattern = ?", (" ".join(pattern.split()),)
    )


def charges_after_cancel(
    conn: sqlite3.Connection, txn_ids: Iterable[int] | None = None
) -> list[LateCharge]:
    """Money out matching a cancelled subscription, posted after its cancel date (split parts
    count by their label). With `txn_ids`, only those transactions are checked."""
    wanted = set(txn_ids) if txn_ids is not None else None
    found = []
    for cancelled in list_cancelled(conn):
        rows = conn.execute(
            "SELECT t.transaction_id, t.posted_on, t.amount_cents, a.name AS account_name "
            "FROM allocations t LEFT JOIN accounts a ON a.id = t.account_id "
            "WHERE t.amount_cents < 0 AND t.posted_on > ? AND instr(upper(t.description), ?) > 0",
            (cancelled.cancelled_on.isoformat(), cancelled.pattern.upper()),
        )
        found += [
            LateCharge(cancelled.pattern, cancelled.name, date.fromisoformat(row["posted_on"]),
                       -row["amount_cents"], row["account_name"], row["transaction_id"])
            for row in rows
            if wanted is None or row["transaction_id"] in wanted
        ]
    return sorted(found, key=lambda charge: (charge.posted_on, charge.subscription))


def default_other_line(conn: sqlite3.Connection) -> planning.LineItem | None:
    """Where released charges go: a Discretionary line if there is one."""
    items = [
        item for group in planning.grouped(conn) if group.category.kind == "expense"
        for item in group.items if not _SUBSCRIPTION_LINE.search(item.name)
    ]
    fallback = items[0] if items else None
    return next((i for i in items if "discretionary" in i.name.lower()), fallback)


def _charge(row: sqlite3.Row) -> Charge:
    return Charge(date.fromisoformat(row["posted_on"]), -row["amount_cents"], row["account_name"])


def _display(merchant: str) -> str:
    if merchant != merchant.upper():  # typed by the user (a split's label): keep as written
        return merchant.strip()
    name = _PROCESSOR.sub("", merchant.strip()) or merchant
    return " ".join(word.capitalize() for word in name.split())
