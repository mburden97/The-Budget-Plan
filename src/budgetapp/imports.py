"""Bank / card CSV import: parse, map columns, de-duplicate, auto-categorize.

The uploaded file is parsed in memory and never written to disk. Error messages
never echo cell contents (they can end up in the browser's session cookie).

Columns are found from the header words first, then from what the cells hold, so a file
with no header row still maps sensibly. Dates are read one way round for the whole file
(day-first only when the data proves it). OFX/QFX files arrive here too, via ofx.py, with a
transaction id column that makes re-imports exact.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime

from budgetapp import transactions
from budgetapp.money import parse_money

MAX_ROWS = 20_000
DELIMITERS = ",;\t|"
DATE_FORMATS: dict[str, str] = {
    "auto": "Detect from the file",
    "%m/%d/%Y": "MM/DD/YYYY",
    "%m/%d/%y": "MM/DD/YY",
    "%Y-%m-%d": "YYYY-MM-DD",
    "%d/%m/%Y": "DD/MM/YYYY",
    "%d/%m/%y": "DD/MM/YY",
    "%d.%m.%Y": "DD.MM.YYYY",
}
_AUTO_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%Y%m%d",
)
# The same, for a file whose dates put the day first (13/04/2026).
_DAY_FIRST_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%Y%m%d",
)
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}")
REF_PREFIX = "ref:"  # import hashes built from the bank's own transaction id


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class Mapping:
    date_col: int
    description_col: int
    amount_col: int | None = None  # signed amount; or use debit/credit columns
    debit_col: int | None = None
    credit_col: int | None = None
    date_format: str = "auto"
    flip_sign: bool = False  # card exports that list purchases as positive
    category_col: int | None = None  # the bank's own category (Chase, AmEx), optional
    id_col: int | None = None  # the bank's own id for each transaction (OFX FITID), optional

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Mapping | None:
        try:
            return cls(**json.loads(text))
        except (TypeError, ValueError):
            return None

    def fits(self, parsed: ParsedCsv) -> bool:
        cols = (
            self.date_col,
            self.description_col,
            self.amount_col,
            self.debit_col,
            self.credit_col,
            self.category_col,
            self.id_col,
        )
        return all(c is None or 0 <= c < len(parsed.headers) for c in cols)


@dataclass(frozen=True)
class ImportRow:
    posted_on: date
    description: str
    amount_cents: int
    bank_category: str = ""
    ref: str = ""  # the bank's id for the transaction; exact de-duplication when present


@dataclass(frozen=True)
class DateOrder:
    day_first: bool  # the file writes 13/04/2026
    ambiguous: bool  # every numeric date reads either way (no day above 12): taken month-first


@dataclass(frozen=True)
class ImportSummary:
    added: int
    duplicates: int
    categorized: int
    latest: date | None
    categories_filled: int = 0  # bank categories added to already-imported transactions
    ids: tuple[int, ...] = ()  # the transactions this import added


# ---------------------------------------------------------------- parsing
def parse_csv(data: bytes) -> ParsedCsv:
    if not data or not data.strip():
        raise ValueError("The file is empty.")
    text = _decode(data)
    reader = csv.reader(io.StringIO(text), delimiter=_detect_delimiter(text))
    try:
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    except csv.Error:
        raise ValueError("This file isn't a readable CSV.") from None
    if len(rows) > MAX_ROWS + 50:
        raise ValueError(f"Too many rows; import at most {MAX_ROWS:,} at a time.")
    while rows and sum(1 for cell in rows[0] if cell.strip()) < 2:  # account-info lines
        rows.pop(0)
    if not rows:
        raise ValueError("Couldn't find columns in this file. Is it a CSV export?")
    width = Counter(len(row) for row in rows).most_common(1)[0][0]
    start = _header_index(rows, width)
    if start is None:  # no header row: the whole file is data
        header, body = None, rows
    else:
        header, body = rows[start], rows[start + 1 :]
        # Some banks (e.g. Chase checking) end every data row with an extra comma, so
        # data rows can be one field wider than the header; keep the wider of the two.
        width = max(width, len(header))
    if not body:
        raise ValueError("The file has a header row but no transactions.")
    headers = [
        ((header[i].strip() if header and i < len(header) else "") or f"Column {i + 1}")
        for i in range(width)
    ]
    body = [[cell.strip() for cell in (row + [""] * width)[:width]] for row in body]
    return ParsedCsv(headers, body)


def guess_mapping(headers: list[str], rows: list[list[str]] | None = None) -> Mapping:
    """Columns from the header words; anything the headers don't name is judged by content."""
    lower = [h.lower() for h in headers]

    def find(*words: str, exclude: tuple[str, ...] = ()) -> int | None:
        for word in words:
            for i, header in enumerate(lower):
                if word in header and not any(x in header for x in exclude):
                    return i
        return None

    date_col = find("posting date", "posted", "transaction date", "trans date", "date")
    desc_col = find("description", "payee", "merchant", "name", "memo", "details", "narrative")
    amount_col = find("amount", exclude=("balance",))
    debit_col = find("debit", "withdrawal", "money out", exclude=("indicator",))
    credit_col = find("credit", "deposit", "money in", exclude=("indicator", "card"))
    category_col = find("category")
    if rows:
        named = {c for c in (date_col, desc_col, amount_col, debit_col, credit_col, category_col)
                 if c is not None}
        found_date, found_amount, found_desc = _infer_columns(rows[:100], len(headers), named)
        if date_col is None:
            date_col = found_date
        if amount_col is None and debit_col is None and credit_col is None:
            amount_col = found_amount
        if desc_col is None and found_desc not in (date_col, amount_col):
            desc_col = found_desc
    if amount_col is None and debit_col is None and credit_col is None:
        amount_col = len(headers) - 1
    return Mapping(
        date_col=0 if date_col is None else date_col,
        description_col=(min(1, len(headers) - 1) if desc_col is None else desc_col),
        amount_col=amount_col,
        debit_col=None if amount_col is not None else debit_col,
        credit_col=None if amount_col is not None else credit_col,
        category_col=category_col,
    )


def parse_date(text: str, fmt: str = "auto", *, day_first: bool = False) -> date:
    text = text.strip()
    if fmt == "auto" and len(text) > 10 and text[4:5] == "-":  # ISO timestamp
        text = text[:10]
    candidates = (_DAY_FIRST_FORMATS if day_first else _AUTO_FORMATS) if fmt == "auto" else (fmt,)
    for candidate in candidates:
        try:
            return datetime.strptime(text, candidate).date()
        except ValueError:
            continue
    raise ValueError("unrecognized date")


def date_order(values: list[str]) -> DateOrder:
    """Which way round a file writes its dates, judged from the whole column at once.

    Reading each date on its own would take 03/04 as March 4th in a file where 13/04 shows
    the day comes first. One answer for the whole file avoids that.
    """
    first_over_12 = second_over_12 = seen = False
    for value in values:
        match = _NUMERIC_DATE.match(value.strip())
        if match is None:
            continue
        seen = True
        first_over_12 |= int(match[1]) > 12
        second_over_12 |= int(match[2]) > 12
    if first_over_12 and second_over_12:
        raise ValueError(
            "Some dates in this file put the day first and some the month; "
            "choose the date format."
        )
    return DateOrder(first_over_12, seen and not (first_over_12 or second_over_12))


def parse_amount(text: str, label: str = "Amount") -> int:
    """An amount cell in cents. Besides what parse_money reads ($, commas, (12.30)), banks
    mark the direction with a suffix: "12.30 CR" is money in, "12.30 DR" and "12.30-" out."""
    cleaned = text.strip()
    upper = cleaned.upper()
    direction = 0
    if upper.endswith("CR"):
        cleaned, direction = cleaned[:-2], 1
    elif upper.endswith("DR"):
        cleaned, direction = cleaned[:-2], -1
    elif cleaned.endswith("-") and len(cleaned) > 1:
        cleaned, direction = cleaned[:-1], -1
    cents = parse_money(cleaned.strip(), allow_negative=True, label=label)
    return direction * abs(cents) if direction else cents


def build_rows(
    parsed: ParsedCsv, mapping: Mapping, *, day_first: bool | None = None
) -> tuple[list[ImportRow], list[str]]:
    """Convert CSV rows with a mapping. Returns (rows, per-row problems).

    With the "auto" date format the date order is worked out from all of `parsed` unless
    `day_first` says it (the preview passes the answer for the whole file with a sample).
    """
    if not mapping.fits(parsed):
        raise ValueError("Choose columns from this file.")
    if mapping.amount_col is None and mapping.debit_col is None and mapping.credit_col is None:
        raise ValueError("Choose an amount column, or debit and credit columns.")
    if mapping.date_format not in DATE_FORMATS:
        raise ValueError("Unknown date format.")
    if mapping.date_format != "auto":
        day_first = False
    elif day_first is None:
        day_first = date_order([cells[mapping.date_col] for cells in parsed.rows]).day_first
    rows: list[ImportRow] = []
    problems: list[str] = []
    for number, cells in enumerate(parsed.rows, start=1):
        if mapping.amount_col is not None and not cells[mapping.amount_col].strip():
            continue  # balance lines ("Beginning balance as of ...") carry no amount
        try:
            posted = parse_date(cells[mapping.date_col], mapping.date_format, day_first=day_first)
            if mapping.amount_col is not None:
                amount = parse_amount(cells[mapping.amount_col])
            else:
                debit = _money_cell(cells, mapping.debit_col, "Debit")
                credit = _money_cell(cells, mapping.credit_col, "Credit")
                amount = abs(credit) - abs(debit)
        except ValueError as exc:
            problems.append(f"row {number}: {exc}")
            continue
        if mapping.flip_sign:
            amount = -amount
        if amount == 0:
            continue  # $0 authorizations, blank lines
        description = " ".join(cells[mapping.description_col].split()) or "(no description)"
        category = cells[mapping.category_col].strip() if mapping.category_col is not None else ""
        ref = cells[mapping.id_col].strip()[:100] if mapping.id_col is not None else ""
        rows.append(ImportRow(posted, description, amount, category, ref))
    return rows, problems


def suggest_flip(rows: list[ImportRow]) -> bool:
    """True when most amounts are positive, as in card exports (e.g. American Express)
    that list purchases as positive numbers; the app records money out as negative."""
    return len(rows) >= 2 and sum(row.amount_cents > 0 for row in rows) * 2 > len(rows)


# ---------------------------------------------------------------- storing
def import_rows(
    conn: sqlite3.Connection, account_id: int, rows: list[ImportRow]
) -> ImportSummary:
    """Insert rows, skipping any already imported for this account; apply rules to new ones.

    A row with the bank's own id (OFX) is matched on that id. Otherwise identical rows in
    one file (two $4.50 coffees on the same day) are kept apart by their occurrence number,
    so re-importing an overlapping export is safe.

    The two kinds can't match each other's hash, so switching an account from CSV to OFX
    (or back) would import its history twice. A row that isn't found its own way is also
    matched against rows imported the other way on the same day for the same amount, each
    of those used up once.
    """
    seen: Counter = Counter()
    other_way = _imported_by_kind(conn, account_id)
    used: Counter = Counter()
    new_ids: list[int] = []
    duplicates = filled = 0
    for row in rows:
        if row.ref:
            digest = REF_PREFIX + hashlib.sha256(
                f"{account_id}|ref|{row.ref}".encode()
            ).hexdigest()
        else:
            key = (row.posted_on, row.amount_cents, row.description.lower())
            seen[key] += 1
            digest = hashlib.sha256(
                f"{account_id}|{row.posted_on}|{row.amount_cents}|{key[2]}|{seen[key]}".encode()
            ).hexdigest()
        known = conn.execute(
            "SELECT 1 FROM transactions WHERE import_hash = ?", (digest,)
        ).fetchone()
        if known is None:
            slot = (row.posted_on.isoformat(), row.amount_cents, not row.ref)
            if other_way[slot] > used[slot]:
                used[slot] += 1
                duplicates += 1
                continue
        txn_id = None if known else transactions.add_transaction(
            conn,
            posted_on=row.posted_on,
            description=row.description,
            amount_cents=row.amount_cents,
            account_id=account_id,
            import_hash=digest,
            bank_category=row.bank_category,
        )
        if txn_id is None:
            duplicates += 1
            if row.bank_category:  # re-importing with a category column fills it in
                filled += conn.execute(
                    "UPDATE transactions SET bank_category = ? WHERE import_hash = ? "
                    "AND (bank_category IS NULL OR bank_category = '')",
                    (row.bank_category[:60], digest),
                ).rowcount
        else:
            new_ids.append(txn_id)
    categorized = transactions.apply_rules(conn, new_ids) if new_ids else 0
    categorized += transactions.apply_bank_categories(conn)
    latest = max((row.posted_on for row in rows), default=None)
    return ImportSummary(len(new_ids), duplicates, categorized, latest, filled, tuple(new_ids))


def _imported_by_kind(conn: sqlite3.Connection, account_id: int) -> Counter:
    """Imported rows per (day, amount, matched by bank id?), counted before an import starts."""
    counts: Counter = Counter()
    for row in conn.execute(
        "SELECT posted_on, amount_cents, import_hash LIKE ? AS by_ref, COUNT(*) AS n "
        "FROM transactions WHERE account_id = ? AND import_hash IS NOT NULL "
        "GROUP BY posted_on, amount_cents, by_ref",
        (REF_PREFIX + "%", account_id),
    ):
        counts[(row["posted_on"], row["amount_cents"], bool(row["by_ref"]))] = row["n"]
    return counts


def get_profile(conn: sqlite3.Connection, account_id: int) -> Mapping | None:
    row = conn.execute(
        "SELECT mapping FROM import_profiles WHERE account_id = ?", (account_id,)
    ).fetchone()
    return Mapping.from_json(row["mapping"]) if row else None


def save_profile(conn: sqlite3.Connection, account_id: int, mapping: Mapping) -> None:
    conn.execute(
        "INSERT INTO import_profiles (account_id, mapping) VALUES (?, ?) "
        "ON CONFLICT (account_id) DO UPDATE SET mapping = excluded.mapping",
        (account_id, mapping.to_json()),
    )


# ---------------------------------------------------------------- helpers
def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _detect_delimiter(text: str) -> str:
    """The delimiter whose per-line count is most consistent across the first lines."""
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    best, best_lines = ",", 0
    for delimiter in DELIMITERS:
        counts = Counter(line.count(delimiter) for line in lines)
        counts.pop(0, None)
        if counts:
            _count, lines_with_count = counts.most_common(1)[0]
            if lines_with_count > best_lines:
                best, best_lines = delimiter, lines_with_count
    return best


def _header_index(rows: list[list[str]], width: int) -> int | None:
    """Index of the header row, or None if the file has none.

    A header is a non-data row about as wide as the table with data right below it.
    Banks can put a balance summary above the table (Bank of America); when several
    rows qualify, the widest wins, then the later one. Headers sit near the top.
    """
    best = None
    for i, row in enumerate(rows[:60]):
        if i + 1 >= len(rows):
            break
        if (
            len(row) >= width - 1
            and sum(1 for cell in row if cell.strip()) >= 2
            and not _looks_like_data(row)
            and _looks_like_data(rows[i + 1])
            and (best is None or len(row) >= len(rows[best]))
        ):
            best = i
    return best


def _looks_like_data(row: list[str]) -> bool:
    return any(_is_date(cell) or _is_amount(cell) for cell in row)


def _is_date(cell: str) -> bool:
    for day_first in (False, True):
        try:
            parse_date(cell, day_first=day_first)
            return True
        except ValueError:
            pass
    return False


def _is_amount(cell: str) -> bool:
    if not any(ch.isdigit() for ch in cell) or _is_date(cell):
        return False
    try:
        parse_amount(cell)
        return True
    except ValueError:
        return False


def _infer_columns(
    rows: list[list[str]], width: int, taken: set[int]
) -> tuple[int | None, int | None, int | None]:
    """(date, amount, description) columns judged by what the cells hold.

    For files whose headers say nothing useful, or that have none (a header-less export
    reads as Column 1, Column 2, ...). A column counts as dates or amounts when at least
    80% of its cells are; among amount columns, one with decimals and some negative values
    beats a running balance or a reference number. The description is the column with the
    most words in it.
    """
    if not rows:
        return None, None, None
    total = len(rows)

    def column(i: int) -> list[str]:
        return [row[i] for row in rows if i < len(row)]

    free = [i for i in range(width) if i not in taken]
    dates = {i: sum(map(_is_date, column(i))) / total for i in free}
    date_col = max((i for i in free if dates[i] >= 0.8), key=lambda i: (dates[i], -i),
                   default=None)

    def amount_rank(i: int) -> tuple:
        cells = [c for c in column(i) if _is_amount(c)]
        return (
            sum("." in c for c in cells) / total,  # 12.40, not a check or reference number
            any(parse_amount(c) < 0 for c in cells),  # money out, which a balance rarely is
            len(cells) / total,
            -i,
        )

    amounts = [i for i in free if i != date_col
               and sum(map(_is_amount, column(i))) / total >= 0.8]
    amount_col = max(amounts, key=amount_rank, default=None)

    def wordiness(i: int) -> float:
        return sum(len(c) for c in column(i) if any(ch.isalpha() for ch in c)) / total

    texts = [i for i in free if i not in (date_col, amount_col) and wordiness(i) > 0]
    desc_col = max(texts, key=lambda i: (wordiness(i), -i), default=None)
    return date_col, amount_col, desc_col


def _money_cell(cells: list[str], col: int | None, label: str) -> int:
    if col is None or not cells[col].strip():
        return 0
    return parse_amount(cells[col], label=label)
