"""Create, unlock and lock the vault."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from budgetapp import planning
from budgetapp.vault import BadPassphrase, BadRecoveryCode, NoRecoveryCode, VaultError
from budgetapp.web import (
    blank_allowed,
    blank_unlock,
    current_store,
    forms,
    locked_page,
    start_session,
)

bp = Blueprint("auth", __name__)

MIN_PASSPHRASE = 12


def passphrase_problem(passphrase: str, confirm: str) -> str | None:
    """Why a new passphrase can't be used, or None. Blank is allowed while that mode is on."""
    if passphrase != confirm:
        return "Passphrases don't match."
    if passphrase == "" and blank_allowed():
        return None
    if len(passphrase) < MIN_PASSPHRASE:
        return f"Use at least {MIN_PASSPHRASE} characters. A few random words works well."
    return None


@bp.get("/setup")
def setup():
    if current_store().exists:
        return redirect(url_for("auth.unlock"))
    return render_template(
        "setup.html", min_length=MIN_PASSPHRASE, allow_blank=blank_allowed()
    )


@bp.post("/setup")
def setup_post():
    store = current_store()
    if store.exists:
        return redirect(url_for("auth.unlock"))
    passphrase = request.form.get("passphrase", "")
    problem = passphrase_problem(passphrase, request.form.get("confirm", ""))
    if problem:
        flash(problem, "error")
        return redirect(url_for("auth.setup"))
    store.create(passphrase)
    if forms.checkbox(request.form, "starter_lines"):
        with store.write() as conn:
            planning.add_starter_lines(conn)
    start_session(store)
    if passphrase:
        flash("Vault created. Keep your passphrase safe, and consider a recovery code in "
              "Settings: without one or the other the vault can't be opened.", "info")
    else:
        flash("Vault created without a passphrase. Set one in Settings when you're ready.", "info")
    return redirect(url_for("dashboard.index"))


@bp.get("/unlock")
def unlock():
    store = current_store()
    if locked_page(store) != "auth.unlock":
        return redirect(url_for(locked_page(store)))
    if blank_unlock(store):
        start_session(store)
        return redirect(url_for("dashboard.index"))
    return render_template("unlock.html", recovery=store.recovery_available())


@bp.post("/unlock")
def unlock_post():
    store = current_store()
    try:
        store.unlock(request.form.get("passphrase", ""))
    except BadPassphrase:
        flash("Incorrect passphrase.", "error")
        return redirect(url_for("auth.unlock"))
    except (VaultError, OSError) as exc:
        flash(f"Could not open the vault: {exc}", "error")
        return redirect(url_for("auth.unlock"))
    start_session(store)
    return redirect(url_for("dashboard.index"))


@bp.get("/set-passphrase")
def set_passphrase():
    """A vault made in the old no-passphrase mode gets its first passphrase here."""
    store = current_store()
    if locked_page(store) != "auth.set_passphrase" and not store.passphrase_blank:
        return redirect(url_for("auth.unlock" if store.exists else "auth.setup"))
    return render_template("set_passphrase.html", min_length=MIN_PASSPHRASE)


@bp.post("/set-passphrase")
def set_passphrase_post():
    store = current_store()
    if locked_page(store) != "auth.set_passphrase" and not store.passphrase_blank:
        return redirect(url_for("auth.unlock" if store.exists else "auth.setup"))
    passphrase = request.form.get("passphrase", "")
    problem = passphrase_problem(passphrase, request.form.get("confirm", ""))
    if problem:
        flash(problem, "error")
        return redirect(url_for("auth.set_passphrase"))
    try:
        store.set_first_passphrase(passphrase)
    except (VaultError, OSError) as exc:
        flash(f"Could not set the passphrase: {exc}", "error")
        return redirect(url_for("auth.set_passphrase"))
    start_session(store)
    flash("Passphrase set. Next, create a recovery code below, so a lost passphrase can't "
          "lock you out.", "info")
    return redirect(url_for("settings.index", _anchor="recovery"))


@bp.get("/recover")
def recover():
    store = current_store()
    if not store.exists:
        return redirect(url_for("auth.setup"))
    return render_template("recover.html", available=store.recovery_available())


@bp.post("/recover")
def recover_post():
    store = current_store()
    try:
        store.unlock_with_recovery_code(request.form.get("code", ""))
    except (BadRecoveryCode, NoRecoveryCode):
        flash("That recovery code doesn't open this vault. Check it and try again.", "error")
        return redirect(url_for("auth.recover"))
    except (VaultError, OSError) as exc:
        flash(f"Could not open the vault: {exc}", "error")
        return redirect(url_for("auth.recover"))
    start_session(store)
    return redirect(url_for("auth.recover_passphrase"))


@bp.get("/recover/new-passphrase")
def recover_passphrase():
    if not current_store().needs_new_passphrase:
        return redirect(url_for("dashboard.index"))
    return render_template("recover_passphrase.html", min_length=MIN_PASSPHRASE)


@bp.post("/recover/new-passphrase")
def recover_passphrase_post():
    store = current_store()
    if not store.needs_new_passphrase:
        return redirect(url_for("dashboard.index"))
    passphrase = request.form.get("passphrase", "")
    problem = passphrase_problem(passphrase, request.form.get("confirm", ""))
    if problem:
        flash(problem, "error")
        return redirect(url_for("auth.recover_passphrase"))
    store.change_passphrase(passphrase)
    flash("New passphrase set. Your recovery code still works; if anyone else may have seen it, "
          "make a new one in Settings.", "info")
    return redirect(url_for("dashboard.index"))


@bp.post("/lock")
def lock():
    current_store().lock()
    session.clear()
    flash("Locked.", "info")
    return redirect(url_for("auth.unlock"))
