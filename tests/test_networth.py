from datetime import date
from decimal import Decimal

import pytest

from budgetapp import networth as nw
from budgetapp.charts import compact_money, line_chart


def test_net_worth_uses_latest_snapshot(conn):
    checking = nw.add_account(conn, name="Checking", type="cash")
    brokerage = nw.add_account(conn, name="Taxable", type="brokerage")
    car = nw.add_account(conn, name="Car loan", type="loan")

    nw.record_balance(conn, checking, 500000, date(2026, 1, 1))
    nw.record_balance(conn, checking, 300000, date(2026, 2, 1))
    nw.record_balance(conn, brokerage, 2000000, date(2026, 1, 15))
    nw.record_balance(conn, car, 1500000, date(2026, 1, 1))

    jan = nw.net_worth(conn, date(2026, 1, 31))
    assert (jan.assets_cents, jan.liabilities_cents, jan.net_cents) == (2500000, 1500000, 1000000)
    assert nw.net_worth(conn, date(2026, 2, 28)).net_cents == 800000
    assert nw.net_worth(conn, date(2025, 12, 31)).net_cents == 0


def test_record_balance_overwrites_same_day(conn):
    acct = nw.add_account(conn, name="Savings", type="cash")
    nw.record_balance(conn, acct, 100, date(2026, 3, 1))
    nw.record_balance(conn, acct, 200, date(2026, 3, 1))
    assert nw.net_worth(conn, date(2026, 3, 1)).assets_cents == 200


def test_list_accounts_with_latest_balance(conn):
    acct = nw.add_account(conn, name="Checking", type="cash", emergency_fund=True)
    nw.record_balance(conn, acct, 100, date(2026, 1, 1))
    nw.record_balance(conn, acct, 250, date(2026, 2, 1))
    card = nw.add_account(conn, name="Visa", type="credit_card")
    accounts = {a.name: a for a in nw.list_accounts(conn)}
    assert accounts["Checking"].balance_cents == 250
    assert accounts["Checking"].balance_as_of == "2026-02-01"
    assert accounts["Checking"].emergency_fund
    assert accounts["Visa"].is_liability and accounts["Visa"].balance_cents is None

    nw.update_account(
        conn, card, name="Visa", institution="", notes="", include_in_net_worth=True,
        emergency_fund=False, archived=True,
    )
    assert [a.name for a in nw.list_accounts(conn)] == ["Checking"]
    assert len(nw.list_accounts(conn, include_archived=True)) == 2


def test_history_and_staleness(conn):
    cash = nw.add_account(conn, name="Checking", type="cash")
    nw.record_balance(conn, cash, 100_00, date(2026, 1, 10))
    nw.record_balance(conn, cash, 300_00, date(2026, 3, 5))
    points = nw.history(conn, today=date(2026, 3, 20))
    assert [p.as_of for p in points] == ["2026-01-31", "2026-02-28", "2026-03-20"]
    assert [p.net_cents for p in points] == [10000, 10000, 30000]
    assert nw.stale_accounts(conn, days=30, today=date(2026, 3, 20)) == []
    stale = nw.stale_accounts(conn, days=30, today=date(2026, 5, 1))
    assert [a.name for a in stale] == ["Checking"]


def test_delete_snapshot_and_account(conn):
    acct = nw.add_account(conn, name="Checking", type="cash")
    nw.record_balance(conn, acct, 100, date(2026, 1, 1))
    [snap] = nw.snapshots(conn, acct)
    assert nw.delete_snapshot(conn, snap["id"]) == acct
    nw.delete_account(conn, acct)
    with pytest.raises(LookupError):
        nw.get_account(conn, acct)


def test_savings_interest(conn):
    hysa = nw.add_account(conn, name="HYSA", type="cash", emergency_fund=True)
    nw.record_balance(conn, hysa, 390000, date(2026, 9, 10))
    fields = dict(
        name="HYSA", institution="Example Bank", notes="", include_in_net_worth=True,
        emergency_fund=True, archived=False,
    )
    nw.update_account(conn, hysa, **fields, apy=Decimal("0.03"))
    account = nw.get_account(conn, hysa)
    assert account.apy == Decimal("0.03")
    assert account.monthly_interest_cents == 962  # $3,900 at 3% APY, one month
    with pytest.raises(ValueError):
        nw.update_account(conn, hysa, **fields, apy=Decimal("1.5"))
    nw.update_account(conn, hysa, **fields)  # no APY clears it
    assert nw.get_account(conn, hysa).monthly_interest_cents is None


def test_record_transfer(conn):
    everyday = nw.add_account(conn, name="Everyday", type="cash")
    rainy = nw.add_account(conn, name="Rainy Day", type="cash", emergency_fund=True)
    card = nw.add_account(conn, name="Card", type="credit_card")
    nw.record_balance(conn, everyday, 120000, date(2026, 3, 1))
    nw.record_transfer(
        conn, to_account_id=rainy, from_account_id=everyday, amount_cents=25000, on=date(2026, 3, 5)
    )
    nw.record_transfer(conn, to_account_id=rainy, amount_cents=5000, on=date(2026, 3, 5))
    assert nw.get_account(conn, rainy).balance_cents == 30000
    assert nw.get_account(conn, everyday).balance_cents == 95000

    bad = [
        ({"to_account_id": rainy, "amount_cents": 0}, "more than"),
        ({"to_account_id": rainy, "from_account_id": rainy, "amount_cents": 100}, "different"),
        ({"to_account_id": card, "amount_cents": 100}, "cash account"),
        ({"to_account_id": rainy, "amount_cents": 100, "on": date(2026, 3, 4)}, "later date"),
        ({"to_account_id": rainy, "from_account_id": card, "amount_cents": 100}, "cash account"),
    ]
    for kwargs, message in bad:
        with pytest.raises(ValueError, match=message):
            nw.record_transfer(conn, **({"on": date(2026, 3, 6)} | kwargs))
    assert nw.get_account(conn, rainy).balance_cents == 30000  # nothing half-applied


def test_add_account_validation(conn):
    with pytest.raises(ValueError):
        nw.add_account(conn, name=" ", type="cash")
    with pytest.raises(ValueError):
        nw.add_account(conn, name="X", type="piggy_bank")


def test_line_chart():
    assert str(line_chart([(date(2026, 1, 1), 1)])) == ""
    svg = str(line_chart([(date(2026, 1, 31), -50_000), (date(2026, 2, 28), 120_000)]))
    assert svg.startswith("<svg") and "chart-line" in svg and "chart-zero" in svg
    assert compact_money(1_234_567_89) == "$1.2M"
    assert compact_money(-45_000_00) == "-$45k"
