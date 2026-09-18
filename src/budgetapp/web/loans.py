"""Loan tracker: balances, payoff projections, what-ifs, strategies, payment log."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from budgetapp import loans
from budgetapp import networth as nw
from budgetapp.dates import add_months
from budgetapp.money import cents_to_input, parse_money
from budgetapp.web import current_store, forms

bp = Blueprint("loans", __name__, url_prefix="/loans")


@bp.get("/")
def index():
    today = date.today()
    extra = _extra_arg() or 0
    with current_store().read() as conn:
        all_loans = loans.list_loans(conn)
    rows = [
        (loan, _safe_payoff(loan, today), loans.promo_check(loan, today)) for loan in all_loans
    ]
    debt_free = debt_free_date(all_loans, today)
    return render_template(
        "loans.html",
        rows=rows,
        plans=_strategy_rows(all_loans, extra, today),
        names={loan.account_id: loan.name for loan in all_loans},
        extra_input=cents_to_input(extra) if extra else "",
        total_owed=sum(loan.balance_cents for loan in all_loans),
        total_min=sum(loan.min_payment_cents for loan in all_loans),
        total_extra=sum(loan.extra_payment_cents for loan in all_loans),
        debt_free=debt_free,
        has_loans=bool(all_loans),
        min_rules=loans.MIN_RULES,
        today=today,
    )


@bp.post("/")
def create():
    form = request.form
    try:
        fields = _loan_fields(form)
        balance = forms.money(form, "balance", "Current balance")
        with current_store().write() as conn:
            account_id = loans.create_loan(conn, balance_cents=balance, **fields)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("loans.index"))
    return redirect(url_for("loans.detail", account_id=account_id))


@bp.get("/<int:account_id>")
def detail(account_id: int):
    today = date.today()
    whatif = _extra_arg()
    with current_store().read() as conn:
        try:
            loan = loans.get_loan(conn, account_id)
        except LookupError:
            abort(404)
        history = loans.payments(conn, account_id)

    # A loan with no set monthly payment (no minimum, all of it "extra") has no base case.
    minimum = _safe_payoff(loan, today, 0) if loan.min_payment_cents else None
    scenarios = []
    if loan.min_payment_cents:
        scenarios.append(("Payment only, no extra", loan.min_payment_cents, minimum))
    if loan.extra_payment_cents or not loan.min_payment_cents:
        scenarios.append(("Current plan", loan.payment_cents, _safe_payoff(loan, today)))
    if whatif is not None:
        scenarios.append(
            (
                f"What if: {cents_to_input(whatif)} "
                + ("extra" if loan.min_payment_cents else "a month"),
                loan.min_payment_cents + whatif,
                _safe_payoff(loan, today, whatif),
            )
        )
    try:
        schedule = loan.schedule(today=today)
    except ValueError:
        schedule = []
    return render_template(
        "loan.html",
        loan=loan,
        current=_safe_payoff(loan, today),
        minimum=minimum,
        scenarios=scenarios,
        schedule=schedule,
        payments=history,
        promo=loans.promo_check(loan, today),
        min_rules=loans.MIN_RULES,
        whatif_input=cents_to_input(whatif) if whatif is not None else "",
        today=today,
    )


@bp.post("/<int:account_id>")
def update(account_id: int):
    try:
        fields = _loan_fields(request.form)
        with current_store().write() as conn:
            loans.update_loan(conn, account_id, **fields)
        flash("Loan saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("loans.detail", account_id=account_id))


@bp.post("/<int:account_id>/payments")
def record_payment(account_id: int):
    form = request.form
    try:
        with current_store().write() as conn:
            before = loan_balances(conn)
            loans.record_payment(
                conn,
                account_id,
                paid_on=forms.day(form, "paid_on", "Date", default=date.today()),
                amount_cents=forms.money(form, "amount", "Amount"),
                principal_cents=forms.money(form, "principal", "Principal", required=False),
                interest_cents=forms.money(form, "interest", "Interest", required=False),
                notes=forms.text(form, "notes", "Notes", max_len=200),
            )
            flash("Payment recorded.", "info")
            celebrate_payoffs(conn, before)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("loans.detail", account_id=account_id))


@bp.post("/<int:account_id>/payments/<int:payment_id>/delete")
def delete_payment(account_id: int, payment_id: int):
    try:
        with current_store().write() as conn:
            loans.delete_latest_payment(conn, account_id, payment_id)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("loans.detail", account_id=account_id))


@bp.post("/<int:account_id>/delete")
def delete(account_id: int):
    try:
        with current_store().write() as conn:
            loans.get_loan(conn, account_id)
            nw.delete_account(conn, account_id)
    except LookupError:
        abort(404)
    flash("Loan deleted.", "info")
    return redirect(url_for("loans.index"))


# ---------------------------------------------------------------- helpers
def loan_balances(conn) -> dict[int, int]:
    return {loan.account_id: loan.balance_cents for loan in loans.list_loans(conn)}


def celebrate_payoffs(conn, before: dict[int, int]) -> None:
    """If a loan owed money before this write and owes nothing now, flash a celebration:
    the next page rains money (app.js), harder when that was the last loan."""
    after = loan_balances(conn)
    cleared = [i for i, owed in before.items() if owed > 0 and after.get(i, owed) <= 0]
    if not cleared:
        return
    if all(owed <= 0 for owed in after.values()):
        flash("Every loan is paid off. You're debt-free!", "celebrate-big")
    else:
        flash("Loan paid off!", "celebrate")


def debt_free_date(all_loans: list[loans.Loan], today: date) -> date | None:
    """Last payoff date across loans on their current plans; None if any never pays off."""
    owing = [loan for loan in all_loans if loan.balance_cents > 0]
    payoffs = [_safe_payoff(loan, today) for loan in owing]
    if not payoffs or not all(p and p.payoff_date for p in payoffs):
        return None
    return max(p.payoff_date for p in payoffs)


def _loan_fields(form) -> dict:
    rule = form.get("min_rule", "fixed")
    return {
        "name": forms.text(form, "name", "Name", required=True),
        "institution": forms.text(form, "institution", "Lender"),
        "annual_rate": forms.percent(form, "apr", "APR"),
        "min_rule": rule,
        "min_percent": (
            forms.percent(form, "min_percent", "Minimum % of balance")
            if rule == "percent" else None
        ),
        # With a fixed minimum this *is* the minimum; otherwise it's simply what you pay.
        "min_payment_cents": forms.money(
            form, "min_payment", "Monthly payment", required=rule == "fixed"
        ) or 0,
        "extra_payment_cents": forms.money(form, "extra_payment", "Extra payment", required=False)
        or 0,
        "original_principal_cents": forms.money(
            form, "original_principal", "Original amount", required=False
        ),
        "term_months": forms.integer(form, "term_months", "Term (months)", lo=1, hi=1200),
        "start_date": forms.day(form, "start_date", "Start date", required=False),
        "payment_day": forms.integer(form, "payment_day", "Payment day", lo=1, hi=31),
        "promo_ends_on": forms.day(form, "promo_ends_on", "Promo ends", required=False),
    }


def _extra_arg() -> int | None:
    raw = request.args.get("extra", "").strip()
    if not raw:
        return None
    try:
        return parse_money(raw, label="Extra payment")
    except ValueError as exc:
        flash(str(exc), "error")
        return None


def _safe_payoff(loan: loans.Loan, today: date, extra_cents: int | None = None):
    try:
        return loans.payoff(loan.schedule(extra_cents=extra_cents, today=today))
    except ValueError:
        return None


def _strategy_rows(all_loans: list[loans.Loan], extra: int, today: date) -> list[dict]:
    if not all_loans:
        return []
    # Each debt brings its whole planned payment to the shared budget; `extra` tops it up.
    debts = [loan.as_debt(today) for loan in all_loans]
    names = {loan.account_id: loan.name for loan in all_loans}
    results = {}
    for key in loans.STRATEGIES:
        try:
            results[key] = loans.plan_payoff(debts, extra_cents=extra, strategy=key)
        except ValueError:
            results[key] = None
    baseline = results["minimum"]
    rows = []
    for key, label in loans.STRATEGIES.items():
        plan = results[key]
        # A plan that misses a promo would owe interest on the whole purchase, which isn't
        # in its total, so interest "saved" against it would flatter the plan that misses.
        unfair = bool(baseline and plan and baseline.promo_missed and not plan.promo_missed)
        rows.append(
            {
                "label": label,
                "plan": plan,
                "debt_free": add_months(today, plan.months) if plan else None,
                "missed": [names[i] for i in plan.promo_missed] if plan else [],
                "saved": baseline.total_interest_cents - plan.total_interest_cents
                if plan and baseline and not unfair
                else None,
                "baseline_misses_promo": unfair,
            }
        )
    return rows
