"""Goals: emergency fund status and sinking funds."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from budgetapp import goals, planning
from budgetapp.web import current_store, forms

bp = Blueprint("goals", __name__, url_prefix="/goals")


@bp.get("/")
def index():
    with current_store().read() as conn:
        funds = goals.list_funds(conn)
        emergency = goals.emergency_fund(conn)
        groups = planning.grouped(conn)
    return render_template(
        "goals.html", funds=funds, ef=emergency, groups=groups, today=date.today()
    )


@bp.post("/funds")
def add_fund():
    try:
        with current_store().write() as conn:
            goals.add_fund(conn, **_fund_fields(request.form))
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("goals.index"))


@bp.post("/funds/<int:fund_id>")
def update_fund(fund_id: int):
    try:
        with current_store().write() as conn:
            goals.update_fund(conn, fund_id, **_fund_fields(request.form))
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("goals.index"))


@bp.post("/funds/<int:fund_id>/delete")
def delete_fund(fund_id: int):
    try:
        with current_store().write() as conn:
            goals.delete_fund(conn, fund_id)
    except LookupError:
        abort(404)
    return redirect(url_for("goals.index"))


def _fund_fields(form) -> dict:
    line_raw = form.get("line_item_id", "").strip()
    try:
        line_item_id = int(line_raw) if line_raw else None
    except ValueError:
        raise ValueError("Choose a budget line.") from None
    return {
        "name": forms.text(form, "name", "Name", required=True),
        "target_cents": forms.money(form, "target", "Target"),
        "due_date": forms.day(form, "due_date", "Due date", required=False),
        "saved_cents": forms.money(form, "saved", "Saved", required=False) or 0,
        "line_item_id": line_item_id,
        "notes": forms.text(form, "notes", "Notes", max_len=200),
    }
