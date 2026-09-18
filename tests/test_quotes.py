from decimal import Decimal

import pytest

from budgetapp import quotes
from budgetapp.quotes import QuoteError, QuoteRequest, check_url, refresh
from tests.fakes import ALPHA, CG_PRICE, CG_SEARCH, FINNHUB, YAHOO, make_fetch, yahoo

VTI = QuoteRequest("market", "VTI")
BTC = QuoteRequest("crypto", "BTC")


def test_yahoo_market_price_is_decimal():
    fetch = make_fetch({YAHOO + "VTI?": yahoo("376.61")})
    result = refresh([VTI], keys={}, fetch=fetch)
    assert result.prices[VTI] == Decimal("376.61")
    assert result.sources[VTI] == "Yahoo"


def test_finnhub_preferred_when_key_set_and_key_stays_out_of_url():
    fetch = make_fetch({FINNHUB + "VTI": {"c": Decimal("377.00")}, YAHOO: yahoo("1")})
    result = refresh([VTI], keys={"finnhub_api_key": "secretkey123"}, fetch=fetch)
    assert (result.prices[VTI], result.sources[VTI]) == (Decimal("377.00"), "Finnhub")
    url, headers = fetch.calls[0]
    assert "secretkey123" not in url and headers["X-Finnhub-Token"] == "secretkey123"


def test_falls_back_when_finnhub_does_not_know_symbol():
    fetch = make_fetch({FINNHUB: {"c": 0}, YAHOO + "VTSAX?": yahoo("181.65")})
    req = QuoteRequest("market", "VTSAX")
    result = refresh([req], keys={"finnhub_api_key": "secretkey123"}, fetch=fetch)
    assert result.sources[req] == "Yahoo"


def test_all_providers_fail_reports_each():
    fetch = make_fetch({ALPHA: {"Note": "Thank you for using Alpha Vantage"}})
    result = refresh([VTI], keys={"alphavantage_api_key": "abcdefgh"}, fetch=fetch)
    assert VTI not in result.prices
    assert "Yahoo: HTTP 404" in result.errors[VTI]
    assert "Alpha Vantage: daily limit reached" in result.errors[VTI]


def test_crypto_symbol_resolved_by_market_cap():
    fetch = make_fetch(
        {
            CG_SEARCH + "BTC": {
                "coins": [
                    {"id": "batcat", "symbol": "btc", "market_cap_rank": 4000},
                    {"id": "bitcoin", "symbol": "BTC", "market_cap_rank": 1},
                    {"id": "wrapped-bitcoin", "symbol": "WBTC", "market_cap_rank": 20},
                ]
            },
            CG_PRICE + "bitcoin": {"bitcoin": {"usd": 77253}},
        }
    )
    result = refresh([BTC], keys={}, fetch=fetch)
    assert result.prices[BTC] == Decimal("77253")
    assert result.resolved[BTC] == "bitcoin"
    assert result.sources[BTC] == "CoinGecko"


def test_crypto_override_skips_search_and_falls_back_to_yahoo():
    req = QuoteRequest("crypto", "XYZ", "some-coin")
    fetch = make_fetch({CG_PRICE: {}, YAHOO + "XYZ-USD?": yahoo("0.042")})
    result = refresh([req], keys={"coingecko_api_key": "CG-demokey1"}, fetch=fetch)
    assert result.prices[req] == Decimal("0.042") and result.sources[req] == "Yahoo"
    assert not any(url.startswith(CG_SEARCH) for url, _ in fetch.calls)
    assert fetch.calls[0][1]["x-cg-demo-api-key"] == "CG-demokey1"


def test_unknown_crypto_reports_error():
    result = refresh([BTC], keys={}, fetch=make_fetch({CG_SEARCH: {"coins": []}}))
    assert "no coin with symbol BTC" in result.errors[BTC]


@pytest.mark.parametrize(
    "url",
    [
        "http://query1.finance.yahoo.com/v8/finance/chart/VTI",  # not HTTPS
        "https://evil.example/steal",
        "https://query1.finance.yahoo.com.evil.example/x",
    ],
)
def test_only_allowlisted_https_hosts(url):
    with pytest.raises(QuoteError):
        check_url(url)
    with pytest.raises(QuoteError):
        quotes.http_fetch(url, {})


def test_rejects_nonsense_prices():
    fetch = make_fetch({YAHOO: yahoo("-5")})
    assert VTI in refresh([VTI], keys={}, fetch=fetch).errors


DIV_CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "PAY?range=1y&interval=1mo&events=div%2Csplit"
)


def _chart(price, dividends, splits=None):
    events = {"dividends": {str(i): d for i, d in enumerate(dividends)}}
    if splits:
        events["splits"] = {str(i): s for i, s in enumerate(splits)}
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price}, "events": events}]}}


def test_yahoo_dividend_yield_sums_a_year_of_payments():
    fetch = make_fetch({DIV_CHART: _chart(
        100, [{"date": 1, "amount": "0.50"}, {"date": 2, "amount": "0.75"}]
    )})
    req = QuoteRequest("market", "PAY")
    result = quotes.dividend_yields([req], keys={}, fetch=fetch)
    assert result.yields[req] == Decimal("0.012500")  # $1.25 a year on a $100 price
    assert result.sources[req] == "Yahoo" and not result.errors


def test_dividends_before_a_split_are_restated_per_share():
    fetch = make_fetch({DIV_CHART: _chart(
        100,
        [{"date": 1, "amount": "2.00"}, {"date": 9, "amount": "1.00"}],
        [{"date": 5, "numerator": 2, "denominator": 1}],
    )})
    req = QuoteRequest("market", "PAY")
    # The $2.00 was paid on shares that later became two, so it counts as $1.00 today.
    assert quotes.dividend_yields([req], keys={}, fetch=fetch).yields[req] == Decimal("0.020000")


def test_a_non_payer_yields_zero_and_crypto_is_skipped():
    fetch = make_fetch({DIV_CHART: {"chart": {"result": [{"meta": {"regularMarketPrice": 100}}]}}})
    pays, coin = QuoteRequest("market", "PAY"), QuoteRequest("crypto", "BTC")
    result = quotes.dividend_yields([pays, coin], keys={}, fetch=fetch)
    assert result.yields == {pays: Decimal(0)} and not result.errors


def test_an_implausible_yield_is_refused_and_fallbacks_are_tried():
    fetch = make_fetch({
        DIV_CHART: _chart(1, [{"date": 1, "amount": "0.90"}]),  # 90% a year: bad data
        "https://finnhub.io/api/v1/stock/metric?metric=all&symbol=PAY": {
            "metric": {"dividendYieldIndicatedAnnual": 3.5}
        },
    })
    req = QuoteRequest("market", "PAY")
    result = quotes.dividend_yields([req], keys={"finnhub_api_key": "k"}, fetch=fetch)
    assert result.yields[req] == Decimal("0.035000") and result.sources[req] == "Finnhub"

    only_yahoo = quotes.dividend_yields([req], keys={}, fetch=fetch)
    assert req not in only_yahoo.yields and "looks wrong" in only_yahoo.errors[req]
