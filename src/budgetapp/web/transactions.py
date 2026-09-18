"""Transactions: monthly list, categorizing (with rules), manual entry, CSV import."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from budgetapp import imports, ofx, planning, transactions
from budgetapp import networth as nw
from budgetapp import subscriptions as subs
from budgetapp.dates import add_months, parse_month
from budgetapp.web import current_store, forms

bp = Blueprint("transactions", __name__, url_prefix="/transactions")

MAX_CSV_BYTES = 5 * 1024 * 1024  # a year of bank rows, and nothing like a scanned statement

IMPORT_ACCOUNT_TYPES = ("cash", "credit_card", "other_asset", "other_liability")
MAX_PENDING_IMPORTS = 3
NEW_LINE = "new"  # the categorize dropdown's "+ New budget line…" choice


@bp.get("/")
def index():
    today = date.today()
    month = parse_month(request.args.get("month"), today)
    show = "uncategorized" if request.args.get("show") == "uncategorized" else None
    with current_store().read() as conn:
        txns = transactions.list_transactions(conn, month, only_uncategorized=bool(show))
        splits = transactions.splits_by_transaction(conn, [t.id for t in txns])
        totals = transactions.month_totals(conn, month)
        groups = planning.grouped(conn)
        accounts = nw.list_accounts(conn)
        rules = transactions.list_rules(conn)
    return render_template(
        "transactions.html",
        txns=txns,
        splits=splits,
        month=month,
        month_q=month.strftime("%Y-%m"),
        prev_q=add_months(month, -1).strftime("%Y-%m"),
        next_q=add_months(month, 1).strftime("%Y-%m"),
        show=show,
        totals=totals,
        groups=groups,
        accounts=accounts,
        rules=rules,
        suggest=transactions.suggest_pattern,
        today=today,
    )


@bp.post("/")
def add():
    form = request.form
    try:
        amount = forms.money(form, "amount", "Amount")
        if form.get("direction") != "in":
            amount = -amount
        line_item_id, excluded = _category_choice(form.get("category", ""))
        account_raw = form.get("account_id", "").strip()
        with current_store().write() as conn:
            transactions.add_transaction(
                conn,
                posted_on=forms.day(form, "posted_on", "Date", default=date.today()),
                description=forms.text(form, "description", "Description", required=True,
                                       max_len=transactions.MAX_DESCRIPTION),
                amount_cents=amount,
                account_id=int(account_raw) if account_raw else None,
                line_item_id=line_item_id,
                excluded=excluded,
            )
        flash("Transaction added.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return _back(form)


@bp.post("/<int:txn_id>/categorize")
def categorize(txn_id: int):
    form = request.form
    if form.get("category") == NEW_LINE:  # "+ New budget line…": name it on its own page
        return redirect(url_for(
            "transactions.new_line", txn_id=txn_id, month=form.get("month"), show=form.get("show")
        ))
    try:
        line_item_id, excluded = _category_choice(form.get("category", ""))
        pattern = form.get("pattern", "").strip() if forms.checkbox(form, "remember") else None
        with current_store().write() as conn:
            others = transactions.categorize(
                conn, txn_id, line_item_id=line_item_id, excluded=excluded,
                remember_pattern=pattern,
            )
        if pattern:
            flash(f"Rule saved; it also categorized {others} other transaction(s).", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return _back(form)


@bp.get("/<int:txn_id>/new-line")
def new_line(txn_id: int):
    """Make a budget line for a transaction that doesn't fit any, and file it there."""
    with current_store().read() as conn:
        try:
            txn = transactions.get_transaction(conn, txn_id)
        except LookupError:
            abort(404)
        categories = planning.list_categories(conn)
    pattern = transactions.suggest_pattern(txn.description)
    wanted = "income" if txn.amount_cents > 0 else "expense"
    default = next(
        (c for c in categories if c.kind == wanted and "flexible" in c.name.lower()),
        next((c for c in categories if c.kind == wanted), categories[0] if categories else None),
    )
    return render_template(
        "new_line.html",
        txn=txn,
        categories=categories,
        default_category=default.id if default else None,
        kinds=planning.KINDS,
        wanted_kind=wanted,
        suggested_name=pattern.title() if pattern else "",
        pattern=pattern,
        frequencies=planning.FREQUENCIES,
        month_q=request.args.get("month", ""),
        show=request.args.get("show") or None,
    )


@bp.post("/<int:txn_id>/new-line")
def create_line(txn_id: int):
    form = request.form
    try:
        new_category = forms.text(form, "new_category", "New category", max_len=60)
        amount = forms.money(form, "amount", "Planned amount", required=False) or 0
        pattern = form.get("pattern", "").strip() if forms.checkbox(form, "remember") else None
        with current_store().write() as conn:
            transactions.get_transaction(conn, txn_id)
            if new_category:
                category_id = planning.add_category(
                    conn, name=new_category, kind=form.get("kind", "expense")
                )
            else:
                category_id = forms.integer(form, "category_id", "Category", required=True)
            line_id = planning.add_line_item(
                conn, category_id=category_id,
                name=forms.text(form, "name", "Line name", required=True),
                amount_cents=amount, frequency=form.get("frequency", "monthly"),
            )
            others = transactions.categorize(
                conn, txn_id, line_item_id=line_id, remember_pattern=pattern
            )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for(
            "transactions.new_line", txn_id=txn_id, month=form.get("month"), show=form.get("show")
        ))
    except LookupError:
        abort(404)
    message = "New budget line added, and this transaction filed under it."
    if pattern:
        message += f" The rule also filed {others} other transaction(s)."
    flash(message, "info")
    return _back(form)


@bp.post("/<int:txn_id>/delete")
def delete(txn_id: int):
    try:
        with current_store().write() as conn:
            transactions.delete_transaction(conn, txn_id)
    except LookupError:
        abort(404)
    return _back(request.form)


@bp.post("/rules/<int:rule_id>/delete")
def delete_rule(rule_id: int):
    try:
        with current_store().write() as conn:
            transactions.delete_rule(conn, rule_id)
    except LookupError:
        abort(404)
    return _back(request.form)


# ---------------------------------------------------------------- splits
@bp.get("/<int:txn_id>/split")
def split(txn_id: int):
    month_q, show = _list_args(request.args)
    with current_store().read() as conn:
        try:
            txn = transactions.get_transaction(conn, txn_id)
        except LookupError:
            abort(404)
        parts = transactions.list_splits(conn, txn_id)
        groups = planning.grouped(conn)
    return render_template(
        "split.html",
        txn=txn,
        parts=parts,
        remainder=txn.amount_cents - sum(p.amount_cents for p in parts),
        groups=groups,
        pattern=transactions.suggest_pattern(txn.description),
        month_q=month_q,
        show=show,
    )


@bp.post("/<int:txn_id>/splits")
def add_split(txn_id: int):
    form = request.form
    month_q, show = _list_args(form)
    try:
        line_item_id = forms.integer(form, "line_item_id", "Budget line", required=True)
        amount = forms.money(form, "amount", "Split amount")
        note = forms.text(form, "note", "Label", max_len=60)
        with current_store().write() as conn:
            txn = transactions.get_transaction(conn, txn_id)
            transactions.add_split(
                conn, txn_id, line_item_id=line_item_id, amount_cents=amount, note=note
            )
            matched = None
            if forms.checkbox(form, "every"):
                if txn.line_item_id is None:
                    raise ValueError("Give this charge a category first, then split every one.")
                rule = transactions.add_rule(
                    conn, pattern=form.get("pattern", ""), line_item_id=txn.line_item_id,
                    split_line_item_id=line_item_id, split_amount_cents=amount, split_note=note,
                )
                matched = transactions.apply_rule_everywhere(conn, rule)
        if matched is None:
            flash("Split saved.", "info")
        else:
            flash(f"Split saved, and a rule now splits every matching charge ({matched} so far).",
                  "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("transactions.split", txn_id=txn_id, month=month_q, show=show))


@bp.post("/splits/<int:split_id>/delete")
def delete_split(split_id: int):
    month_q, show = _list_args(request.form)
    try:
        with current_store().write() as conn:
            txn_id = transactions.delete_split(conn, split_id)
    except LookupError:
        abort(404)
    return redirect(url_for("transactions.split", txn_id=txn_id, month=month_q, show=show))


def _list_args(source) -> tuple[str, str | None]:
    """The month and filter of the list a page was opened from (re-validated)."""
    month = parse_month(source.get("month"), date.today()).strftime("%Y-%m")
    return month, "uncategorized" if source.get("show") == "uncategorized" else None


# ---------------------------------------------------------------- bank categories
@bp.get("/bank-categories")
def bank_categories():
    with current_store().read() as conn:
        cats = transactions.bank_categories(conn)
        groups = planning.grouped(conn)
    lines = [item for g in groups if g.category.kind != "income" for item in g.items]
    suggestions = {
        c.name: transactions.suggest_line_item(c.name, lines) for c in cats if not c.mapped
    }
    return render_template(
        "bank_categories.html", cats=cats, groups=groups, suggestions=suggestions
    )


@bp.post("/bank-categories")
def save_bank_categories():
    form = request.form
    choices: dict[str, tuple[int | None, bool] | None] = {}
    try:
        for key in form:
            if not key.startswith("name-"):
                continue
            name = form[key].strip()[:60]
            value = form.get(f"map-{key.removeprefix('name-')}", "")
            if name:
                choices[name] = _category_choice(value) if value else None
        with current_store().write() as conn:
            mapped = transactions.set_bank_category_map(conn, choices)
            applied = transactions.apply_bank_categories(conn)
        flash(f"{mapped} bank categories matched; categorized {applied} transaction(s).", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("transactions.bank_categories"))


# ---------------------------------------------------------------- CSV import
@bp.get("/import")
def import_form():
    with current_store().read() as conn:
        accounts = nw.list_accounts(conn, types=IMPORT_ACCOUNT_TYPES)
    return render_template("import.html", accounts=accounts)


def stage_import(store, *, account_id: int, filename: str, raw: bytes) -> str:
    """Parse a CSV or OFX/QFX file and hold it for the preview step; returns its token.

    Shared by the Transactions upload form and the import button on a file filed under
    Documents. The rows are held in memory only (store.scratch, cleared on lock). An OFX
    file's columns are fixed, so it arrives with its mapping already chosen.
    """
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError(f"That file is over {MAX_CSV_BYTES // (1024 * 1024)} MB.")
    is_ofx = ofx.looks_like_ofx(raw)
    parsed = ofx.parse(raw) if is_ofx else imports.parse_csv(raw)
    with store.read() as conn:
        try:
            nw.get_account(conn, account_id)
        except LookupError:
            raise ValueError("Choose an account.") from None
    pending = store.scratch.setdefault("imports", {})
    while len(pending) >= MAX_PENDING_IMPORTS:
        pending.pop(next(iter(pending)))
    token = secrets.token_urlsafe(16)
    pending[token] = {
        "account_id": account_id,
        "parsed": parsed,
        "filename": filename[:80],
        "mapping": ofx.MAPPING if is_ofx else None,
        "kind": "ofx" if is_ofx else "csv",
    }
    return token


@bp.post("/import")
def upload():
    store = current_store()
    try:
        account_id = forms.integer(request.form, "account_id", "Account", required=True)
        upload_file = request.files.get("file")
        if upload_file is None or not upload_file.filename:
            raise ValueError("Choose a CSV, OFX or QFX file to import.")
        token = stage_import(
            store, account_id=account_id, filename=upload_file.filename,
            raw=upload_file.read(MAX_CSV_BYTES + 1),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("transactions.import_form"))
    return redirect(url_for("transactions.map_import", token=token))


@bp.get("/import/<token>")
def map_import(token: str):
    pending = _pending(token)
    parsed: imports.ParsedCsv = pending["parsed"]
    with current_store().read() as conn:
        account = nw.get_account(conn, pending["account_id"])
        profile = imports.get_profile(conn, pending["account_id"])
    remembered = bool(profile and profile.fits(parsed))
    mapping = pending["mapping"] or (
        profile if remembered else imports.guess_mapping(parsed.headers, parsed.rows)
    )
    flip_suggested = False
    if not pending["mapping"] and not remembered and account.type == "credit_card":
        try:
            rows, _ = imports.build_rows(parsed, mapping)
        except ValueError:
            rows = []
        if imports.suggest_flip(rows):
            mapping, flip_suggested = replace(mapping, flip_sign=True), True
    sample = imports.ParsedCsv(parsed.headers, parsed.rows[:8])
    date_note = None
    try:
        # The date order comes from the whole file, even though only a sample is shown.
        order = imports.date_order([row[mapping.date_col] for row in parsed.rows])
        if mapping.date_format == "auto" and order.day_first:
            date_note = ("Dates are read day first (DD/MM), because some days in this file "
                         "are over 12.")
        elif mapping.date_format == "auto" and order.ambiguous:
            date_note = ("Every date in this file reads either way round, so they're taken "
                         "month first (MM/DD). Choose DD/MM below if that's wrong.")
        preview, problems = imports.build_rows(sample, mapping, day_first=order.day_first)
    except (ValueError, IndexError) as exc:
        preview, problems = [], [str(exc) if isinstance(exc, ValueError)
                                 else "Choose columns from this file."]
    return render_template(
        "import_map.html",
        token=token,
        account=account,
        filename=pending["filename"],
        parsed=parsed,
        mapping=mapping,
        preview=preview,
        problems=problems,
        remembered=remembered and not pending["mapping"],
        flip_suggested=flip_suggested,
        date_formats=imports.DATE_FORMATS,
        date_note=date_note,
        is_ofx=pending.get("kind") == "ofx",
    )


@bp.post("/import/<token>")
def run_import(token: str):
    pending = _pending(token)
    form = request.form
    try:
        mapping = imports.Mapping(
            date_col=_column(form, "date_col", required=True),
            description_col=_column(form, "description_col", required=True),
            amount_col=_column(form, "amount_col"),
            debit_col=_column(form, "debit_col"),
            credit_col=_column(form, "credit_col"),
            date_format=form.get("date_format", "auto"),
            flip_sign=forms.checkbox(form, "flip_sign"),
            category_col=_column(form, "category_col"),
            id_col=_column(form, "id_col"),
        )
        if form.get("action") == "preview":
            pending["mapping"] = mapping
            return redirect(url_for("transactions.map_import", token=token))
        rows, problems = imports.build_rows(pending["parsed"], mapping)
        if not rows:
            raise ValueError("No transactions could be read with these columns.")
        with current_store().write() as conn:
            summary = imports.import_rows(conn, pending["account_id"], rows)
            if pending.get("kind") != "ofx":  # OFX columns are fixed; keep the CSV profile
                imports.save_profile(conn, pending["account_id"], mapping)
            late = len(subs.charges_after_cancel(conn, summary.ids))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("transactions.map_import", token=token))
    current_store().scratch.get("imports", {}).pop(token, None)
    flash(
        f"Imported {summary.added} transaction(s): {summary.categorized} categorized "
        f"automatically, {summary.duplicates} skipped as already imported.",
        "info",
    )
    if summary.categories_filled:
        flash(
            f"Added bank categories to {summary.categories_filled} earlier transaction(s).", "info"
        )
    if late:
        flash(f"{late} imported charge(s) came from a subscription you cancelled; "
              "see the Subscriptions tab.", "warn")
    if problems:
        flash(f"{len(problems)} row(s) couldn't be read: " + "; ".join(problems[:3]), "warn")
    month = summary.latest.strftime("%Y-%m") if summary.latest else None
    return redirect(url_for("transactions.index", month=month))


# ---------------------------------------------------------------- helpers
def _category_choice(value: str) -> tuple[int | None, bool]:
    """Form value -> (line_item_id, excluded). "" = uncategorized, "x" = not budgeted."""
    if value == "x":
        return None, True
    if not value:
        return None, False
    try:
        return int(value), False
    except ValueError:
        raise ValueError("Choose a category.") from None


def _column(form, key: str, *, required: bool = False) -> int | None:
    raw = form.get(key, "").strip()
    if not raw:
        if required:
            raise ValueError("Choose the date and description columns.")
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError("Choose columns from this file.") from None


def _pending(token: str) -> dict:
    item = current_store().scratch.get("imports", {}).get(token)
    if item is None:
        flash("That import expired (the app was locked). Upload the file again.", "warn")
        abort(redirect(url_for("transactions.import_form")))
    return item


def _back(form):
    """Return to the list the form came from (month/filter are re-validated)."""
    month = parse_month(form.get("month"), date.today()).strftime("%Y-%m")
    show = "uncategorized" if form.get("show") == "uncategorized" else None
    return redirect(url_for("transactions.index", month=month, show=show))
