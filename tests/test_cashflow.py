from datetime import date
from decimal import Decimal

from budgetapp import cashflow, loans, planning
from budgetapp import networth as nw
from budgetapp import transactions as tx

TODAY = date(2026, 9, 14)


def _add(conn, account, day, description, cents):
    tx.add_transaction(
        conn, posted_on=day, description=description, amount_cents=cents, account_id=account
    )


def _setup(conn):
    cats = {c.name: c.id for c in planning.list_categories(conn)}
    everyday = nw.add_account(conn, name="Everyday", type="cash")
    nw.record_balance(conn, everyday, 50000, date(2026, 9, 13))
    savings = nw.add_account(conn, name="Rainy", type="cash", emergency_fund=True)
    for day in (3, 5, 7, 9, 11):  # busier, but emergency savings isn't the everyday account
        _add(conn, savings, date(2026, 9, day), "INTEREST", 100)
    for month, day in ((8, 1), (8, 15), (8, 29), (9, 12)):
        _add(conn, everyday, date(2026, month, day), "ACME PAYROLL PPD 123", 200000)
    for month, day in ((6, 20), (7, 21), (8, 20)):
        _add(conn, everyday, date(2026, month, day), "CARD CO AUTOPAY", -80000)
    rent = planning.add_line_item(
        conn, category_id=cats["Fixed Expenses"], name="Rent", amount_cents=130000
    )
    tx.add_rule(conn, pattern="RENTCO ONLINE PMT", line_item_id=rent)
    for month in (6, 7, 8, 9):
        _add(conn, everyday, date(2026, month, 3), "RENTCO ONLINE PMT 77X", -130000)
    card = planning.add_line_item(
        conn, category_id=cats["Debt Payments"], name="Store card", amount_cents=48000
    )
    tx.add_rule(conn, pattern="LENDER PMT", line_item_id=card)
    for month in (6, 7, 8):  # paid unevenly; the Loans page has the plan
        _add(conn, everyday, date(2026, month, 25), "LENDER PMT 991", -30000)
    loans.create_loan(
        conn, name="Store card", balance_cents=500000, annual_rate=Decimal(0),
        min_payment_cents=48000, payment_day=25, as_of=date(2026, 9, 13),
    )
    for day in (1, 5, 9):  # to a friend, every few days: not a schedule
        _add(conn, everyday, date(2026, 9, day), "Zelle payment to Pat", -2000)
    return everyday


def test_forecast_lays_out_the_next_two_weeks(conn):
    _setup(conn)
    result = cashflow.forecast(conn, TODAY)
    assert (result.account, result.start_cents, result.balance_as_of, result.counted) == (
        "Everyday", 50000, date(2026, 9, 13), 0,
    )
    assert [(e.when, e.label, e.amount_cents, e.balance_cents) for e in result.events] == [
        (date(2026, 9, 20), "Card Co Autopay", -80000, -30000),
        (date(2026, 9, 25), "Store card", -48000, -78000),  # the loan's planned payment
        (date(2026, 9, 26), "Acme Payroll Ppd", 200000, 122000),
    ]  # rent is next due Oct 3, outside the window; payments to Pat aren't a schedule
    assert result.short.when == date(2026, 9, 20) and result.low_cents == -78000


def test_newer_transactions_count_and_stopped_bills_drop_out(conn):
    everyday = _setup(conn)
    _add(conn, everyday, date(2026, 9, 14), "COFFEE SHOP", -450)  # after the recorded balance
    result = cashflow.forecast(conn, TODAY)
    assert (result.start_cents, result.counted) == (49550, 1)
    later = cashflow.forecast(conn, date(2026, 11, 20), days=7)  # no payroll since Sep 12
    assert all(e.label != "Acme Payroll Ppd" for e in later.events)


def test_no_everyday_account(conn):
    assert cashflow.forecast(conn, TODAY) is None
    daily = cashflow.Recurring("Rent", "monthly", 30, -1000, date(2026, 1, 31))
    assert daily.between(date(2026, 1, 31), date(2026, 4, 30)) == [
        date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
    ]
