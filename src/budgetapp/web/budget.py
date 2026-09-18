"""Budget plan views: edit amounts, add/remove line items and categories."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from budgetapp import planning, transactions
from budgetapp import subscriptions as subs
from budgetapp.dates import add_months, parse_month
from budgetapp.money import parse_money
from budgetapp.web import current_store, forms

bp = Blueprint("budget", __name__, url_prefix="/budget")


@bp.get("/actual")
def actual():
    month = parse_month(request.args.get("month"), date.today())
    with current_store().read() as conn:
        result = transactions.budget_vs_actual(conn, month)
    return render_template(
        "actual.html",
        result=result,
        kinds=planning.KINDS,
        month_q=month.strftime("%Y-%m"),
        prev_q=add_months(month, -1).strftime("%Y-%m"),
        next_q=add_months(month, 1).strftime("%Y-%m"),
    )


@bp.get("/subscriptions")
def subscriptions():
    today = date.today()
    year_ago = today - timedelta(days=365)
    with current_store().read() as conn:
        lines = subs.subscription_lines(conn)
        items = subs.itemize(conn)
        maybe = subs.candidates(conn)
        cancelled = subs.list_cancelled(conn)
        late = subs.charges_after_cancel(conn)
    by_pattern = {s.pattern.upper(): s for s in items}
    gone = {c.pattern.upper() for c in cancelled}
    live = [s for s in items if s.pattern.upper() not in gone]
    active = sorted(
        (s for s in live if s.is_active(today)),
        key=lambda s: -(s.monthly_cents or s.typical_cents),
    )
    stopped = sorted(
        (s for s in live if not s.is_active(today)), key=lambda s: s.last.posted_on, reverse=True
    )
    monthly_total = sum(s.monthly_cents or 0 for s in active)
    return render_template(
        "subscriptions.html",
        lines=lines,
        active=active,
        stopped=stopped,
        maybe=maybe,
        cancelled=[(c, by_pattern.get(c.pattern.upper())) for c in cancelled],
        cancelled_monthly=sum(by_pattern[p].monthly_cents or 0 for p in gone if p in by_pattern),
        late=late,
        late_patterns={charge.pattern for charge in late},
        today=today,
        year_ago=year_ago,
        monthly_total=monthly_total,
        active_12=sum(s.spent_since(year_ago) for s in active),
        planned=sum(line.monthly_cents for line in lines),
        spent_12=sum(s.spent_since(year_ago) for s in items),
    )


@bp.post("/subscriptions/adopt")
def adopt_subscription():
    pattern = request.form.get("pattern", "").strip()
    try:
        with current_store().write() as conn:
            lines = subs.subscription_lines(conn)
            if not lines:
                raise ValueError("Add a budget line with Subscriptions in its name first.")
            moved = transactions.recategorize_matching(conn, pattern, lines[0].id)
        flash(f"Moved {moved} charge(s) to {lines[0].name}; future charges will follow.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("budget.subscriptions"))


@bp.post("/subscriptions/release")
def release_subscription():
    pattern = request.form.get("pattern", "").strip()
    try:
        with current_store().write() as conn:
            target = subs.default_other_line(conn)
            if target is None:
                raise ValueError("Add an expense budget line to move these charges to.")
            moved = subs.release(conn, pattern, target.id)
        flash(f"Moved {moved} charge(s) to {target.name}.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("budget.subscriptions"))


@bp.post("/subscriptions/cancel")
def cancel_subscription():
    form = request.form
    try:
        with current_store().write() as conn:
            subs.cancel(
                conn, form.get("pattern", ""), form.get("name", ""),
                forms.day(form, "on", "Cancelled on", default=date.today()),
            )
        flash("Saved as cancelled; any charge after that date gets flagged.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("budget.subscriptions"))


@bp.post("/subscriptions/uncancel")
def uncancel_subscription():
    with current_store().write() as conn:
        subs.uncancel(conn, request.form.get("pattern", ""))
    flash("Moved back to your subscriptions.", "info")
    return redirect(url_for("budget.subscriptions"))


@bp.post("/categories/<int:category_id>/essential")
def toggle_essential(category_id: int):
    try:
        with current_store().write() as conn:
            planning.set_essential(conn, category_id, request.form.get("essential") == "1")
    except LookupError:
        abort(404)
    return _back()


@bp.get("/")
def index():
    with current_store().read() as conn:
        categories = planning.list_categories(conn)
        groups = planning.grouped(conn)
        mode = planning.budget_mode(conn)
    return render_template(
        "budget.html",
        categories=categories,
        groups=groups,
        mode=mode,
        summary=planning.summarize(groups),
        frequencies=planning.FREQUENCIES,
        kinds=planning.KINDS,
    )


@bp.post("/items")
def add_item():
    try:
        fields = _item_fields()
        with current_store().write() as conn:
            planning.add_line_item(conn, **fields)
    except ValueError as exc:
        flash(str(exc), "error")
    return _back()


@bp.post("/items/<int:item_id>")
def update_item(item_id: int):
    try:
        fields = _item_fields()
        with current_store().write() as conn:
            planning.update_line_item(conn, item_id, **fields)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return _back()


@bp.post("/items/<int:item_id>/delete")
def delete_item(item_id: int):
    try:
        with current_store().write() as conn:
            planning.delete_line_item(conn, item_id)
    except LookupError:
        abort(404)
    return _back()


@bp.post("/categories")
def add_category():
    try:
        with current_store().write() as conn:
            planning.add_category(
                conn, name=request.form.get("name", ""), kind=request.form.get("kind", "")
            )
    except ValueError as exc:
        flash(str(exc), "error")
    return _back()


@bp.post("/categories/<int:category_id>/delete")
def delete_category(category_id: int):
    try:
        with current_store().write() as conn:
            planning.delete_category(conn, category_id)
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return _back()


def _item_fields() -> dict:
    form = request.form
    try:
        category_id = int(form.get("category_id", ""))
    except ValueError:
        raise ValueError("Choose a category.") from None
    return {
        "category_id": category_id,
        "name": form.get("name", ""),
        "amount_cents": parse_money(form.get("amount", "")),
        "frequency": form.get("frequency", "monthly"),
        "notes": form.get("notes", ""),
    }


def _back():
    return redirect(url_for("budget.index"))
