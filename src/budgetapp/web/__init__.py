"""Flask app served on 127.0.0.1 only.

Defenses for a localhost web UI:
  * Host header allow-list        -> blocks DNS-rebinding attacks from websites
  * Origin check + CSRF token     -> blocks cross-site form posts
  * SameSite=Strict HttpOnly cookie bound to the current unlock -> other local
    processes can't use the app without the session cookie
  * Strict CSP, no-store caching  -> no third-party code, no financial pages in browser cache
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from flask import Flask, abort, current_app, flash, redirect, request, session, url_for
from markupsafe import Markup, escape

from budgetapp import documents as docs
from budgetapp import settings as prefs
from budgetapp.charts import compact_money
from budgetapp.dates import month_label
from budgetapp.money import (
    cents_to_input,
    format_money,
    format_percent,
    format_quantity,
    percent_input,
)
from budgetapp.store import Locked, Store

PUBLIC_ENDPOINTS = {
    "auth.setup",
    "auth.setup_post",
    "auth.unlock",
    "auth.unlock_post",
    "auth.set_passphrase",
    "auth.set_passphrase_post",
    "auth.recover",
    "auth.recover_post",
    "static",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# After opening with the recovery code, only the new-passphrase page is reachable.
RECOVERY_ENDPOINTS = {"auth.recover_passphrase", "auth.recover_passphrase_post"}

# Every vault needs a passphrase. The early no-passphrase mode (a vault that opened with no
# prompt) was turned off at the user's go-ahead on 2026-09-16; a vault made in that mode
# opens to /set-passphrase until it gets one. Don't turn this back on.
ALLOW_BLANK_PASSPHRASE = False

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def create_app(store: Store, *, port: int) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32),  # per process: restarting invalidates sessions
        # Browsers share cookies across ports on one host, so each port gets its own name:
        # two copies of the app side by side must not overwrite each other's session.
        SESSION_COOKIE_NAME=f"budget_session_{port}",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        # Documents are the big uploads; bank CSVs are held to MAX_CSV_BYTES separately.
        MAX_CONTENT_LENGTH=docs.MAX_BYTES + 1024 * 1024,
    )
    app.extensions["budget_store"] = store

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @app.before_request
    def guard():
        if request.host not in allowed_hosts:
            abort(400)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                abort(403)
            sent = request.form.get("csrf_token", "").encode()
            expected = session.get("csrf", "").encode()
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400)

        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        unlocked = _session_is_unlocked(store)
        if unlocked and store.passphrase_blank and not ALLOW_BLANK_PASSPHRASE:
            return redirect(url_for("auth.set_passphrase"))
        if unlocked and store.needs_new_passphrase and request.endpoint not in RECOVERY_ENDPOINTS:
            return redirect(url_for("auth.recover_passphrase"))
        if not unlocked:
            if request.method in SAFE_METHODS and blank_unlock(store):
                start_session(store)
            else:
                session.pop("unlock_token", None)
                return redirect(url_for(locked_page(store)))
        store.touch()
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # same-origin, not no-referrer: under no-referrer browsers send "Origin: null" on
        # form posts, which the origin check (correctly) rejects. Nothing leaks cross-site.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cache-Control"] = cache_control(request.endpoint, request.path)
        return response

    @app.errorhandler(Locked)
    def handle_locked(_exc):
        return redirect(url_for("auth.unlock"))

    @app.errorhandler(413)
    def handle_too_large(_exc):
        flash(f"That file is too big: {docs.MAX_BYTES // (1024 * 1024)} MB at most.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    @app.context_processor
    def template_globals():
        unlocked = _session_is_unlocked(store)
        show_cat = False
        if unlocked:
            with store.read() as conn:
                show_cat = prefs.get(conn, "show_cat") != "off"
        return {
            "show_cat": show_cat,
            "csrf_field": csrf_field,
            "unlocked": unlocked,
            "no_passphrase": unlocked and store.passphrase_blank,
            # The user's own logo if they put one in static/ (git-ignored); else the drawn one.
            "brand_logo": (Path(app.static_folder) / "logo.png").is_file(),
        }

    app.add_template_filter(format_money, "money")
    app.add_template_filter(cents_to_input, "money_input")
    app.add_template_filter(format_percent, "percent")
    app.add_template_filter(percent_input, "percent_input")
    app.add_template_filter(format_quantity, "quantity")
    app.add_template_filter(compact_money, "compact")
    app.add_template_filter(month_label, "month_label")

    from budgetapp.web import (
        auth,
        brokerage,
        budget,
        dashboard,
        documents,
        goals,
        loans,
        networth,
        settings,
        transactions,
        trends,
    )

    for module in (auth, brokerage, budget, dashboard, documents, goals, loans, networth,
                   settings, transactions, trends):
        app.register_blueprint(module.bp)

    return app


# Background videos are big and hold no financial data, so the browser may keep them.
# Every page, and every other file, stays out of the cache.
CACHEABLE_MEDIA = (".mp4", ".webm")


def cache_control(endpoint: str | None, path: str) -> str:
    if endpoint == "static" and path.lower().endswith(CACHEABLE_MEDIA):
        return "private, max-age=604800"
    return "no-store"


def current_store() -> Store:
    return current_app.extensions["budget_store"]


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field() -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(csrf_token())}">')


def locked_page(store: Store) -> str:
    """Where a request without an unlocked session goes."""
    if not store.exists:
        return "auth.setup"
    if not ALLOW_BLANK_PASSPHRASE and store.opens_without_passphrase():
        return "auth.set_passphrase"
    return "auth.unlock"


def blank_unlock(store: Store) -> bool:
    """Open a passphrase-less vault without prompting, while that mode is allowed."""
    return ALLOW_BLANK_PASSPHRASE and store.unlock_blank()


def start_session(store: Store) -> None:
    """Fresh session after unlock (prevents session fixation) bound to this unlock."""
    session.clear()
    session["csrf"] = secrets.token_urlsafe(32)
    session["unlock_token"] = store.token


def _session_is_unlocked(store: Store) -> bool:
    token = session.get("unlock_token")
    return bool(
        store.is_unlocked
        and token
        and store.token
        and hmac.compare_digest(token.encode(), store.token.encode())
    )
