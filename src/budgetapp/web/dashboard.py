"""Home dashboard: every tracker at a glance, plus what needs attention."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from budgetapp import brokerage, cashflow, goals, loans, planning, transactions
from budgetapp import networth as nw
from budgetapp import settings as prefs
from budgetapp import subscriptions as subs
from budgetapp.charts import donut, line_chart
from budgetapp.money import format_money
from budgetapp.web import current_store, forms
from budgetapp.web.loans import celebrate_payoffs, debt_free_date, loan_balances

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    today = date.today()
    with current_store().read() as conn:
        current = nw.net_worth(conn, today)
        previous = nw.net_worth(conn, today - timedelta(days=30))
        history = nw.history(conn, today=today, max_points=13)
        reminder_days = prefs.get_int(conn, "networth_reminder_days", 30)
        stale = nw.stale_accounts(conn, days=reminder_days, today=today)
        plan = planning.summarize(planning.grouped(conn))
        month = transactions.budget_vs_actual(conn, today)
        totals = transactions.month_totals(conn, today)
        emergency = goals.emergency_fund(conn)
        funds = goals.list_funds(conn)
        all_loans = loans.list_loans(conn)
        holdings = brokerage.list_holdings(conn)
        refreshed = prefs.get(conn, "prices_refreshed_at")
        cash_accounts = nw.list_accounts(conn, types=("cash",))
        late_charges = len(subs.charges_after_cancel(conn))
        upcoming = cashflow.forecast(conn, today)

    spending = sum(g.actual_cents for g in month.groups if g.category.kind in ("expense", "debt"))
    saving = sum(g.actual_cents for g in month.groups if g.category.kind == "savings")
    ring = donut(
        [(spending, "donut-a"), (saving, "donut-b")],
        total=max(month.outflow_planned_cents, 1),
        value=format_money(month.outflow_actual_cents),
        caption=f"of {format_money(month.outflow_planned_cents)} plan",
    )
    points = [(date.fromisoformat(p.as_of), p.net_cents) for p in history]
    return render_template(
        "dashboard.html",
        net=current,
        change_cents=current.net_cents - previous.net_cents,
        chart=line_chart(points, label="Net worth, last 12 months", height=200),
        stale=stale,
        reminder_days=reminder_days,
        plan=plan,
        month=month,
        totals=totals,
        ring=ring,
        ef=emergency,
        funds=funds,
        behind=[f for f in funds if f.on_track(today) is False],
        debt_total=sum(loan.balance_cents for loan in all_loans),
        debt_free=debt_free_date(all_loans, today),
        loan_count=len(all_loans),
        loans=[loan for loan in all_loans if loan.balance_cents > 0],
        cash_accounts=cash_accounts,
        late_charges=late_charges,
        recovery_missing=not current_store().has_recovery_code,
        upcoming=upcoming,
        promo_alerts=[
            (loan, check) for loan in all_loans
            if (check := loans.promo_check(loan, today)) and not check.on_track
        ],
        invest_total=sum(h.value_cents or 0 for h in holdings),
        invest_gain=sum(h.gain_cents for h in holdings if h.gain_cents is not None),
        refreshed=refreshed,
        today=today,
        # A personal picture, if the user put one in static/ (git-ignored, not part of the app).
        avatar=(Path(current_app.static_folder) / "avatar.gif").is_file(),
        home_video=(video := _home_video()),
        # Changes when the file is replaced, so a cached older video isn't shown.
        home_video_v=_file_version(video) if video else 0,
    )


HOME_VIDEOS = ("home-bg.webm", "home-bg.mp4")


def _home_video() -> str | None:
    """A personal background video for the home page, if the user put one in static/
    (git-ignored, not part of the app)."""
    folder = Path(current_app.static_folder)
    return next((name for name in HOME_VIDEOS if (folder / name).is_file()), None)


def _file_version(name: str) -> int:
    path = Path(current_app.static_folder) / name
    return int(path.stat().st_mtime) if path.is_file() else 0


@bp.post("/log/loan-payment")
def log_loan_payment():
    form = request.form
    try:
        account_id = forms.integer(form, "account_id", "Loan", required=True)
        with current_store().write() as conn:
            before = loan_balances(conn)
            loans.record_payment(
                conn,
                account_id,
                paid_on=forms.day(form, "paid_on", "Date", default=date.today()),
                amount_cents=forms.money(form, "amount", "Amount"),
            )
            flash("Loan payment logged; the balance is updated.", "info")
            celebrate_payoffs(conn, before)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("dashboard.index"))


@bp.post("/log/transfer")
def log_transfer():
    form = request.form
    try:
        to_account_id = forms.integer(form, "to_account_id", "To account", required=True)
        from_account_id = forms.integer(form, "from_account_id", "From account")
        fund_id = forms.integer(form, "fund_id", "Sinking fund")
        amount = forms.money(form, "amount", "Amount")
        with current_store().write() as conn:
            nw.record_transfer(
                conn,
                to_account_id=to_account_id,
                from_account_id=from_account_id,
                amount_cents=amount,
                on=forms.day(form, "on", "Date", default=date.today()),
            )
            if fund_id is not None:
                goals.fund_transfer(
                    conn, fund_id, amount,
                    to_account_id=to_account_id, from_account_id=from_account_id,
                )
        flash("Transfer logged; balances are updated." if fund_id is None
              else "Transfer logged; balances and the sinking fund are updated.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("dashboard.index"))
