"""Financial documents: one place to upload policies, statements and their newer versions."""

from __future__ import annotations

from urllib.parse import quote

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from budgetapp import documents
from budgetapp import networth as nw
from budgetapp.web import current_store, forms
from budgetapp.web.transactions import IMPORT_ACCOUNT_TYPES, stage_import

bp = Blueprint("documents", __name__, url_prefix="/documents")


@bp.get("/")
def index():
    store = current_store()
    with store.read() as conn:
        items = documents.list_documents(conn)
        used = documents.total_bytes(conn)
        accounts = nw.list_accounts(conn, types=IMPORT_ACCOUNT_TYPES)
    return render_template(
        "documents.html",
        documents=items,
        used_label=documents.size_label(used),
        max_mb=documents.MAX_BYTES // (1024 * 1024),
        suggestions=documents.SUGGESTIONS,
        accounts=accounts,
        folder=store.documents_dir,
    )


@bp.post("/upload")
def upload():
    """Add a version to an existing document, or start a new one. Used by home and this page."""
    store = current_store()
    upload_file = request.files.get("file")
    name = forms.text(request.form, "name", "Document name", max_len=documents.MAX_NAME)
    try:
        if upload_file is None or not upload_file.filename:
            raise ValueError("Choose a file to upload.")
        data = upload_file.read(documents.MAX_BYTES + 1)
        with store.write() as conn:
            document_id = (
                documents.find_or_add(conn, name)
                if name
                else forms.integer(request.form, "document_id", "Document", required=True)
            )
            documents.save_version(
                conn, store.documents_dir, document_id,
                filename=upload_file.filename, data=data,
            )
        flash(f"Uploaded to {name or _name(document_id)}.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        flash("That document no longer exists.", "error")
    return redirect(request.form.get("next") or url_for("documents.index"))


def _name(document_id: int) -> str:
    with current_store().read() as conn:
        try:
            return documents.get_document(conn, document_id).name
        except LookupError:
            return "that document"


@bp.post("/")
def add():
    try:
        with current_store().write() as conn:
            documents.add_document(
                conn,
                name=forms.text(request.form, "name", "Document name", required=True,
                                max_len=documents.MAX_NAME),
                notes=forms.text(request.form, "notes", "Notes", max_len=200),
            )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("documents.index"))


@bp.post("/<int:document_id>")
def edit(document_id: int):
    try:
        with current_store().write() as conn:
            documents.update_document(
                conn, document_id,
                name=forms.text(request.form, "name", "Document name", required=True,
                                max_len=documents.MAX_NAME),
                notes=forms.text(request.form, "notes", "Notes", max_len=200),
            )
        flash("Document saved.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("documents.index"))


@bp.post("/<int:document_id>/delete")
def delete(document_id: int):
    store = current_store()
    try:
        with store.write() as conn:
            documents.delete_document(conn, store.documents_dir, document_id)
        flash("Document and its files deleted.", "info")
    except LookupError:
        abort(404)
    return redirect(url_for("documents.index"))


@bp.post("/files/<int:version_id>/delete")
def delete_version(version_id: int):
    store = current_store()
    try:
        with store.write() as conn:
            documents.delete_version(conn, store.documents_dir, version_id)
        flash("That version was deleted.", "info")
    except LookupError:
        abort(404)
    return redirect(url_for("documents.index"))


@bp.post("/files/<int:version_id>/import")
def import_csv(version_id: int):
    """Hand a CSV or OFX/QFX filed here to the Transactions import, preview and all."""
    store = current_store()
    try:
        account_id = forms.integer(request.form, "account_id", "Account", required=True)
        with store.read() as conn:
            version, data = documents.open_version(conn, store.documents_dir, version_id)
        if not version.importable:
            raise ValueError("Only a CSV, OFX or QFX file can be imported into Transactions.")
        token = stage_import(
            store, account_id=account_id, filename=version.filename, raw=data
        )
    except (ValueError, LookupError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.index"))
    return redirect(url_for("transactions.map_import", token=token))


@bp.get("/files/<int:version_id>")
def file(version_id: int):
    """Decrypt one uploaded file and hand it to the browser. Never cached (see cache_control)."""
    store = current_store()
    try:
        with store.read() as conn:
            version, data = documents.open_version(conn, store.documents_dir, version_id)
    except LookupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.index"))
    # Uploads are not trusted markup: only known-safe types are shown in the browser, and
    # nosniff (set on every response) keeps the rest from being interpreted as HTML.
    inline = version.viewable and request.args.get("download") != "1"
    disposition = "inline" if inline else "attachment"
    return Response(
        data,
        mimetype=version.content_type if inline else documents.DEFAULT_TYPE,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(version.filename)}",
            "Content-Length": str(len(data)),
        },
    )
