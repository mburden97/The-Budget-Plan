"""Brokerage and crypto: holdings, price refresh, allocation."""

from __future__ import annotations

from datetime import date, datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from budgetapp import brokerage, quotes
from budgetapp import networth as nw
from budgetapp import settings as prefs
from budgetapp.money import parse_percent
from budgetapp.web import current_store, forms

bp = Blueprint("brokerage", __name__, url_prefix="/brokerage")


@bp.get("/")
def index():
    with current_store().read() as conn:
        holdings = brokerage.list_holdings(conn)
        accounts = nw.list_accounts(conn, types=nw.INVESTMENT_TYPES)
        targets = brokerage.get_targets(conn)
        refreshed = prefs.get(conn, "prices_refreshed_at")
        dividends_refreshed = prefs.get(conn, "dividends_refreshed_at")
    groups = [(a, [h for h in holdings if h.account_id == a.id]) for a in accounts]
    valued = [h for h in holdings if h.value_cents is not None]
    with_basis = [h for h in valued if h.cost_basis_cents is not None]
    return render_template(
        "brokerage.html",
        groups=groups,
        accounts=accounts,
        total_value=sum(h.value_cents for h in valued),
        total_cost=sum(h.cost_basis_cents for h in with_basis),
        total_gain=sum(h.gain_cents for h in with_basis),
        unpriced=[h for h in holdings if h.value_cents is None],
        dividends=brokerage.dividends(holdings),
        allocation=brokerage.allocation(holdings, targets),
        targets=targets,
        refreshed=refreshed,
        dividends_refreshed=dividends_refreshed,
        asset_classes=brokerage.ASSET_CLASSES,
        quote_sources=brokerage.QUOTE_SOURCES,
        investment_types={t: nw.ACCOUNT_TYPES[t] for t in nw.INVESTMENT_TYPES},
    )


@bp.post("/refresh")
def refresh():
    store = current_store()
    with store.read() as conn:
        requests = brokerage.quote_requests(conn)
        keys = {name: prefs.get(conn, name) or "" for name in quotes.PROVIDER_KEYS}
    if not requests:
        flash("Nothing to refresh: no holdings use market or crypto prices.", "info")
        return redirect(url_for("brokerage.index"))

    # Network calls happen outside the store lock so the UI stays responsive.
    fetch = current_app.config.get("QUOTE_FETCH")
    result = quotes.refresh(requests, keys=keys, fetch=fetch)

    with store.write() as conn:
        updated = brokerage.apply_prices(conn, result, date.today())
        stamp = datetime.now().isoformat(sep=" ", timespec="minutes")
        prefs.put(conn, "prices_refreshed_at", stamp)
    flash(f"Updated prices for {updated} holding(s).", "info")
    for req, error in result.errors.items():
        flash(f"{req.symbol}: {error}", "error")
    return redirect(url_for("brokerage.index"))


@bp.post("/dividends/refresh")
def refresh_dividends():
    """Look up each holding's trailing dividend yield. Symbols only, same providers as prices."""
    store = current_store()
    with store.read() as conn:
        requests = brokerage.dividend_requests(conn)
        keys = {name: prefs.get(conn, name) or "" for name in quotes.PROVIDER_KEYS}
    if not requests:
        flash("Nothing to look up: no holdings use market prices.", "info")
        return redirect(url_for("brokerage.index"))

    replace = bool(request.form.get("replace"))
    fetch = current_app.config.get("QUOTE_FETCH")
    result = quotes.dividend_yields(requests, keys=keys, fetch=fetch)
    with store.write() as conn:
        updated, kept = brokerage.apply_dividend_yields(
            conn, result, date.today(), replace_manual=replace
        )
        prefs.put(
            conn, "dividends_refreshed_at", datetime.now().isoformat(sep=" ", timespec="minutes")
        )
    flash(f"Updated dividend yields for {updated} holding(s).", "info")
    if kept:
        flash(f"Left {kept} yield(s) you typed yourself alone.", "info")
    for req, error in result.errors.items():
        flash(f"{req.symbol}: {error}", "error")
    return redirect(url_for("brokerage.index"))


@bp.post("/holdings")
def add_holding():
    try:
        with current_store().write() as conn:
            brokerage.add_holding(conn, **_holding_fields(request.form, with_account=True))
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("brokerage.index"))


@bp.get("/holdings/<int:holding_id>")
def holding(holding_id: int):
    with current_store().read() as conn:
        try:
            item = brokerage.get_holding(conn, holding_id)
        except LookupError:
            abort(404)
    return render_template(
        "holding.html",
        holding=item,
        asset_classes=brokerage.ASSET_CLASSES,
        quote_sources=brokerage.QUOTE_SOURCES,
    )


@bp.post("/holdings/<int:holding_id>")
def update_holding(holding_id: int):
    try:
        with current_store().write() as conn:
            brokerage.update_holding(conn, holding_id, **_holding_fields(request.form))
        flash("Holding saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("brokerage.holding", holding_id=holding_id))
    except LookupError:
        abort(404)
    return redirect(url_for("brokerage.index"))


@bp.post("/holdings/<int:holding_id>/delete")
def delete_holding(holding_id: int):
    try:
        with current_store().write() as conn:
            brokerage.delete_holding(conn, holding_id)
    except LookupError:
        abort(404)
    return redirect(url_for("brokerage.index"))


@bp.post("/targets")
def targets():
    try:
        values = {}
        for key, label in brokerage.ASSET_CLASSES.items():
            raw = request.form.get(f"target-{key}", "").strip()
            if raw:
                values[key] = parse_percent(raw, label=f"{label} target")
        with current_store().write() as conn:
            brokerage.set_targets(conn, values)
        flash("Allocation targets saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("brokerage.index"))


def _holding_fields(form, *, with_account: bool = False) -> dict:
    price_raw = form.get("price", "").strip()
    fields = {
        "symbol": forms.text(form, "symbol", "Symbol", required=True, max_len=20),
        "name": forms.text(form, "name", "Name"),
        "asset_class": form.get("asset_class", "other"),
        "shares": brokerage.parse_quantity(form.get("shares", ""), "Shares"),
        "cost_basis_cents": forms.money(form, "cost_basis", "Cost basis", required=False),
        "quote_source": form.get("quote_source", "market"),
        "quote_id": forms.text(form, "quote_id", "Price lookup id", max_len=60),
        "price": brokerage.parse_quantity(price_raw, "Price", places=8) if price_raw else None,
        "dividend_yield": (
            parse_percent(form.get("dividend_yield", ""), label="Dividend yield")
            if form.get("dividend_yield", "").strip() else None
        ),
        "reinvest": forms.checkbox(form, "reinvest"),
    }
    if with_account:
        fields["account_id"] = forms.integer(form, "account_id", "Account", required=True)
    return fields
