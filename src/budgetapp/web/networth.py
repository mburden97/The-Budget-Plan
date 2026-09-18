"""Accounts, balance check-ins and net worth history."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from budgetapp import networth as nw
from budgetapp import settings
from budgetapp.charts import line_chart
from budgetapp.web import current_store, forms
from budgetapp.web.loans import celebrate_payoffs, loan_balances

bp = Blueprint("networth", __name__, url_prefix="/networth")


@bp.get("/")
def index():
    today = date.today()
    show_archived = request.args.get("archived") == "1"
    with current_store().read() as conn:
        everything = nw.list_accounts(conn, include_archived=True)
        current = nw.net_worth(conn, today)
        previous = nw.net_worth(conn, today - timedelta(days=30))
        history = nw.history(conn, today=today)
        reminder_days = settings.get_int(conn, "networth_reminder_days", 30)
        stale = nw.stale_accounts(conn, days=reminder_days, today=today)
        auto_ids = nw.auto_valued_account_ids(conn)
    points = [(date.fromisoformat(p.as_of), p.net_cents) for p in history]
    accounts = [a for a in everything if show_archived or not a.archived]
    return render_template(
        "networth.html",
        show_archived=show_archived,
        archived_count=sum(a.archived for a in everything),
        assets=[a for a in accounts if not a.is_liability],
        liabilities=[a for a in accounts if a.is_liability],
        current=current,
        change_cents=current.net_cents - previous.net_cents,
        chart=line_chart(points, label="Net worth over time"),
        stale=stale,
        reminder_days=reminder_days,
        auto_ids=auto_ids,
        today=today,
        account_types=nw.ACCOUNT_TYPES,
    )


@bp.post("/accounts")
def add_account():
    form = request.form
    try:
        account_type = form.get("type", "")
        if account_type == "loan":
            raise ValueError("Add loans on the Loans page so their payoff can be tracked.")
        name = forms.text(form, "name", "Name", required=True)
        institution = forms.text(form, "institution", "Institution")
        balance = forms.money(form, "balance", "Balance", required=False, allow_negative=True)
        with current_store().write() as conn:
            account_id = nw.add_account(
                conn, name=name, type=account_type, institution=institution
            )
            if balance is not None:
                nw.record_balance(conn, account_id, balance)
    except ValueError as exc:
        flash(str(exc), "error")
    if form.get("next") == "brokerage":  # fixed allow-list, not an open redirect
        return redirect(url_for("brokerage.index"))
    return redirect(url_for("networth.index"))


@bp.post("/checkin")
def checkin():
    form = request.form
    try:
        as_of = forms.day(form, "as_of", "Date", default=date.today())
        updates: dict[int, int] = {}
        for key in form:
            if key.startswith("balance-") and form[key].strip():
                account_id = int(key.removeprefix("balance-"))
                updates[account_id] = forms.money(form, key, "Balance", allow_negative=True)
        if not updates:
            raise ValueError("Enter at least one new balance.")
        with current_store().write() as conn:
            before = loan_balances(conn)
            known = {a.id for a in nw.list_accounts(conn)}
            for account_id, cents in updates.items():
                if account_id in known:
                    nw.record_balance(conn, account_id, cents, as_of)
            flash(f"Saved {len(updates)} balance(s) as of {as_of}.", "info")
            celebrate_payoffs(conn, before)
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("networth.index"))


@bp.get("/accounts/<int:account_id>")
def account(account_id: int):
    with current_store().read() as conn:
        try:
            acct = nw.get_account(conn, account_id)
        except LookupError:
            abort(404)
        history = nw.snapshots(conn, account_id)
        auto = account_id in nw.auto_valued_account_ids(conn)
    return render_template(
        "account.html", account=acct, history=history, auto=auto, today=date.today()
    )


@bp.post("/accounts/<int:account_id>")
def update_account(account_id: int):
    form = request.form
    try:
        fields = {
            "name": forms.text(form, "name", "Name", required=True),
            "institution": forms.text(form, "institution", "Institution"),
            "notes": forms.text(form, "notes", "Notes", max_len=500),
            "include_in_net_worth": forms.checkbox(form, "include_in_net_worth"),
            "emergency_fund": forms.checkbox(form, "emergency_fund"),
            "archived": forms.checkbox(form, "archived"),
            "apy": forms.percent(form, "apy", "APY") if form.get("apy", "").strip() else None,
        }
        with current_store().write() as conn:
            nw.update_account(conn, account_id, **fields)
        flash("Account saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("networth.account", account_id=account_id))


@bp.post("/accounts/<int:account_id>/balances")
def record_balance(account_id: int):
    form = request.form
    try:
        cents = forms.money(form, "balance", "Balance", allow_negative=True)
        as_of = forms.day(form, "as_of", "Date", default=date.today())
        with current_store().write() as conn:
            nw.get_account(conn, account_id)
            before = loan_balances(conn)
            nw.record_balance(conn, account_id, cents, as_of)
            celebrate_payoffs(conn, before)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("networth.account", account_id=account_id))


@bp.post("/snapshots/<int:snapshot_id>/delete")
def delete_snapshot(snapshot_id: int):
    try:
        with current_store().write() as conn:
            account_id = nw.delete_snapshot(conn, snapshot_id)
    except LookupError:
        abort(404)
    return redirect(url_for("networth.account", account_id=account_id))


@bp.post("/accounts/<int:account_id>/delete")
def delete_account(account_id: int):
    try:
        with current_store().write() as conn:
            nw.delete_account(conn, account_id)
    except LookupError:
        abort(404)
    flash("Account deleted.", "info")
    return redirect(url_for("networth.index"))
