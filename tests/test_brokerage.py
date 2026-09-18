from datetime import date
from decimal import Decimal

import pytest

from budgetapp import brokerage
from budgetapp import networth as nw
from budgetapp.quotes import QuoteRequest, RefreshResult


@pytest.fixture
def taxable(conn):
    return nw.add_account(conn, name="Taxable", type="brokerage")


def _balance(conn, account_id):
    return nw.get_account(conn, account_id).balance_cents


def test_holdings_only_in_investment_accounts(conn):
    checking = nw.add_account(conn, name="Checking", type="cash")
    with pytest.raises(ValueError):
        brokerage.add_holding(conn, account_id=checking, symbol="VTI", shares=Decimal(1))


def test_manual_cash_holding_values_account(conn, taxable):
    brokerage.add_holding(
        conn, account_id=taxable, symbol="cash", shares=Decimal("1234.56"),
        asset_class="cash", quote_source="manual",
    )
    [holding] = brokerage.list_holdings(conn)
    assert holding.symbol == "CASH" and holding.value_cents == 123456
    assert _balance(conn, taxable) == 123456
    assert brokerage.quote_requests(conn) == []


def test_validation(conn, taxable):
    brokerage.add_holding(conn, account_id=taxable, symbol="VTI", shares=Decimal(1))
    with pytest.raises(ValueError):
        brokerage.add_holding(conn, account_id=taxable, symbol="vti", shares=Decimal(1))
    with pytest.raises(ValueError):
        brokerage.add_holding(conn, account_id=taxable, symbol="bad symbol!", shares=Decimal(1))
    with pytest.raises(ValueError):
        brokerage.add_holding(
            conn, account_id=taxable, symbol="HOUSE", shares=Decimal(1), quote_source="manual"
        )
    for bad in ["abc", "-1", "1.12345678901"]:
        with pytest.raises(ValueError):
            brokerage.parse_quantity(bad)
    assert brokerage.parse_quantity("0.00012345") == Decimal("0.00012345")


def test_apply_prices_updates_holdings_history_and_balance(conn, taxable):
    coins = nw.add_account(conn, name="Coinbase", type="crypto")
    brokerage.add_holding(
        conn, account_id=taxable, symbol="VTI", shares=Decimal(10), asset_class="us_stock",
        cost_basis_cents=300000,
    )
    brokerage.add_holding(
        conn, account_id=coins, symbol="BTC", shares=Decimal("0.5"), asset_class="crypto",
        quote_source="crypto",
    )
    vti, btc = QuoteRequest("market", "VTI"), QuoteRequest("crypto", "BTC")
    assert set(brokerage.quote_requests(conn)) == {vti, btc}

    result = RefreshResult(
        prices={vti: Decimal("376.61"), btc: Decimal("77253")}, resolved={btc: "bitcoin"}
    )
    assert brokerage.apply_prices(conn, result, date.today()) == 2

    holdings = {h.symbol: h for h in brokerage.list_holdings(conn)}
    assert holdings["VTI"].value_cents == 376610 and holdings["VTI"].gain_cents == 76610
    assert holdings["BTC"].quote_id == "bitcoin" and holdings["BTC"].value_cents == 3862650
    assert _balance(conn, taxable) == 376610 and _balance(conn, coins) == 3862650
    keys = {r["symbol"] for r in conn.execute("SELECT symbol FROM prices")}
    assert keys == {"market:VTI", "crypto:bitcoin"}
    assert nw.net_worth(conn, date.today()).assets_cents == 376610 + 3862650


def test_update_and_delete_revalue(conn, taxable):
    holding_id = brokerage.add_holding(
        conn, account_id=taxable, symbol="VTI", shares=Decimal(10), price=Decimal(100)
    )
    assert _balance(conn, taxable) == 100000
    brokerage.update_holding(
        conn, holding_id, symbol="VTI", shares=Decimal(12), asset_class="us_stock", name="Total",
        cost_basis_cents=None, quote_source="market", quote_id="",
    )
    assert _balance(conn, taxable) == 120000
    brokerage.delete_holding(conn, holding_id)
    assert brokerage.list_holdings(conn) == []


def test_changing_symbol_clears_resolved_id(conn, taxable):
    holding_id = brokerage.add_holding(
        conn, account_id=taxable, symbol="ETH", shares=Decimal(1), quote_source="crypto",
        quote_id="ethereum",
    )
    brokerage.update_holding(
        conn, holding_id, symbol="SOL", shares=Decimal(1), asset_class="crypto", name="",
        cost_basis_cents=None, quote_source="crypto", quote_id="ethereum",
    )
    assert brokerage.get_holding(conn, holding_id).quote_id == ""


def test_allocation_and_targets(conn, taxable):
    brokerage.add_holding(
        conn, account_id=taxable, symbol="VTI", shares=Decimal(3), asset_class="us_stock",
        price=Decimal(100),
    )
    brokerage.add_holding(
        conn, account_id=taxable, symbol="BND", shares=Decimal(1), asset_class="bond",
        price=Decimal(100),
    )
    with pytest.raises(ValueError):
        brokerage.set_targets(conn, {"us_stock": Decimal("0.6"), "bond": Decimal("0.3")})
    brokerage.set_targets(conn, {"us_stock": Decimal("0.6"), "bond": Decimal("0.4")})
    rows = {r.asset_class: r for r in brokerage.allocation(
        brokerage.list_holdings(conn), brokerage.get_targets(conn)
    )}
    assert rows["us_stock"].share == Decimal("0.75")
    assert rows["us_stock"].drift == Decimal("0.15")
    assert rows["bond"].drift == Decimal("-0.15")
    brokerage.set_targets(conn, {})
    assert brokerage.get_targets(conn) == {}


def test_dividends_reinvested_or_paid_out(conn):
    account = nw.add_account(conn, name="Taxable", type="brokerage")
    drip = brokerage.add_holding(
        conn, account_id=account, symbol="DRIP", shares=Decimal(100), quote_source="manual",
        price=Decimal(10), dividend_yield=Decimal("0.04"),
    )
    brokerage.add_holding(
        conn, account_id=account, symbol="CASHY", shares=Decimal(50), quote_source="manual",
        price=Decimal(10), dividend_yield=Decimal("0.02"), reinvest=False,
    )
    brokerage.add_holding(
        conn, account_id=account, symbol="QUIET", shares=Decimal(50), quote_source="manual",
        price=Decimal(10),
    )
    holdings = brokerage.list_holdings(conn)
    assert [h.annual_dividend_cents for h in holdings] == [1000, 4000, None]  # alphabetical
    paid = brokerage.dividends(holdings)
    assert (paid.invested_cents, paid.reinvested_cents, paid.paid_out_cents) == (200000, 4000, 1000)
    assert paid.total_cents == 5000
    assert paid.reinvest_yield == Decimal("0.02")  # $40 of $2,000 invested
    assert paid.payout_yield == Decimal("0.005")

    brokerage.update_holding(
        conn, drip, symbol="DRIP", shares=Decimal(100), asset_class="us_stock", name="",
        cost_basis_cents=None, quote_source="manual", quote_id="", dividend_yield=Decimal("0.03"),
        reinvest=False,
    )
    changed = brokerage.dividends(brokerage.list_holdings(conn))
    assert (changed.reinvested_cents, changed.paid_out_cents) == (0, 4000)
    with pytest.raises(ValueError, match="between 0% and 25%"):
        brokerage.add_holding(
            conn, account_id=account, symbol="TOOMUCH", shares=Decimal(1),
            dividend_yield=Decimal("0.5"),
        )
