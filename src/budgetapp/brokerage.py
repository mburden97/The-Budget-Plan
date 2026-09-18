"""Brokerage, retirement and crypto holdings: valuation, allocation, price updates.

An investment account's balance is derived from its holdings (shares x latest price)
and written as a balance snapshot, so net worth picks it up automatically.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from budgetapp import networth, settings
from budgetapp.money import round_cents
from budgetapp.quotes import DividendResult, QuoteRequest, RefreshResult

ASSET_CLASSES: dict[str, str] = {
    "us_stock": "US stocks",
    "intl_stock": "International stocks",
    "bond": "Bonds",
    "cash": "Cash / money market",
    "real_estate": "Real estate",
    "crypto": "Crypto",
    "other": "Other",
}
QUOTE_SOURCES: dict[str, str] = {
    "market": "Stock / ETF / fund",
    "crypto": "Crypto",
    "manual": "Manual price",
}
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-^=/]{0,19}")


@dataclass(frozen=True)
class Holding:
    id: int
    account_id: int
    account_name: str
    symbol: str
    name: str
    asset_class: str
    shares: Decimal
    cost_basis_cents: int | None
    quote_source: str
    quote_id: str
    last_price: Decimal | None
    price_as_of: str | None
    dividend_yield: Decimal | None = None  # a year's dividends as a fraction of value
    reinvest: bool = True  # dividends buy more shares (DRIP) rather than paying out cash
    dividend_source: str = "manual"  # "manual", or the provider a refresh got the yield from
    dividend_as_of: str | None = None  # ISO date of that refresh

    @property
    def value_cents(self) -> int | None:
        if self.last_price is None:
            return None
        return round_cents(self.shares * self.last_price * 100)

    @property
    def annual_dividend_cents(self) -> int | None:
        value = self.value_cents
        if value is None or self.dividend_yield is None:
            return None
        return round_cents(Decimal(value) * self.dividend_yield)

    @property
    def gain_cents(self) -> int | None:
        value = self.value_cents
        if value is None or self.cost_basis_cents is None:
            return None
        return value - self.cost_basis_cents

    def quote_request(self) -> QuoteRequest | None:
        if self.quote_source == "manual":
            return None
        return QuoteRequest(self.quote_source, self.symbol, self.quote_id)


@dataclass(frozen=True)
class AllocationRow:
    asset_class: str
    label: str
    value_cents: int
    share: Decimal  # fraction of total
    target: Decimal | None

    @property
    def drift(self) -> Decimal | None:
        return None if self.target is None else self.share - self.target


def parse_quantity(text: str, label: str = "Shares", *, places: int = 10) -> Decimal:
    cleaned = (text or "").strip().replace(",", "").replace("$", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be zero or more.")
    if value.as_tuple().exponent < -places:
        raise ValueError(f"{label}: use at most {places} decimal places.")
    if value >= Decimal(10) ** 12:
        raise ValueError(f"{label} is too large.")
    return value


@dataclass(frozen=True)
class Dividends:
    """A year of expected dividends across the holdings that have a yield set."""

    invested_cents: int  # value of all priced holdings
    reinvested_cents: int  # buys more shares
    paid_out_cents: int  # lands as cash

    @property
    def total_cents(self) -> int:
        return self.reinvested_cents + self.paid_out_cents

    @property
    def reinvest_yield(self) -> Decimal:
        """Reinvested dividends as a fraction of everything invested, so the projection can
        compound them alongside price growth."""
        return self._share(self.reinvested_cents)

    @property
    def payout_yield(self) -> Decimal:
        return self._share(self.paid_out_cents)

    def _share(self, part: int) -> Decimal:
        if self.invested_cents <= 0:
            return Decimal(0)
        return Decimal(part) / Decimal(self.invested_cents)


def dividends(holdings: list[Holding]) -> Dividends:
    priced = [h for h in holdings if h.value_cents is not None]
    paying = [(h, h.annual_dividend_cents or 0) for h in priced if h.dividend_yield]
    return Dividends(
        invested_cents=sum(h.value_cents for h in priced),
        reinvested_cents=sum(amount for h, amount in paying if h.reinvest),
        paid_out_cents=sum(amount for h, amount in paying if not h.reinvest),
    )


def _check_yield(dividend_yield: Decimal | None) -> None:
    if dividend_yield is not None and not (0 <= dividend_yield <= Decimal("0.25")):
        raise ValueError("Dividend yield must be between 0% and 25%.")


# ---------------------------------------------------------------- holdings
_HOLDING_SQL = """
SELECT h.id, h.account_id, a.name AS account_name, h.symbol, h.name, h.asset_class, h.shares,
       h.cost_basis_cents, h.quote_source, h.quote_id, h.last_price, h.price_as_of,
       h.dividend_yield, h.reinvest, h.dividend_source, h.dividend_as_of
FROM holdings h JOIN accounts a ON a.id = h.account_id
"""


def _holding(row: sqlite3.Row) -> Holding:
    data = dict(row)
    data["shares"] = Decimal(data["shares"])
    data["last_price"] = Decimal(data["last_price"]) if data["last_price"] else None
    data["dividend_yield"] = Decimal(data["dividend_yield"]) if data["dividend_yield"] else None
    data["reinvest"] = bool(data["reinvest"])
    return Holding(**data)


def list_holdings(conn: sqlite3.Connection) -> list[Holding]:
    rows = conn.execute(
        _HOLDING_SQL + " WHERE a.archived = 0 ORDER BY a.name COLLATE NOCASE, h.symbol"
    )
    return [_holding(row) for row in rows]


def get_holding(conn: sqlite3.Connection, holding_id: int) -> Holding:
    row = conn.execute(_HOLDING_SQL + " WHERE h.id = ?", (holding_id,)).fetchone()
    if row is None:
        raise LookupError(f"Holding {holding_id} not found.")
    return _holding(row)


def add_holding(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    symbol: str,
    shares: Decimal,
    asset_class: str = "other",
    name: str = "",
    cost_basis_cents: int | None = None,
    quote_source: str = "market",
    quote_id: str = "",
    price: Decimal | None = None,
    dividend_yield: Decimal | None = None,
    reinvest: bool = True,
) -> int:
    symbol, price = _validate(conn, account_id, symbol, asset_class, quote_source, price)
    _check_yield(dividend_yield)
    try:
        cur = conn.execute(
            "INSERT INTO holdings (account_id, symbol, name, asset_class, shares, "
            "cost_basis_cents, quote_source, quote_id, dividend_yield, reinvest, "
            "dividend_source, dividend_as_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                symbol,
                name.strip(),
                asset_class,
                str(shares),
                cost_basis_cents,
                quote_source,
                quote_id.strip(),
                str(dividend_yield) if dividend_yield is not None else None,
                int(reinvest),
                "manual",
                date.today().isoformat() if dividend_yield is not None else None,
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"{symbol} is already in this account; edit it instead.") from None
    if price is not None:
        _set_price(conn, cur.lastrowid, price, date.today())
    revalue_accounts(conn, {account_id})
    return cur.lastrowid


def update_holding(
    conn: sqlite3.Connection,
    holding_id: int,
    *,
    symbol: str,
    shares: Decimal,
    asset_class: str,
    name: str,
    cost_basis_cents: int | None,
    quote_source: str,
    quote_id: str,
    price: Decimal | None = None,
    dividend_yield: Decimal | None = None,
    reinvest: bool = True,
) -> None:
    current = get_holding(conn, holding_id)
    _check_yield(dividend_yield)
    yield_changed = dividend_yield != current.dividend_yield
    symbol, new_price = _validate(
        conn, current.account_id, symbol, asset_class, quote_source, price or current.last_price
    )
    quote_id = quote_id.strip()
    if symbol != current.symbol and quote_id == current.quote_id:
        quote_id = ""  # a resolved coin id belongs to the old symbol
    try:
        conn.execute(
            "UPDATE holdings SET symbol = ?, name = ?, asset_class = ?, shares = ?, "
            "cost_basis_cents = ?, quote_source = ?, quote_id = ?, dividend_yield = ?, "
            "reinvest = ?, dividend_source = ?, dividend_as_of = ? WHERE id = ?",
            (
                symbol,
                name.strip(),
                asset_class,
                str(shares),
                cost_basis_cents,
                quote_source,
                quote_id,
                str(dividend_yield) if dividend_yield is not None else None,
                int(reinvest),
                # Typing a different yield makes it yours; just saving the page doesn't.
                "manual" if yield_changed else current.dividend_source,
                date.today().isoformat() if yield_changed else current.dividend_as_of,
                holding_id,
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"{symbol} is already in this account.") from None
    if new_price is not None and new_price != current.last_price:
        _set_price(conn, holding_id, new_price, date.today())
    revalue_accounts(conn, {current.account_id})


def delete_holding(conn: sqlite3.Connection, holding_id: int) -> int:
    """Delete a holding; returns its account id."""
    holding = get_holding(conn, holding_id)
    conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
    revalue_accounts(conn, {holding.account_id})
    return holding.account_id


def _validate(
    conn: sqlite3.Connection,
    account_id: int,
    symbol: str,
    asset_class: str,
    quote_source: str,
    price: Decimal | None,
) -> tuple[str, Decimal | None]:
    try:
        account = networth.get_account(conn, account_id)
    except LookupError:
        raise ValueError("Choose an account.") from None
    if account.type not in networth.INVESTMENT_TYPES:
        raise ValueError("Holdings belong in a brokerage, retirement or crypto account.")
    symbol = (symbol or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("Symbol must be 1-20 letters or numbers, e.g. VTI, BRK-B, BTC.")
    if asset_class not in ASSET_CLASSES:
        raise ValueError("Unknown asset class.")
    if quote_source not in QUOTE_SOURCES:
        raise ValueError("Unknown price source.")
    if quote_source == "manual" and price is None:
        if asset_class != "cash":
            raise ValueError("Holdings with a manual price need a price.")
        price = Decimal(1)
    return symbol, price


def _set_price(conn: sqlite3.Connection, holding_id: int, price: Decimal, as_of: date) -> None:
    conn.execute(
        "UPDATE holdings SET last_price = ?, price_as_of = ? WHERE id = ?",
        (str(price), as_of.isoformat(), holding_id),
    )


# ---------------------------------------------------------------- prices
def quote_requests(conn: sqlite3.Connection) -> list[QuoteRequest]:
    """Unique market/crypto lookups needed to price every holding."""
    requests = (h.quote_request() for h in list_holdings(conn))
    return list(dict.fromkeys(r for r in requests if r is not None))


def dividend_requests(conn: sqlite3.Connection) -> list[QuoteRequest]:
    """Unique lookups for holdings that might report a yield (crypto and manual ones can't)."""
    requests = (h.quote_request() for h in list_holdings(conn))
    return list(dict.fromkeys(r for r in requests if r is not None and r.source == "market"))


def apply_dividend_yields(
    conn: sqlite3.Connection,
    result: DividendResult,
    as_of: date,
    *,
    replace_manual: bool = False,
) -> tuple[int, int]:
    """Store fetched yields. Returns (updated, kept), kept being yields typed in by hand."""
    updated = kept = 0
    for holding in list_holdings(conn):
        req = holding.quote_request()
        if req is None or req not in result.yields:
            continue
        own = holding.dividend_source == "manual" and holding.dividend_yield is not None
        if own and not replace_manual:
            kept += 1
            continue
        conn.execute(
            "UPDATE holdings SET dividend_yield = ?, dividend_source = ?, dividend_as_of = ? "
            "WHERE id = ?",
            (str(result.yields[req]), result.sources.get(req, ""), as_of.isoformat(), holding.id),
        )
        updated += 1
    return updated, kept


def apply_prices(conn: sqlite3.Connection, result: RefreshResult, as_of: date) -> int:
    """Store fetched prices on holdings, keep price history, revalue accounts."""
    updated = 0
    for holding in list_holdings(conn):
        req = holding.quote_request()
        if req is None:
            continue
        if req in result.resolved and not holding.quote_id:
            conn.execute(
                "UPDATE holdings SET quote_id = ? WHERE id = ?", (result.resolved[req], holding.id)
            )
        if req in result.prices:
            _set_price(conn, holding.id, result.prices[req], as_of)
            updated += 1
    for req, price in result.prices.items():
        key = f"{req.source}:{result.resolved.get(req) or req.quote_id or req.symbol}"
        conn.execute(
            "INSERT INTO prices (symbol, as_of, price) VALUES (?, ?, ?) "
            "ON CONFLICT (symbol, as_of) DO UPDATE SET price = excluded.price",
            (key, as_of.isoformat(), str(price)),
        )
    revalue_accounts(conn, as_of=as_of)
    return updated


def revalue_accounts(
    conn: sqlite3.Connection, account_ids: set[int] | None = None, *, as_of: date | None = None
) -> None:
    """Write each investment account's holdings value as its balance."""
    totals: dict[int, int] = {}
    for holding in list_holdings(conn):
        if account_ids is None or holding.account_id in account_ids:
            totals[holding.account_id] = totals.get(holding.account_id, 0) + (
                holding.value_cents or 0
            )
    for account_id, cents in totals.items():
        networth.record_balance(conn, account_id, cents, as_of)


# ---------------------------------------------------------------- allocation
def get_targets(conn: sqlite3.Connection) -> dict[str, Decimal]:
    targets = {}
    for key, value in settings.get_json(conn, "allocation_targets", {}).items():
        try:
            if key in ASSET_CLASSES:
                targets[key] = Decimal(str(value))
        except InvalidOperation:
            continue
    return targets


def set_targets(conn: sqlite3.Connection, targets: dict[str, Decimal]) -> None:
    """Targets are fractions per asset class and must total 100% (or be empty to clear)."""
    targets = {k: v for k, v in targets.items() if v}
    for key, value in targets.items():
        if key not in ASSET_CLASSES:
            raise ValueError("Unknown asset class.")
        if not Decimal(0) < value <= Decimal(1):
            raise ValueError("Each target must be between 0% and 100%.")
    total = sum(targets.values(), Decimal(0))
    if targets and total != 1:
        raise ValueError(f"Targets add up to {total * 100:.1f}%; they need to total 100%.")
    settings.put_json(conn, "allocation_targets", {k: str(v) for k, v in targets.items()})


def allocation(holdings: list[Holding], targets: dict[str, Decimal]) -> list[AllocationRow]:
    values: dict[str, int] = {}
    for holding in holdings:
        if holding.value_cents:
            values[holding.asset_class] = values.get(holding.asset_class, 0) + holding.value_cents
    total = sum(values.values())
    rows = []
    for key, label in ASSET_CLASSES.items():
        value = values.get(key, 0)
        if not value and key not in targets:
            continue
        share = Decimal(value) / Decimal(total) if total else Decimal(0)
        rows.append(AllocationRow(key, label, value, share, targets.get(key)))
    return rows
