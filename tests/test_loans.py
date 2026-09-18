from datetime import date
from decimal import Decimal

import pytest

from budgetapp import loans, networth
from budgetapp.loans import Debt, add_months, amortize, payoff, plan_payoff, standard_payment


def test_standard_payment():
    # $10,000 at 6% for 5 years -> $193.33/mo
    assert standard_payment(1_000_000, Decimal("0.06"), 60) == 19333
    assert standard_payment(120_000, Decimal("0"), 12) == 10000


def test_schedule_pays_off_on_term():
    schedule = amortize(1_000_000, Decimal("0.06"), 19333, first_due=date(2026, 1, 15))
    result = payoff(schedule)
    assert result.months == 60
    assert schedule[-1].balance_cents == 0
    assert result.payoff_date == date(2030, 12, 15)
    assert abs(result.total_interest_cents - 159_968) < 100  # textbook $1,599.68
    assert result.total_paid_cents == 1_000_000 + result.total_interest_cents
    for row in schedule:
        assert row.principal_cents + row.interest_cents == row.payment_cents


def test_extra_payment_saves_interest_and_time():
    base = payoff(amortize(1_000_000, Decimal("0.06"), 19333))
    fast = payoff(amortize(1_000_000, Decimal("0.06"), 19333, extra_cents=10000))
    assert fast.months < base.months
    assert fast.total_interest_cents < base.total_interest_cents


def test_payment_below_interest_rejected():
    with pytest.raises(ValueError):
        amortize(1_000_000, Decimal("0.24"), 10000)  # interest is $200/mo


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2027, 12, 31), 2) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 5), 14) == date(2028, 1, 5)


# ---------------------------------------------------------------- strategies
def test_single_debt_plan_matches_amortization():
    debt = Debt(1, "Car", 1_000_000, Decimal("0.06"), 19333)
    for extra in (0, 10000):
        plan = plan_payoff([debt], extra_cents=extra, strategy="avalanche")
        ref = payoff(amortize(1_000_000, Decimal("0.06"), 19333, extra_cents=extra))
        assert (plan.months, plan.total_interest_cents) == (ref.months, ref.total_interest_cents)


def test_avalanche_vs_snowball():
    small = Debt(1, "Small, low rate", 200_000, Decimal("0.03"), 5_000)
    big = Debt(2, "Big, high rate", 2_000_000, Decimal("0.08"), 25_000)
    minimum = plan_payoff([small, big], strategy="minimum")
    avalanche = plan_payoff([small, big], extra_cents=20_000, strategy="avalanche")
    snowball = plan_payoff([small, big], extra_cents=20_000, strategy="snowball")

    assert avalanche.total_interest_cents <= snowball.total_interest_cents
    assert snowball.total_interest_cents < minimum.total_interest_cents
    assert avalanche.months < minimum.months
    assert snowball.payoff_month[1] < avalanche.payoff_month[1]  # snowball clears small first
    assert avalanche.payoff_month[2] <= snowball.payoff_month[2]  # avalanche attacks the APR
    assert snowball.order == [1, 2]


def test_plan_that_never_pays_off():
    with pytest.raises(ValueError):
        plan_payoff([Debt(1, "x", 1_000_000, Decimal("0.24"), 10_000)], strategy="minimum")


# ---------------------------------------------------------------- storage
def _student_loan(conn):
    return loans.create_loan(
        conn,
        name="Student loan",
        balance_cents=1_000_000,
        annual_rate=Decimal("0.06"),
        min_payment_cents=19333,
        payment_day=15,
        as_of=date(2026, 1, 1),
    )


def test_create_loan_counts_as_liability(conn):
    loan_id = _student_loan(conn)
    loan = loans.get_loan(conn, loan_id)
    assert (loan.balance_cents, loan.annual_rate, loan.original_principal_cents) == (
        1_000_000,
        Decimal("0.06"),
        1_000_000,
    )
    assert networth.net_worth(conn, date(2026, 1, 1)).liabilities_cents == 1_000_000


def test_record_payment_estimates_split_and_can_be_undone(conn):
    loan_id = _student_loan(conn)
    loans.record_payment(conn, loan_id, paid_on=date(2026, 1, 15), amount_cents=19333)
    [payment] = loans.payments(conn, loan_id)
    assert (payment.interest_cents, payment.principal_cents) == (5000, 14333)
    assert loans.get_loan(conn, loan_id).balance_cents == 1_000_000 - 14333

    with pytest.raises(ValueError):  # before the latest recorded balance
        loans.record_payment(conn, loan_id, paid_on=date(2026, 1, 2), amount_cents=100)

    loans.delete_latest_payment(conn, loan_id, payment.id)
    assert loans.get_loan(conn, loan_id).balance_cents == 1_000_000
    assert loans.payments(conn, loan_id) == []


def test_record_payment_with_statement_split(conn):
    loan_id = _student_loan(conn)
    loans.record_payment(
        conn, loan_id, paid_on=date(2026, 2, 15), amount_cents=20000, principal_cents=15500
    )
    [payment] = loans.payments(conn, loan_id)
    assert payment.interest_cents == 4500
    with pytest.raises(ValueError):
        loans.record_payment(
            conn, loan_id, paid_on=date(2026, 3, 15), amount_cents=100, principal_cents=50,
            interest_cents=10,
        )


def test_only_latest_payment_can_be_undone(conn):
    loan_id = _student_loan(conn)
    loans.record_payment(conn, loan_id, paid_on=date(2026, 1, 15), amount_cents=19333)
    loans.record_payment(conn, loan_id, paid_on=date(2026, 2, 15), amount_cents=19333)
    oldest = loans.payments(conn, loan_id)[-1]
    with pytest.raises(ValueError):
        loans.delete_latest_payment(conn, loan_id, oldest.id)


def test_next_due_date(conn):
    loan = loans.get_loan(conn, _student_loan(conn))
    assert loan.next_due(date(2026, 3, 10)) == date(2026, 3, 15)
    assert loan.next_due(date(2026, 3, 15)) == date(2026, 4, 15)
    end_of_month = loan.__class__(**(loan.__dict__ | {"payment_day": 31}))
    assert end_of_month.next_due(date(2026, 2, 10)) == date(2026, 2, 28)
    assert end_of_month.next_due(date(2026, 2, 28)) == date(2026, 3, 31)


def test_update_loan_validates(conn):
    loan_id = _student_loan(conn)
    fields = dict(
        name="Student loan", institution="Servicer", annual_rate=Decimal("0.055"),
        min_payment_cents=20000, extra_payment_cents=5000, original_principal_cents=None,
        term_months=120, start_date=date(2020, 9, 1), payment_day=1,
    )
    loans.update_loan(conn, loan_id, **fields)
    loan = loans.get_loan(conn, loan_id)
    assert (loan.annual_rate, loan.payment_cents, loan.original_principal_cents) == (
        Decimal("0.055"), 25000, 1_000_000,
    )
    with pytest.raises(ValueError):
        loans.update_loan(conn, loan_id, **(fields | {"annual_rate": Decimal("1.5")}))


# ---------------------------------------------------------------- promos and minimum rules
def _store_card(promo_payments=9, planned=60_000):
    """$6,000 at 0% until a deadline nine payments away; the minimum is 3% of the balance."""
    return Debt(1, "Store card", 600_000, Decimal(0), 0, planned_cents=planned,
                min_rule="percent", min_percent=Decimal("0.03"), promo_payments=promo_payments)


def _personal(planned=30_000):
    """$3,000 at 10% with no minimum at all."""
    return Debt(2, "Personal", 300_000, Decimal("0.10"), 0, planned_cents=planned, min_rule="none")


def test_a_promo_is_funded_to_its_deadline_before_avalanche_runs():
    plan = plan_payoff([_store_card(), _personal()], strategy="avalanche")
    assert plan.promo_missed == ()
    assert plan.payoff_month[1] <= 9  # cleared in time

    # The same loans with no deadline known: avalanche sends everything spare to the 10% loan
    # and the 0% card crawls along on its 3% minimum, well past where the promo would end.
    blind = plan_payoff([_store_card(promo_payments=None), _personal()], strategy="avalanche")
    assert blind.payoff_month[1] > 9
    assert blind.payoff_month[2] < plan.payoff_month[2]  # what the blind plan was optimising


def test_a_plan_that_cannot_clear_a_promo_says_so():
    short = [_store_card(planned=40_000), _personal(planned=10_000)]  # $500 against a $667 pace
    for strategy in ("avalanche", "snowball", "minimum"):
        assert plan_payoff(short, strategy=strategy).promo_missed == (1,), strategy
    # Current payments alone ($600 on the card) fall short too, while pooling the budget doesn't.
    assert plan_payoff([_store_card(), _personal()], strategy="minimum").promo_missed == (1,)


def test_a_loan_with_no_minimum_takes_everything_left_over():
    # A $900 budget: the card's 3% minimum is $180, and nothing holds back the other $720,
    # so it all goes to the personal loan, which has no minimum of its own.
    small = Debt(2, "Personal", 70_000, Decimal("0.10"), 0, planned_cents=30_000, min_rule="none")
    plan = plan_payoff([_store_card(promo_payments=None), small], strategy="avalanche")
    assert plan.payoff_month[2] == 1


def test_percent_minimums_follow_the_balance():
    card = _store_card()
    assert (card.required(600_000), card.required(10_000), card.required(0)) == (18_000, 300, 0)
    assert _personal().required(300_000) == 0
    fixed = Debt(3, "Car", 1_000_000, Decimal("0.06"), 19_333)
    assert (fixed.required(5), fixed.planned) == (19_333, 19_333)


def test_the_baseline_pays_each_loans_whole_current_payment():
    with_extra = Debt(1, "Car", 1_000_000, Decimal("0.06"), 19_333, planned_cents=29_333)
    baseline = plan_payoff([with_extra], strategy="minimum")
    ref = payoff(amortize(1_000_000, Decimal("0.06"), 19_333, extra_cents=10_000))
    assert (baseline.months, baseline.total_interest_cents) == (
        ref.months, ref.total_interest_cents
    )


def test_minimum_rules_are_stored_and_checked(conn):
    card = loans.create_loan(
        conn, name="Store card", balance_cents=600_000, annual_rate=Decimal(0),
        min_payment_cents=60_000, min_rule="percent", min_percent=Decimal("0.03"),
        payment_day=15, promo_ends_on=date(2027, 3, 20), as_of=date(2026, 9, 1),
    )
    personal = loans.create_loan(
        conn, name="Personal", balance_cents=300_000, annual_rate=Decimal("0.10"),
        min_payment_cents=0, extra_payment_cents=30_000, min_rule="none", as_of=date(2026, 9, 1),
    )
    card_loan, personal_loan = loans.get_loan(conn, card), loans.get_loan(conn, personal)
    assert (card_loan.min_rule, card_loan.min_percent) == ("percent", Decimal("0.03"))
    assert card_loan.required_minimum_cents == 18_000
    assert (personal_loan.min_rule, personal_loan.required_minimum_cents) == ("none", 0)
    assert personal_loan.payment_cents == 30_000 and personal_loan.schedule(today=date(2026, 9, 1))

    # Payments due on the 15th from Oct 15 through Mar 15: six before March 20.
    assert card_loan.as_debt(date(2026, 9, 20)).promo_payments == 6
    assert card_loan.as_debt().promo_payments is None
    assert card_loan.as_debt(date(2027, 3, 21)).promo_payments is None  # the promo is over

    base = dict(name="Personal", institution="", annual_rate=Decimal("0.10"),
                original_principal_cents=None, term_months=None, start_date=None, payment_day=None)
    with pytest.raises(ValueError, match="what you pay each month"):
        loans.update_loan(conn, personal, min_payment_cents=0, extra_payment_cents=0,
                          min_rule="none", **base)
    for bad in (Decimal(0), Decimal("0.6"), None):
        with pytest.raises(ValueError, match="at most 50%"):
            loans.update_loan(conn, personal, min_payment_cents=5_000, extra_payment_cents=0,
                              min_rule="percent", min_percent=bad, **base)
    with pytest.raises(ValueError, match="more than \\$0"):
        loans.update_loan(conn, personal, min_payment_cents=0, extra_payment_cents=5_000,
                          min_rule="fixed", **base)
    with pytest.raises(ValueError, match="how the minimum"):
        loans.update_loan(conn, personal, min_payment_cents=5_000, extra_payment_cents=0,
                          min_rule="sometimes", **base)
