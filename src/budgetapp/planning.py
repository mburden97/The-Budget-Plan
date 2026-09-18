"""Budget plan: categories and line items, normalized to monthly amounts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from budgetapp import settings
from budgetapp.money import round_cents

# key -> (label, occurrences per year)
FREQUENCIES: dict[str, tuple[str, int]] = {
    "weekly": ("Weekly", 52),
    "biweekly": ("Every 2 weeks", 26),
    "semimonthly": ("Twice a month", 24),
    "monthly": ("Monthly", 12),
    "quarterly": ("Quarterly", 4),
    "annual": ("Yearly", 1),
}

KINDS: dict[str, str] = {
    "income": "Income",
    "expense": "Expense",
    "savings": "Savings & investing",
    "debt": "Debt payment",
}

# Offered when a new vault is created, all at $0, so there is something to file
# transactions under from day one. (category, line) pairs; categories are the seeded ones.
STARTER_LINES: tuple[tuple[str, str], ...] = (
    ("Income", "Paycheck"),
    ("Fixed Expenses", "Rent / mortgage"),
    ("Fixed Expenses", "Utilities"),
    ("Fixed Expenses", "Phone"),
    ("Fixed Expenses", "Internet"),
    ("Fixed Expenses", "Insurance"),
    ("Fixed Expenses", "Subscriptions"),
    ("Flexible Expenses", "Groceries"),
    ("Flexible Expenses", "Dining out"),
    ("Flexible Expenses", "Fuel & transit"),
    ("Flexible Expenses", "Entertainment"),
    ("Flexible Expenses", "Shopping"),
    ("Savings & Investing", "Emergency fund"),
)

MAX_NAME = 100
MAX_NOTES = 500


# How a month is counted.
#   two_paycheck: a month is two paychecks, so bi-weekly lines count twice and weekly lines
#                 four times; the two 3-paycheck months a year are windfalls outside the plan.
#   average:      everything averaged over the year (26 paychecks / 12 per month).
BUDGET_MODES: dict[str, str] = {
    "two_paycheck": "Two-paycheck month",
    "average": "Average month",
}
_TWO_PAYCHECK_PER_MONTH = {"weekly": 4, "biweekly": 2, "semimonthly": 2, "monthly": 1}


def budget_mode(conn: sqlite3.Connection) -> str:
    mode = settings.get(conn, "budget_mode")
    return mode if mode in BUDGET_MODES else "average"


def monthly_equivalent(amount_cents: int, frequency: str, mode: str = "average") -> int:
    if mode == "two_paycheck" and frequency in _TWO_PAYCHECK_PER_MONTH:
        return amount_cents * _TWO_PAYCHECK_PER_MONTH[frequency]
    per_year = FREQUENCIES[frequency][1]
    return round_cents(Decimal(amount_cents) * per_year / 12)


def extra_per_year_cents(amount_cents: int, frequency: str, mode: str) -> int:
    """Occurrences a year that the monthly plan doesn't count (two-paycheck mode only):
    2 extra bi-weekly paychecks or bills, 4 extra weeks of weekly bills."""
    if mode != "two_paycheck" or frequency not in _TWO_PAYCHECK_PER_MONTH:
        return 0
    counted = 12 * _TWO_PAYCHECK_PER_MONTH[frequency]
    return amount_cents * (FREQUENCIES[frequency][1] - counted)


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    kind: str
    sort_order: int
    essential: bool = False  # counts toward the emergency-fund target


@dataclass(frozen=True)
class LineItem:
    id: int
    category_id: int
    name: str
    amount_cents: int
    frequency: str
    notes: str
    mode: str = "average"

    @property
    def monthly_cents(self) -> int:
        return monthly_equivalent(self.amount_cents, self.frequency, self.mode)

    @property
    def extra_per_year_cents(self) -> int:
        return extra_per_year_cents(self.amount_cents, self.frequency, self.mode)


@dataclass
class Group:
    category: Category
    items: list[LineItem] = field(default_factory=list)

    @property
    def monthly_cents(self) -> int:
        return sum(item.monthly_cents for item in self.items)


@dataclass(frozen=True)
class Summary:
    income_cents: int
    expense_cents: int
    savings_cents: int
    debt_cents: int
    # Two-paycheck mode: what the 3-paycheck months bring in, and what the extra weeks
    # of weekly / bi-weekly bills cost, per year (outside the monthly plan).
    extra_income_per_year_cents: int = 0
    extra_outflow_per_year_cents: int = 0

    @property
    def windfall_per_year_cents(self) -> int:
        return self.extra_income_per_year_cents - self.extra_outflow_per_year_cents

    @property
    def outflow_cents(self) -> int:
        return self.expense_cents + self.savings_cents + self.debt_cents

    @property
    def unallocated_cents(self) -> int:
        """Zero-based budgeting target: every dollar of income is assigned."""
        return self.income_cents - self.outflow_cents

    @property
    def savings_rate(self) -> Decimal | None:
        if self.income_cents <= 0:
            return None
        return Decimal(self.savings_cents) / Decimal(self.income_cents)


# ---------------------------------------------------------------- queries
def list_categories(conn: sqlite3.Connection) -> list[Category]:
    rows = conn.execute(
        "SELECT id, name, kind, sort_order, essential FROM categories ORDER BY sort_order, id"
    )
    return [Category(**(dict(row) | {"essential": bool(row["essential"])})) for row in rows]


def set_essential(conn: sqlite3.Connection, category_id: int, essential: bool) -> None:
    cur = conn.execute(
        "UPDATE categories SET essential = ? WHERE id = ?", (int(essential), category_id)
    )
    if cur.rowcount == 0:
        raise LookupError(f"Category {category_id} not found.")


def grouped(conn: sqlite3.Connection) -> list[Group]:
    mode = budget_mode(conn)
    groups = {c.id: Group(c) for c in list_categories(conn)}
    rows = conn.execute(
        "SELECT id, category_id, name, amount_cents, frequency, notes "
        "FROM line_items ORDER BY sort_order, id"
    )
    for row in rows:
        groups[row["category_id"]].items.append(LineItem(**dict(row), mode=mode))
    return list(groups.values())


def summarize(groups: list[Group]) -> Summary:
    totals = dict.fromkeys(KINDS, 0)
    extra_in = extra_out = 0
    for group in groups:
        totals[group.category.kind] += group.monthly_cents
        extra = sum(item.extra_per_year_cents for item in group.items)
        if group.category.kind == "income":
            extra_in += extra
        else:
            extra_out += extra
    return Summary(
        income_cents=totals["income"],
        expense_cents=totals["expense"],
        savings_cents=totals["savings"],
        debt_cents=totals["debt"],
        extra_income_per_year_cents=extra_in,
        extra_outflow_per_year_cents=extra_out,
    )


# ---------------------------------------------------------------- line items
def add_line_item(
    conn: sqlite3.Connection,
    *,
    category_id: int,
    name: str,
    amount_cents: int,
    frequency: str = "monthly",
    notes: str = "",
) -> int:
    name, notes = _clean_item(name, amount_cents, frequency, notes)
    _require_category(conn, category_id)
    next_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM line_items WHERE category_id = ?",
        (category_id,),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO line_items (category_id, name, amount_cents, frequency, notes, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (category_id, name, amount_cents, frequency, notes, next_order),
    )
    return cur.lastrowid


def update_line_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    category_id: int,
    name: str,
    amount_cents: int,
    frequency: str,
    notes: str = "",
) -> None:
    name, notes = _clean_item(name, amount_cents, frequency, notes)
    _require_category(conn, category_id)
    cur = conn.execute(
        "UPDATE line_items SET category_id = ?, name = ?, amount_cents = ?, frequency = ?, "
        "notes = ?, updated_at = datetime('now') WHERE id = ?",
        (category_id, name, amount_cents, frequency, notes, item_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"Line item {item_id} not found.")


def delete_line_item(conn: sqlite3.Connection, item_id: int) -> None:
    if conn.execute("DELETE FROM line_items WHERE id = ?", (item_id,)).rowcount == 0:
        raise LookupError(f"Line item {item_id} not found.")


# ---------------------------------------------------------------- categories
def add_starter_lines(conn: sqlite3.Connection) -> int:
    """Add STARTER_LINES at $0 a month, skipping any a category already has. Returns how many."""
    categories = {c.name: c.id for c in list_categories(conn)}
    added = 0
    for category_name, line_name in STARTER_LINES:
        category_id = categories.get(category_name)
        if category_id is None:
            continue
        exists = conn.execute(
            "SELECT 1 FROM line_items WHERE category_id = ? AND name = ?", (category_id, line_name)
        ).fetchone()
        if not exists:
            add_line_item(conn, category_id=category_id, name=line_name, amount_cents=0)
            added += 1
    return added


def add_category(conn: sqlite3.Connection, *, name: str, kind: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Category name is required.")
    if len(name) > 60:
        raise ValueError("Category name is too long.")
    if kind not in KINDS:
        raise ValueError("Unknown category type.")
    next_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM categories"
    ).fetchone()[0]
    try:
        cur = conn.execute(
            "INSERT INTO categories (name, kind, sort_order) VALUES (?, ?, ?)",
            (name, kind, next_order),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"A category named '{name}' already exists.") from None
    return cur.lastrowid


def delete_category(conn: sqlite3.Connection, category_id: int) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM line_items WHERE category_id = ?", (category_id,)
    ).fetchone()[0]
    if count:
        raise ValueError("Move or delete this category's line items first.")
    if conn.execute("DELETE FROM categories WHERE id = ?", (category_id,)).rowcount == 0:
        raise LookupError(f"Category {category_id} not found.")


# ---------------------------------------------------------------- helpers
def _clean_item(name: str, amount_cents: int, frequency: str, notes: str) -> tuple[str, str]:
    name = (name or "").strip()
    notes = (notes or "").strip()
    if not name:
        raise ValueError("Name is required.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Name must be {MAX_NAME} characters or fewer.")
    if len(notes) > MAX_NOTES:
        raise ValueError(f"Notes must be {MAX_NOTES} characters or fewer.")
    if amount_cents < 0:
        raise ValueError("Amount can't be negative.")
    if frequency not in FREQUENCIES:
        raise ValueError("Unknown frequency.")
    return name, notes


def _require_category(conn: sqlite3.Connection, category_id: int) -> None:
    if conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone() is None:
        raise ValueError("Unknown category.")
