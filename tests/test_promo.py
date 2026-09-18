from datetime import date
from decimal import Decimal

from budgetapp import loans

TODAY = date(2026, 9, 15)


def _update(conn, loan_id, **changes):
    fields = {
        "name": "Store card", "institution": "", "annual_rate": Decimal(0),
        "min_payment_cents": 30000, "extra_payment_cents": 0, "original_principal_cents": None,
        "term_months": None, "start_date": None, "payment_day": 20,
        "promo_ends_on": date(2027, 6, 20),
    }
    loans.update_loan(conn, loan_id, **(fields | changes))


def test_promo_check(conn):
    loan_id = loans.create_loan(
        conn, name="Store card", balance_cents=540000, annual_rate=Decimal(0),
        min_payment_cents=30000, payment_day=20, promo_ends_on=date(2027, 6, 20), as_of=TODAY,
    )
    loan = loans.get_loan(conn, loan_id)
    assert loan.promo_ends_on == date(2027, 6, 20)
    check = loans.promo_check(loan, TODAY)  # Sep 20 through Jun 20: 10 payments
    assert (check.payments_left, check.needed_cents, check.planned_cents) == (10, 54000, 30000)
    assert not check.on_track and not check.ended

    _update(conn, loan_id, extra_payment_cents=25000)
    assert loans.promo_check(loans.get_loan(conn, loan_id), TODAY).on_track  # $550 >= $540

    over = loans.promo_check(loans.get_loan(conn, loan_id), date(2027, 6, 21))
    assert over.ended and not over.on_track and over.payments_left == 0
    assert over.needed_cents == 540000  # nothing left to spread it over

    _update(conn, loan_id, promo_ends_on=None)
    assert loans.promo_check(loans.get_loan(conn, loan_id), TODAY) is None


def test_no_promo_or_paid_off(conn):
    car = loans.create_loan(
        conn, name="Car", balance_cents=100, annual_rate=Decimal(0), min_payment_cents=100,
        as_of=TODAY,
    )
    assert loans.promo_check(loans.get_loan(conn, car), TODAY) is None
    cleared = loans.create_loan(
        conn, name="Old promo", balance_cents=0, annual_rate=Decimal(0), min_payment_cents=100,
        promo_ends_on=date(2027, 1, 1), as_of=TODAY,
    )
    assert loans.promo_check(loans.get_loan(conn, cleared), TODAY) is None
