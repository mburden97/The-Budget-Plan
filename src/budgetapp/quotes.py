"""Price quotes from free public APIs. Runs only when the user clicks "Refresh prices".

Privacy / safety:
  * Only ticker symbols and coin ids are sent. No amounts, shares or account names.
  * HTTPS only, to an allow-list of hosts (redirects are checked too).
  * API keys come from the encrypted vault and go only to their own provider.
  * Responses are size-capped and parsed strictly; prices are Decimal, never float.

Providers:
  market (stocks / ETFs / mutual funds): Finnhub (if key) -> Yahoo -> Alpha Vantage (if key)
  crypto: CoinGecko (key optional) -> Yahoo "<SYMBOL>-USD"
  dividend yields: Yahoo -> Finnhub (if key) -> Alpha Vantage (if key). Yahoo leads here because
  its dividend history covers ETFs and mutual funds, not just US stocks.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

ALLOWED_HOSTS = {
    "query1.finance.yahoo.com",
    "api.coingecko.com",
    "finnhub.io",
    "www.alphavantage.co",
}
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
USER_AGENT = "budgetapp/0.1"

# settings key -> (provider, signup page, what it adds)
PROVIDER_KEYS: dict[str, tuple[str, str, str]] = {
    "finnhub_api_key": (
        "Finnhub",
        "https://finnhub.io/register",
        "US stocks and ETFs; 60 calls/min free. Tried before Yahoo when set.",
    ),
    "alphavantage_api_key": (
        "Alpha Vantage",
        "https://www.alphavantage.co/support/#api-key",
        "Stocks and mutual funds; 25 calls/day free. Used if the others fail.",
    ),
    "coingecko_api_key": (
        "CoinGecko (demo key)",
        "https://www.coingecko.com/en/api/pricing",
        "Optional. Raises crypto rate limits; works without one.",
    ),
}

Fetch = Callable[[str, dict[str, str]], object]


class QuoteError(Exception):
    """A provider couldn't supply a price. The message is safe to show the user."""


@dataclass(frozen=True)
class QuoteRequest:
    source: str  # "market" or "crypto"
    symbol: str  # as the user entered it, e.g. "VTI", "BTC"
    quote_id: str = ""  # optional override: Yahoo symbol or CoinGecko coin id


@dataclass
class DividendResult:
    yields: dict[QuoteRequest, Decimal] = field(default_factory=dict)  # a fraction, e.g. 0.0325
    sources: dict[QuoteRequest, str] = field(default_factory=dict)
    errors: dict[QuoteRequest, str] = field(default_factory=dict)


@dataclass
class RefreshResult:
    prices: dict[QuoteRequest, Decimal] = field(default_factory=dict)
    sources: dict[QuoteRequest, str] = field(default_factory=dict)
    resolved: dict[QuoteRequest, str] = field(default_factory=dict)  # crypto symbol -> coin id
    errors: dict[QuoteRequest, str] = field(default_factory=dict)


def refresh(
    requests: list[QuoteRequest], *, keys: dict[str, str], fetch: Fetch | None = None
) -> RefreshResult:
    fetch = fetch or http_fetch
    result = RefreshResult()
    for req in requests:
        if req.source == "market":
            _market_quote(req, keys, fetch, result)
    _crypto_quotes([r for r in requests if r.source == "crypto"], keys, fetch, result)
    return result


# ---------------------------------------------------------------- HTTP
def check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise QuoteError(f"blocked request to {parts.hostname or url!r}")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_CheckedRedirect)


def http_fetch(url: str, headers: dict[str, str]) -> object:
    check_url(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QuoteError(f"network error ({getattr(exc, 'reason', exc)})") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise QuoteError("response too large")
    try:
        return json.loads(body, parse_float=Decimal)
    except ValueError:
        raise QuoteError("unreadable response") from None


# ---------------------------------------------------------------- market
def _market_quote(req: QuoteRequest, keys: dict[str, str], fetch: Fetch, result: RefreshResult):
    symbol = (req.quote_id or req.symbol).upper()
    providers: list[tuple[str, Callable[[], Decimal]]] = []
    if keys.get("finnhub_api_key"):
        providers.append(("Finnhub", lambda: _finnhub(symbol, keys["finnhub_api_key"], fetch)))
    providers.append(("Yahoo", lambda: _yahoo(symbol, fetch)))
    if keys.get("alphavantage_api_key"):
        providers.append(
            ("Alpha Vantage", lambda: _alphavantage(symbol, keys["alphavantage_api_key"], fetch))
        )
    failures = []
    for name, call in providers:
        try:
            result.prices[req] = call()
            result.sources[req] = name
            return
        except QuoteError as exc:
            failures.append(f"{name}: {exc}")
    result.errors[req] = "; ".join(failures)


def _yahoo(symbol: str, fetch: Fetch) -> Decimal:
    data = fetch(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol, safe='')}?range=1d&interval=1d",
        {},
    )
    try:
        return _price(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except (KeyError, IndexError, TypeError):
        raise QuoteError("symbol not found") from None


def _finnhub(symbol: str, key: str, fetch: Fetch) -> Decimal:
    # Key goes in a header so it never appears in a URL.
    data = fetch(
        f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(symbol, safe='')}",
        {"X-Finnhub-Token": key},
    )
    if not isinstance(data, dict) or not data.get("c"):  # Finnhub returns c=0 when unknown
        raise QuoteError("symbol not found")
    return _price(data["c"])


def _alphavantage(symbol: str, key: str, fetch: Fetch) -> Decimal:
    # Alpha Vantage only accepts the key as a query parameter (still encrypted by HTTPS).
    data = fetch(
        "https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
        f"&symbol={urllib.parse.quote(symbol, safe='')}&apikey={urllib.parse.quote(key, safe='')}",
        {},
    )
    if isinstance(data, dict) and ("Note" in data or "Information" in data):
        raise QuoteError("daily limit reached")
    try:
        return _price(data["Global Quote"]["05. price"])
    except (KeyError, TypeError):
        raise QuoteError("symbol not found") from None


# ---------------------------------------------------------------- dividends
MAX_PLAUSIBLE_YIELD = Decimal("0.25")  # above this, assume bad data and let the user type it


def dividend_yields(
    requests: list[QuoteRequest], *, keys: dict[str, str], fetch: Fetch | None = None
) -> DividendResult:
    """Trailing dividend yield per symbol. Crypto is skipped: coins don't pay dividends."""
    fetch = fetch or http_fetch
    result = DividendResult()
    for req in requests:
        if req.source != "market":
            continue
        try:
            value, source = _one_yield((req.quote_id or req.symbol).upper(), keys, fetch)
        except QuoteError as exc:
            result.errors[req] = str(exc)
            continue
        result.yields[req], result.sources[req] = value, source
    return result


def _one_yield(symbol: str, keys: dict[str, str], fetch: Fetch) -> tuple[Decimal, str]:
    providers: list[tuple[str, Callable[[], Decimal]]] = [
        ("Yahoo", lambda: _yahoo_dividends(symbol, fetch))
    ]
    if keys.get("finnhub_api_key"):
        providers.append(
            ("Finnhub", lambda: _finnhub_dividends(symbol, keys["finnhub_api_key"], fetch))
        )
    if keys.get("alphavantage_api_key"):
        providers.append((
            "Alpha Vantage",
            lambda: _alphavantage_dividends(symbol, keys["alphavantage_api_key"], fetch),
        ))
    failures = []
    for name, call in providers:
        try:
            return _checked_yield(call()), name
        except QuoteError as exc:
            failures.append(f"{name}: {exc}")
    raise QuoteError("; ".join(failures))


def _yahoo_dividends(symbol: str, fetch: Fetch) -> Decimal:
    """A year of payments divided by today's price, from the chart's dividend events.

    Real payments rather than a quoted figure, so it works for ETFs and funds too. Monthly
    bars keep the response small; the events come back either way.
    """
    data = fetch(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol, safe='')}?range=1y&interval=1mo&events=div%2Csplit",
        {},
    )
    try:
        chart = data["chart"]["result"][0]
        price = _price(chart["meta"]["regularMarketPrice"])
        events = chart.get("events") or {}
    except (KeyError, IndexError, TypeError):
        raise QuoteError("symbol not found") from None
    splits = list((events.get("splits") or {}).values())
    paid = Decimal(0)
    for event in (events.get("dividends") or {}).values():
        try:
            amount = Decimal(str(event["amount"])) / _split_factor(splits, int(event["date"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if amount.is_finite() and amount > 0:
            paid += amount
    return paid / price  # zero when it paid nothing: that is an answer, not a failure


def _split_factor(splits: list, paid_at: int) -> Decimal:
    """How many of today's shares one share was worth when a dividend was paid."""
    factor = Decimal(1)
    for split in splits:
        try:
            if int(split["date"]) > paid_at:
                factor *= Decimal(str(split["numerator"])) / Decimal(str(split["denominator"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return factor if factor.is_finite() and factor > 0 else Decimal(1)


def _finnhub_dividends(symbol: str, key: str, fetch: Fetch) -> Decimal:
    data = fetch(
        "https://finnhub.io/api/v1/stock/metric?metric=all"
        f"&symbol={urllib.parse.quote(symbol, safe='')}",
        {"X-Finnhub-Token": key},
    )
    metric = data.get("metric") if isinstance(data, dict) else None
    value = metric.get("dividendYieldIndicatedAnnual") if isinstance(metric, dict) else None
    if value is None:
        raise QuoteError("no dividend data")
    return _decimal(value) / 100  # Finnhub reports a percent


def _alphavantage_dividends(symbol: str, key: str, fetch: Fetch) -> Decimal:
    data = fetch(
        "https://www.alphavantage.co/query?function=OVERVIEW"
        f"&symbol={urllib.parse.quote(symbol, safe='')}&apikey={urllib.parse.quote(key, safe='')}",
        {},
    )
    if isinstance(data, dict) and ("Note" in data or "Information" in data):
        raise QuoteError("daily limit reached")
    raw = data.get("DividendYield") if isinstance(data, dict) else None
    if raw in (None, "", "-", "None"):
        raise QuoteError("no dividend data")
    return _decimal(raw)  # already a fraction


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise QuoteError("unreadable dividend yield") from None


def _checked_yield(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise QuoteError("unreadable dividend yield")
    if value > MAX_PLAUSIBLE_YIELD:
        raise QuoteError(f"reported {value:.1%} a year, which looks wrong; enter it yourself")
    return value.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------- crypto
def _crypto_quotes(
    requests: list[QuoteRequest], keys: dict[str, str], fetch: Fetch, result: RefreshResult
) -> None:
    if not requests:
        return
    key = keys.get("coingecko_api_key")
    headers = {"x-cg-demo-api-key": key} if key else {}

    coin_ids: dict[QuoteRequest, str] = {}
    for req in requests:
        if req.quote_id:
            coin_ids[req] = req.quote_id.lower()
            continue
        try:
            coin_ids[req] = result.resolved[req] = _coingecko_lookup(req.symbol, headers, fetch)
        except QuoteError as exc:
            result.errors[req] = f"CoinGecko: {exc}"

    prices: dict[str, Decimal] = {}
    if coin_ids:
        ids = ",".join(sorted(set(coin_ids.values())))
        try:
            data = fetch(
                "https://api.coingecko.com/api/v3/simple/price"
                f"?ids={urllib.parse.quote(ids, safe=',')}&vs_currencies=usd",
                headers,
            )
            for coin_id, entry in (data if isinstance(data, dict) else {}).items():
                try:
                    prices[coin_id] = _price(entry["usd"])
                except (KeyError, TypeError, QuoteError):
                    continue
        except QuoteError as exc:
            for req in coin_ids:
                result.errors[req] = f"CoinGecko: {exc}"

    for req in requests:
        coin_id = coin_ids.get(req)
        if coin_id in prices:
            result.prices[req], result.sources[req] = prices[coin_id], "CoinGecko"
            result.errors.pop(req, None)
            continue
        try:  # fallback: Yahoo lists major coins as BTC-USD, ETH-USD, ...
            result.prices[req] = _yahoo(f"{req.symbol.upper()}-USD", fetch)
            result.sources[req] = "Yahoo"
            result.errors.pop(req, None)
        except QuoteError as exc:
            earlier = result.errors.get(req, "CoinGecko: no price")
            result.errors[req] = f"{earlier}; Yahoo: {exc}"


def _coingecko_lookup(symbol: str, headers: dict[str, str], fetch: Fetch) -> str:
    """Ticker -> CoinGecko id. Many coins share tickers; pick the largest by market cap."""
    data = fetch(
        f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(symbol, safe='')}",
        headers,
    )
    coins = data.get("coins", []) if isinstance(data, dict) else []
    matches = [c for c in coins if str(c.get("symbol", "")).upper() == symbol.upper()]
    if not matches:
        raise QuoteError(f"no coin with symbol {symbol}")
    best = min(matches, key=lambda c: c.get("market_cap_rank") or 10**9)
    return str(best["id"])


def _price(value) -> Decimal:
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise QuoteError("unreadable price") from None
    if not price.is_finite() or price <= 0:
        raise QuoteError("unreadable price")
    return price
