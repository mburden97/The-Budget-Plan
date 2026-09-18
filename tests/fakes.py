"""Canned HTTP responses for price providers (no network in tests)."""

from decimal import Decimal

from budgetapp.quotes import QuoteError


def yahoo(price):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": Decimal(str(price))}}]}}


def make_fetch(responses: dict):
    """Fake fetch: first URL-prefix match wins; exceptions are raised; no match -> HTTP 404."""
    calls = []

    def fetch(url, headers):
        calls.append((url, dict(headers)))
        for prefix, response in responses.items():
            if url.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise QuoteError("HTTP 404")

    fetch.calls = calls
    return fetch


YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
CG_SEARCH = "https://api.coingecko.com/api/v3/search?query="
CG_PRICE = "https://api.coingecko.com/api/v3/simple/price?ids="
FINNHUB = "https://finnhub.io/api/v1/quote?symbol="
ALPHA = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
