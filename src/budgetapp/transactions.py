"""Actual transactions: categorization rules and budget vs. actual.

Sign convention: money in > 0, money out < 0. A transaction is either assigned to a
budget line item, marked excluded (transfers between your own accounts, card
payments), or uncategorized.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from budgetapp import planning
from budgetapp.dates import add_months, month_start

MAX_DESCRIPTION = 200


@dataclass(frozen=True)
class Transaction:
    id: int
    posted_on: str
    description: str
    amount_cents: int
    account_id: int | None
    account_name: str | None
    line_item_id: int | None
    line_item_name: str | None
    category_name: str | None
    excluded: bool
    notes: str
    bank_category: str | None = None


@dataclass(frozen=True)
class Rule:
    id: int
    pattern: str
    line_item_id: int | None
    line_item_name: str | None
    excluded: bool
    split_line_item_id: int | None = None
    split_line_item_name: str | None = None
    split_amount_cents: int | None = None
    split_note: str = ""


@dataclass(frozen=True)
class Split:
    id: int
    transaction_id: int
    line_item_id: int
    line_item_name: str
    amount_cents: int  # same sign as the transaction
    note: str


@dataclass(frozen=True)
class MonthTotals:
    inflow_cents: int
    outflow_cents: int
    uncategorized_count: int


_TXN_SQL = """
SELECT t.id, t.posted_on, t.description, t.amount_cents, t.account_id, a.name AS account_name,
       t.line_item_id, li.name AS line_item_name, c.name AS category_name, t.excluded, t.notes,
       t.bank_category
FROM transactions t
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN line_items li ON li.id = t.line_item_id
LEFT JOIN categories c ON c.id = li.category_id
"""


def _txn(row: sqlite3.Row) -> Transaction:
    return Transaction(**(dict(row) | {"excluded": bool(row["excluded"])}))


def month_range(month: date) -> tuple[str, str]:
    """ISO bounds [start, end) of the month containing `month`."""
    start = month_start(month)
    return start.isoformat(), add_months(start, 1).isoformat()


# ---------------------------------------------------------------- transactions
def list_transactions(
    conn: sqlite3.Connection, month: date, *, only_uncategorized: bool = False
) -> list[Transaction]:
    sql = _TXN_SQL + " WHERE t.posted_on >= ? AND t.posted_on < ?"
    if only_uncategorized:
        sql += " AND t.line_item_id IS NULL AND t.excluded = 0"
    sql += " ORDER BY t.posted_on DESC, t.id DESC"
    return [_txn(row) for row in conn.execute(sql, month_range(month))]


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> Transaction:
    row = conn.execute(_TXN_SQL + " WHERE t.id = ?", (txn_id,)).fetchone()
    if row is None:
        raise LookupError(f"Transaction {txn_id} not found.")
    return _txn(row)


def add_transaction(
    conn: sqlite3.Connection,
    *,
    posted_on: date,
    description: str,
    amount_cents: int,
    account_id: int | None = None,
    line_item_id: int | None = None,
    excluded: bool = False,
    notes: str = "",
    import_hash: str | None = None,
    bank_category: str | None = None,
) -> int | None:
    """Insert a transaction. Returns None if `import_hash` was already imported."""
    description = " ".join((description or "").split())[:MAX_DESCRIPTION]
    if not description:
        raise ValueError("Description is required.")
    if excluded:
        line_item_id = None
    if line_item_id is not None:
        _require_line_item(conn, line_item_id)
    if account_id is not None and not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
    ).fetchone():
        raise ValueError("Unknown account.")
    cur = conn.execute(
        "INSERT INTO transactions (posted_on, description, amount_cents, account_id, "
        "line_item_id, excluded, notes, import_hash, bank_category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (import_hash) DO NOTHING",
        (
            posted_on.isoformat(),
            description,
            amount_cents,
            account_id,
            line_item_id,
            int(excluded),
            notes.strip(),
            import_hash,
            (bank_category or "").strip()[:60] or None,
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def categorize(
    conn: sqlite3.Connection,
    txn_id: int,
    *,
    line_item_id: int | None,
    excluded: bool = False,
    remember_pattern: str | None = None,
) -> int:
    """Assign a transaction. With `remember_pattern`, also save a rule and apply it to
    other uncategorized transactions; returns how many others it categorized."""
    get_transaction(conn, txn_id)
    if excluded:
        line_item_id = None
    elif line_item_id is not None:
        _require_line_item(conn, line_item_id)
    conn.execute(
        "UPDATE transactions SET line_item_id = ?, excluded = ? WHERE id = ?",
        (line_item_id, int(excluded), txn_id),
    )
    if remember_pattern and (line_item_id is not None or excluded):
        add_rule(conn, pattern=remember_pattern, line_item_id=line_item_id, excluded=excluded)
        return apply_rules(conn)
    return 0


def delete_transaction(conn: sqlite3.Connection, txn_id: int) -> None:
    if conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,)).rowcount == 0:
        raise LookupError(f"Transaction {txn_id} not found.")


def month_totals(conn: sqlite3.Connection, month: date) -> MonthTotals:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS inflow,
               COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS outflow,
               COUNT(CASE WHEN line_item_id IS NULL THEN 1 END) AS uncategorized
        FROM transactions
        WHERE posted_on >= ? AND posted_on < ? AND excluded = 0
        """,
        month_range(month),
    ).fetchone()
    return MonthTotals(row["inflow"], row["outflow"], row["uncategorized"])


# ---------------------------------------------------------------- rules
def list_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute(
        "SELECT r.id, r.pattern, r.line_item_id, li.name AS line_item_name, r.excluded, "
        "r.split_line_item_id, sl.name AS split_line_item_name, r.split_amount_cents, "
        "r.split_note FROM category_rules r "
        "LEFT JOIN line_items li ON li.id = r.line_item_id "
        "LEFT JOIN line_items sl ON sl.id = r.split_line_item_id "
        "ORDER BY r.pattern COLLATE NOCASE"
    )
    return [Rule(**(dict(row) | {"excluded": bool(row["excluded"])})) for row in rows]


def add_rule(
    conn: sqlite3.Connection,
    *,
    pattern: str,
    line_item_id: int | None = None,
    excluded: bool = False,
    split_line_item_id: int | None = None,
    split_amount_cents: int | None = None,
    split_note: str = "",
) -> int:
    """"Description contains `pattern`" -> line item (or excluded). Replaces a same-text rule.

    Optionally carve `split_amount_cents` of every matching charge into another line.
    """
    pattern = " ".join((pattern or "").split())
    if len(pattern) < 3:
        raise ValueError("Rule text must be at least 3 characters.")
    if len(pattern) > 100:
        raise ValueError("Rule text must be 100 characters or fewer.")
    if excluded:
        line_item_id = None
    elif line_item_id is None:
        raise ValueError("Choose a budget line for the rule.")
    else:
        _require_line_item(conn, line_item_id)
    if split_line_item_id is not None or split_amount_cents is not None:
        if excluded or split_line_item_id is None or not split_amount_cents:
            raise ValueError("A split needs a budget line and an amount above $0.")
        _require_line_item(conn, split_line_item_id)
        split_amount_cents = abs(split_amount_cents)
    conn.execute("DELETE FROM category_rules WHERE pattern = ?", (pattern,))
    cur = conn.execute(
        "INSERT INTO category_rules (pattern, line_item_id, excluded, split_line_item_id, "
        "split_amount_cents, split_note) VALUES (?, ?, ?, ?, ?, ?)",
        (pattern, line_item_id, int(excluded), split_line_item_id, split_amount_cents,
         (split_note or "").strip()[:60]),
    )
    return cur.lastrowid


def apply_rule_everywhere(conn: sqlite3.Connection, rule_id: int) -> int:
    """File every matching transaction (categorized or not, transfers excepted) by this rule,
    including its split. Returns how many transactions matched."""
    rule = next((r for r in list_rules(conn) if r.id == rule_id), None)
    if rule is None:
        raise LookupError(f"Rule {rule_id} not found.")
    rows = conn.execute(
        "SELECT id, amount_cents FROM transactions "
        "WHERE excluded = 0 AND instr(lower(description), lower(?)) > 0",
        (rule.pattern,),
    ).fetchall()
    for row in rows:
        if not rule.excluded:
            conn.execute(
                "UPDATE transactions SET line_item_id = ? WHERE id = ?",
                (rule.line_item_id, row["id"]),
            )
        _ensure_rule_split(conn, row["id"], row["amount_cents"], rule)
    return len(rows)


def _ensure_rule_split(
    conn: sqlite3.Connection, txn_id: int, amount_cents: int, rule: Rule
) -> None:
    if rule.split_line_item_id is None or not rule.split_amount_cents or not amount_cents:
        return
    exists = conn.execute(
        "SELECT 1 FROM transaction_splits WHERE transaction_id = ? AND line_item_id = ? "
        "AND note = ?",
        (txn_id, rule.split_line_item_id, rule.split_note),
    ).fetchone()
    if exists:
        return
    magnitude = min(rule.split_amount_cents, abs(amount_cents))
    conn.execute(
        "INSERT INTO transaction_splits (transaction_id, line_item_id, amount_cents, note) "
        "VALUES (?, ?, ?, ?)",
        (txn_id, rule.split_line_item_id, magnitude if amount_cents > 0 else -magnitude,
         rule.split_note),
    )


# ---------------------------------------------------------------- splits
def list_splits(conn: sqlite3.Connection, txn_id: int) -> list[Split]:
    return list(splits_by_transaction(conn, [txn_id]).get(txn_id, []))


def splits_by_transaction(
    conn: sqlite3.Connection, txn_ids: list[int]
) -> dict[int, list[Split]]:
    if not txn_ids:
        return {}
    marks = ",".join("?" * len(txn_ids))
    rows = conn.execute(
        "SELECT s.id, s.transaction_id, s.line_item_id, li.name AS line_item_name, "
        "s.amount_cents, s.note FROM transaction_splits s "
        f"JOIN line_items li ON li.id = s.line_item_id WHERE s.transaction_id IN ({marks}) "
        "ORDER BY s.id",
        tuple(txn_ids),
    )
    found: dict[int, list[Split]] = {}
    for row in rows:
        found.setdefault(row["transaction_id"], []).append(Split(**dict(row)))
    return found


def add_split(
    conn: sqlite3.Connection, txn_id: int, *, line_item_id: int, amount_cents: int, note: str = ""
) -> int:
    """Move part of a transaction to another budget line."""
    txn = get_transaction(conn, txn_id)
    if txn.excluded:
        raise ValueError("Transactions marked not budgeted can't be split.")
    _require_line_item(conn, line_item_id)
    magnitude = abs(amount_cents)
    if magnitude == 0:
        raise ValueError("Split amount must be more than $0.")
    already = sum(abs(s.amount_cents) for s in list_splits(conn, txn_id))
    if already + magnitude > abs(txn.amount_cents):
        raise ValueError("Splits can't add up to more than the transaction.")
    cur = conn.execute(
        "INSERT INTO transaction_splits (transaction_id, line_item_id, amount_cents, note) "
        "VALUES (?, ?, ?, ?)",
        (txn_id, line_item_id, magnitude if txn.amount_cents > 0 else -magnitude,
         (note or "").strip()[:60]),
    )
    return cur.lastrowid


def delete_split(conn: sqlite3.Connection, split_id: int) -> int:
    """Remove a split; returns its transaction id."""
    row = conn.execute(
        "SELECT transaction_id FROM transaction_splits WHERE id = ?", (split_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Split {split_id} not found.")
    conn.execute("DELETE FROM transaction_splits WHERE id = ?", (split_id,))
    return row["transaction_id"]


def recategorize_matching(conn: sqlite3.Connection, pattern: str, line_item_id: int) -> int:
    """Save a rule for `pattern` and move every transaction whose description contains it,
    categorized or not, to that line. Transfers marked not budgeted are left alone."""
    add_rule(conn, pattern=pattern, line_item_id=line_item_id)
    cur = conn.execute(
        "UPDATE transactions SET line_item_id = ? "
        "WHERE excluded = 0 AND instr(lower(description), lower(?)) > 0",
        (line_item_id, " ".join(pattern.split())),
    )
    return cur.rowcount


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    if conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,)).rowcount == 0:
        raise LookupError(f"Rule {rule_id} not found.")


def match(rules: list[Rule], description: str) -> Rule | None:
    """Case-insensitive "contains"; the longest (most specific) matching rule wins."""
    text = description.lower()
    hits = [rule for rule in rules if rule.pattern.lower() in text]
    return max(hits, key=lambda rule: len(rule.pattern)) if hits else None


def apply_rules(conn: sqlite3.Connection, txn_ids: list[int] | None = None) -> int:
    """Categorize uncategorized transactions (optionally only `txn_ids`) using rules."""
    rules = list_rules(conn)
    if not rules:
        return 0
    wanted = set(txn_ids) if txn_ids is not None else None
    count = 0
    for row in conn.execute(
        "SELECT id, description, amount_cents FROM transactions "
        "WHERE line_item_id IS NULL AND excluded = 0"
    ).fetchall():
        if wanted is not None and row["id"] not in wanted:
            continue
        rule = match(rules, row["description"])
        if rule:
            conn.execute(
                "UPDATE transactions SET line_item_id = ?, excluded = ? WHERE id = ?",
                (rule.line_item_id, int(rule.excluded), row["id"]),
            )
            _ensure_rule_split(conn, row["id"], row["amount_cents"], rule)
            count += 1
    return count


def suggest_pattern(description: str) -> str:
    """A rule suggestion: leading words up to the first one containing digits (store #s)."""
    words = []
    for word in description.split():
        if word.startswith("#") or any(ch.isdigit() for ch in word):
            break
        words.append(word)
        if len(words) == 3:
            break
    return " ".join(words) or description[:30]


# ---------------------------------------------------------------- bank categories
@dataclass(frozen=True)
class BankCategory:
    name: str
    total: int
    uncategorized: int
    line_item_id: int | None
    excluded: bool

    @property
    def mapped(self) -> bool:
        return self.line_item_id is not None or self.excluded


def bank_categories(conn: sqlite3.Connection) -> list[BankCategory]:
    """Every bank category seen on imported transactions, busiest first, with its mapping."""
    rows = conn.execute(
        """
        SELECT t.bank_category AS name, COUNT(*) AS total,
               SUM(t.line_item_id IS NULL AND t.excluded = 0) AS uncategorized,
               m.line_item_id, COALESCE(m.excluded, 0) AS excluded
        FROM transactions t
        LEFT JOIN bank_category_map m ON m.bank_category = t.bank_category
        WHERE t.bank_category IS NOT NULL AND t.bank_category != ''
        GROUP BY t.bank_category COLLATE NOCASE
        ORDER BY total DESC, name
        """
    )
    return [BankCategory(**(dict(row) | {"excluded": bool(row["excluded"])})) for row in rows]


def set_bank_category_map(
    conn: sqlite3.Connection, choices: dict[str, tuple[int | None, bool] | None]
) -> int:
    """Save bank category -> (line_item_id, excluded); None removes a mapping.
    Returns how many categories are mapped afterwards."""
    for name, choice in choices.items():
        conn.execute("DELETE FROM bank_category_map WHERE bank_category = ?", (name,))
        if choice is None:
            continue
        line_item_id, excluded = choice
        if excluded:
            line_item_id = None
        elif line_item_id is None:
            continue
        else:
            _require_line_item(conn, line_item_id)
        conn.execute(
            "INSERT INTO bank_category_map (bank_category, line_item_id, excluded) "
            "VALUES (?, ?, ?)",
            (name, line_item_id, int(excluded)),
        )
    return conn.execute("SELECT COUNT(*) FROM bank_category_map").fetchone()[0]


def apply_bank_categories(conn: sqlite3.Connection) -> int:
    """Categorize uncategorized transactions from their bank category's mapping.
    Description rules run first on import because they are more specific."""
    cur = conn.execute(
        """
        UPDATE transactions SET line_item_id = m.line_item_id, excluded = m.excluded
        FROM bank_category_map m
        WHERE m.bank_category = transactions.bank_category
          AND transactions.line_item_id IS NULL AND transactions.excluded = 0
        """
    )
    return cur.rowcount


# Words a budget line's name might use for common bank categories (Chase, AmEx).
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "food & drink": ("dining", "restaurant", "food", "eating"),
    "groceries": ("grocer",),
    "gas": ("fuel", "gas"),  # Chase "Gas" = gas stations; try Fuel before utility "Gas"
    "fuel": ("fuel", "gas"),
    "restaurant": ("dining", "restaurant", "food", "eating"),
    "bar & café": ("dining", "restaurant", "food", "eating"),
    "wholesale stores": ("grocer",),
    "cable & internet comm": ("internet", "wifi", "cable", "phone"),
    "internet services": ("subscription", "membership"),
    "insurance services": ("insurance",),
    "bills & utilities": ("utilit", "electric", "internet", "phone", "bill"),
    "entertainment": ("entertain", "streaming", "fun"),
    "shopping": ("shopping", "discretionary"),
    "travel": ("travel", "vacation", "trip"),
    "health & wellness": ("health", "medical", "gym", "fitness"),
    "automotive": ("car", "auto"),
    "personal": ("personal", "discretionary"),
    "home": ("home", "house"),
    "education": ("education", "school", "tuition"),
    "gifts & donations": ("gift", "donat", "charit"),
    "fees & adjustments": ("fee",),
}


# Too vague to match a budget line on their own.
_GENERIC = {"business", "services", "service", "merchandise", "supplies", "other", "general",
            "miscellaneous", "stores", "purchase", "adjustments"}


def suggest_line_item(bank_category: str, items: list[planning.LineItem]) -> int | None:
    """A likely budget line for a bank category, matched on words in the line's name.

    AmEx categories are "Group-Subgroup" ("Transportation-Fuel"); the subgroup is the
    useful part. Fallback words must be long and specific ("care" alone would match
    "Pet Care" for "Health Care Services").
    """
    key = bank_category.lower().strip()
    sub = key.split("-", 1)[-1].strip()
    needles = _SYNONYMS.get(key) or _SYNONYMS.get(sub) or tuple(
        w for w in re.findall(r"[a-z]+", sub) if len(w) > 4 and w not in _GENERIC
    )
    for needle in needles:
        # Short words must match whole words ("car" shouldn't match "Credit Card").
        pattern = rf"\b{re.escape(needle)}" + (r"\b" if len(needle) <= 3 else "")
        for item in items:
            if re.search(pattern, item.name.lower()):
                return item.id
    return None


# ---------------------------------------------------------------- budget vs. actual
@dataclass(frozen=True)
class ActualRow:
    item: planning.LineItem
    actual_cents: int  # income: money received; everything else: money spent / set aside

    @property
    def planned_cents(self) -> int:
        return self.item.monthly_cents

    @property
    def remaining_cents(self) -> int:
        return self.planned_cents - self.actual_cents


@dataclass(frozen=True)
class ActualGroup:
    category: planning.Category
    rows: list[ActualRow]

    @property
    def planned_cents(self) -> int:
        return sum(r.planned_cents for r in self.rows)

    @property
    def actual_cents(self) -> int:
        return sum(r.actual_cents for r in self.rows)

    @property
    def remaining_cents(self) -> int:
        return self.planned_cents - self.actual_cents


@dataclass(frozen=True)
class MonthActuals:
    month: date
    groups: list[ActualGroup]
    uncategorized_count: int
    uncategorized_in_cents: int
    uncategorized_out_cents: int

    def _sum(self, income: bool, attr: str) -> int:
        return sum(
            getattr(g, attr) for g in self.groups if (g.category.kind == "income") == income
        )

    @property
    def income_planned_cents(self) -> int:
        return self._sum(True, "planned_cents")

    @property
    def income_actual_cents(self) -> int:
        return self._sum(True, "actual_cents")

    @property
    def outflow_planned_cents(self) -> int:
        return self._sum(False, "planned_cents")

    @property
    def outflow_actual_cents(self) -> int:
        return self._sum(False, "actual_cents")


def budget_vs_actual(conn: sqlite3.Connection, month: date) -> MonthActuals:
    bounds = month_range(month)
    sums = {
        row["line_item_id"]: row["total"]
        for row in conn.execute(
            "SELECT line_item_id, SUM(amount_cents) AS total FROM allocations "
            "WHERE posted_on >= ? AND posted_on < ? AND excluded = 0 "
            "AND line_item_id IS NOT NULL GROUP BY line_item_id",
            bounds,
        )
    }
    loose = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS inflow,
               COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS outflow
        FROM allocations
        WHERE posted_on >= ? AND posted_on < ? AND excluded = 0 AND line_item_id IS NULL
          AND amount_cents != 0
        """,
        bounds,
    ).fetchone()
    groups = []
    for group in planning.grouped(conn):
        sign = 1 if group.category.kind == "income" else -1
        rows = [ActualRow(item, sign * sums.get(item.id, 0)) for item in group.items]
        groups.append(ActualGroup(group.category, rows))
    return MonthActuals(
        month_start(month), groups, loose["n"], loose["inflow"], loose["outflow"]
    )


def _require_line_item(conn: sqlite3.Connection, line_item_id: int) -> None:
    if conn.execute("SELECT 1 FROM line_items WHERE id = ?", (line_item_id,)).fetchone() is None:
        raise ValueError("Unknown budget line.")
