"""Preferences, price API keys and passphrase change."""

from __future__ import annotations

import re
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from budgetapp import planning, quotes
from budgetapp import settings as prefs
from budgetapp.web import ALLOW_BLANK_PASSPHRASE, current_store, forms
from budgetapp.web.auth import MIN_PASSPHRASE, passphrase_problem

bp = Blueprint("settings", __name__, url_prefix="/settings")

GENERAL = ("networth_reminder_days", "emergency_fund_months")
_API_KEY = re.compile(r"[A-Za-z0-9_\-.]{8,128}")


@bp.get("/")
def index():
    with current_store().read() as conn:
        values = {key: prefs.get(conn, key) for key in GENERAL}
        keys = {name: _mask(prefs.get(conn, name)) for name in quotes.PROVIDER_KEYS}
        mode = planning.budget_mode(conn)
        groups = planning.grouped(conn)
    return render_template(
        "settings.html",
        income_lines=_income_lines(groups),
        has_recovery=current_store().has_recovery_code,
        recovery_created=current_store().recovery_code_created(),
        plan=planning.summarize(groups),
        frequencies=planning.FREQUENCIES,
        mode=mode,
        values=values,
        keys=keys,
        providers=quotes.PROVIDER_KEYS,
        min_length=MIN_PASSPHRASE,
        has_passphrase=not current_store().passphrase_blank,
        allow_blank=ALLOW_BLANK_PASSPHRASE,
    )


@bp.post("/general")
def general():
    form = request.form
    try:
        reminder = forms.integer(
            form, "networth_reminder_days", "Reminder interval", required=True, lo=1, hi=365
        )
        months = forms.integer(
            form, "emergency_fund_months", "Emergency fund target", required=True, lo=1, hi=24
        )
        with current_store().write() as conn:
            prefs.put(conn, "networth_reminder_days", str(reminder))
            prefs.put(conn, "emergency_fund_months", str(months))
        flash("Settings saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("settings.index"))


@bp.post("/income")
def income():
    """Base income: the amount and frequency of each income line in the budget plan."""
    form = request.form
    try:
        with current_store().write() as conn:
            for item in _income_lines(planning.grouped(conn)):
                if f"amount-{item.id}" not in form:
                    continue
                planning.update_line_item(
                    conn, item.id, category_id=item.category_id, name=item.name,
                    amount_cents=forms.money(form, f"amount-{item.id}", item.name),
                    frequency=form.get(f"frequency-{item.id}", item.frequency), notes=item.notes,
                )
            left = planning.summarize(planning.grouped(conn)).unallocated_cents
        flash("Income saved." + (" Left to assign changed: rebalance on the Budget page."
                                 if left else ""), "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("settings.index", _anchor="income"))


def _income_lines(groups: list[planning.Group]) -> list[planning.LineItem]:
    return [item for g in groups if g.category.kind == "income" for item in g.items]


@bp.post("/cat")
def cat():
    show = forms.checkbox(request.form, "show_cat")
    with current_store().write() as conn:
        prefs.put(conn, "show_cat", "on" if show else "off")
    flash("The cat is back." if show else "The cat has gone off to nap somewhere else.", "info")
    return redirect(url_for("settings.index"))


@bp.post("/budget-mode")
def budget_mode():
    mode = request.form.get("budget_mode", "")
    if mode in planning.BUDGET_MODES:
        with current_store().write() as conn:
            prefs.put(conn, "budget_mode", mode)
        flash(f"Budget now uses: {planning.BUDGET_MODES[mode]}.", "info")
    return redirect(url_for("settings.index"))


@bp.post("/api-keys")
def api_keys():
    form = request.form
    changes: dict[str, str | None] = {}
    for name, (provider, _url, _note) in quotes.PROVIDER_KEYS.items():
        if forms.checkbox(form, f"clear-{name}"):
            changes[name] = None
        elif value := form.get(name, "").strip():
            if not _API_KEY.fullmatch(value):
                flash(f"That doesn't look like a {provider} key.", "error")
                return redirect(url_for("settings.index"))
            changes[name] = value
    with current_store().write() as conn:
        for name, value in changes.items():
            if value is None:
                prefs.delete(conn, name)
            else:
                prefs.put(conn, name, value)
    flash("API keys updated." if changes else "No changes.", "info")
    return redirect(url_for("settings.index"))


@bp.post("/passphrase")
def passphrase():
    store = current_store()
    form = request.form
    new = form.get("new", "")
    problem = passphrase_problem(new, form.get("confirm", ""))
    if not store.verify_passphrase(form.get("current", "")):  # "" when none is set
        flash("Current passphrase is incorrect.", "error")
    elif problem:
        flash(problem, "error")
    else:
        store.change_passphrase(new)
        if new:
            flash("Passphrase set. Older backups still open with the previous one.", "info")
        else:
            flash("Passphrase removed; the app now opens without asking.", "info")
    return redirect(url_for("settings.index"))


@bp.post("/recovery-code")
def recovery_code():
    """Make (or replace) the recovery code and show it once. It goes straight into this
    response: never into the session cookie, a flash message or a redirect."""
    store = current_store()
    if not store.verify_passphrase(request.form.get("current", "")):
        flash("Current passphrase is incorrect.", "error")
        return redirect(url_for("settings.index", _anchor="recovery"))
    code = store.create_recovery_code()
    return render_template("recovery_code.html", code=code, today=date.today())


@bp.post("/recovery-code/remove")
def remove_recovery_code():
    store = current_store()
    if not store.verify_passphrase(request.form.get("current", "")):
        flash("Current passphrase is incorrect.", "error")
    else:
        store.remove_recovery_code()
        flash("Recovery code removed. Only your passphrase opens the vault now.", "info")
    return redirect(url_for("settings.index", _anchor="recovery"))


def _mask(value: str | None) -> str:
    return f"saved (ends in …{value[-4:]})" if value else "not set"
