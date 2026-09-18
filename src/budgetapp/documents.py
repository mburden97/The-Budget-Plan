"""Financial documents: encrypted files on disk, indexed inside the vault.

Each uploaded version gets its own random AES-256-GCM key, and only the vault knows it,
so the files under `data/documents/` are unreadable on their own -- including to whatever
syncs that folder. The file name is random too: a folder listing gives nothing away.

The bytes deliberately live outside the vault. The vault is serialized, re-encrypted and
rewritten on *every* save, so a library of PDFs inside it would make ordinary clicks slow
and resync the whole thing each time. A document file is written once and never rewritten.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from budgetapp.vault import write_atomic

MAX_BYTES = 25 * 1024 * 1024  # one big scanned PDF
NONCE_BYTES = 12
MAX_NAME = 80

# Extensions we can name a type for. Anything else is stored and handed back as a download.
CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".ofx": "application/x-ofx",
    ".qfx": "application/vnd.intu.qfx",
    ".json": "application/json",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".zip": "application/zip",
}
# Types a browser can show safely in its own viewer. Everything else downloads.
VIEWABLE = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}
DEFAULT_TYPE = "application/octet-stream"
# Types the Import button can hand to Transactions.
IMPORTABLE = {"text/csv", "application/x-ofx", "application/vnd.intu.qfx"}

# Offered in the "new document" box; nothing is created until the user picks one.
SUGGESTIONS = (
    "Lease", "Renters insurance policy", "Car insurance policy", "Health insurance card",
    "Pay stub", "W-2", "Tax return", "Loan statement", "Brokerage statement",
    "Bank statement", "Credit card statement", "Warranty", "Receipt",
)


@dataclass(frozen=True)
class Version:
    id: int
    document_id: int
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: str  # 'YYYY-MM-DD HH:MM'
    blob_name: str

    @property
    def uploaded_on(self) -> date:
        return date.fromisoformat(self.uploaded_at[:10])

    @property
    def viewable(self) -> bool:
        return self.content_type in VIEWABLE

    @property
    def importable(self) -> bool:
        return self.content_type in IMPORTABLE

    @property
    def size_label(self) -> str:
        return size_label(self.size_bytes)


@dataclass(frozen=True)
class Document:
    id: int
    name: str
    notes: str
    versions: tuple[Version, ...]  # newest first

    @property
    def latest(self) -> Version | None:
        return self.versions[0] if self.versions else None

    @property
    def updated_on(self) -> date | None:
        return self.latest.uploaded_on if self.latest else None

    @property
    def older(self) -> tuple[Version, ...]:
        return self.versions[1:]

    @property
    def size_bytes(self) -> int:
        return sum(v.size_bytes for v in self.versions)


def size_label(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{max(size_bytes // 1024, 1) if size_bytes else 0} KB"


def list_documents(conn: sqlite3.Connection) -> list[Document]:
    """Every document, most recently updated first; ones never uploaded to come last."""
    documents = [
        Document(row["id"], row["name"], row["notes"], _versions(conn, row["id"]))
        for row in conn.execute("SELECT id, name, notes FROM documents ORDER BY name")
    ]
    return sorted(documents, key=_newest_first)


def _newest_first(document: Document) -> tuple:
    when = document.updated_on
    return (when is None, -when.toordinal() if when else 0, document.name.lower())


def get_document(conn: sqlite3.Connection, document_id: int) -> Document:
    row = conn.execute(
        "SELECT id, name, notes FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Document {document_id} not found.")
    return Document(row["id"], row["name"], row["notes"], _versions(conn, row["id"]))


def _versions(conn: sqlite3.Connection, document_id: int) -> tuple[Version, ...]:
    rows = conn.execute(
        "SELECT id, document_id, filename, content_type, size_bytes, uploaded_at, blob_name "
        "FROM document_versions WHERE document_id = ? ORDER BY uploaded_at DESC, id DESC",
        (document_id,),
    )
    return tuple(Version(*tuple(row)) for row in rows)


def add_document(conn: sqlite3.Connection, *, name: str, notes: str = "") -> int:
    name = _check_name(name)
    try:
        cur = conn.execute(
            "INSERT INTO documents (name, notes, created_at) VALUES (?, ?, ?)",
            (name, notes.strip(), date.today().isoformat()),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"You already have a document called {name}.") from None
    return cur.lastrowid


def update_document(conn: sqlite3.Connection, document_id: int, *, name: str, notes: str) -> None:
    get_document(conn, document_id)
    try:
        conn.execute(
            "UPDATE documents SET name = ?, notes = ? WHERE id = ?",
            (_check_name(name), notes.strip(), document_id),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"You already have a document called {name.strip()}.") from None


def find_or_add(conn: sqlite3.Connection, name: str) -> int:
    """The id of the document with this name, creating it if it is new."""
    row = conn.execute("SELECT id FROM documents WHERE name = ?", (_check_name(name),)).fetchone()
    return row["id"] if row else add_document(conn, name=name)


def _check_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Give the document a name, e.g. Renters insurance policy.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Document names are at most {MAX_NAME} characters.")
    return name


# ---------------------------------------------------------------- files
def save_version(
    conn: sqlite3.Connection, folder: Path, document_id: int, *, filename: str, data: bytes
) -> int:
    """Encrypt an uploaded file, write it to `folder`, and record it as the newest version."""
    get_document(conn, document_id)
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Files are limited to {MAX_BYTES // (1024 * 1024)} MB.")
    filename = safe_filename(filename)
    blob_name = f"{secrets.token_hex(16)}.bin"  # tells a folder listing nothing
    key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(NONCE_BYTES)
    folder.mkdir(parents=True, exist_ok=True)
    blob = nonce + AESGCM(key).encrypt(nonce, data, blob_name.encode())
    write_atomic(folder / blob_name, blob)
    try:
        cur = conn.execute(
            "INSERT INTO document_versions (document_id, filename, content_type, size_bytes, "
            "uploaded_at, blob_name, key_hex) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                filename,
                content_type(filename),
                len(data),
                datetime.now().isoformat(sep=" ", timespec="minutes"),
                blob_name,
                key.hex(),
            ),
        )
    except Exception:
        (folder / blob_name).unlink(missing_ok=True)  # no row, so no orphan file either
        raise
    return cur.lastrowid


def open_version(conn: sqlite3.Connection, folder: Path, version_id: int) -> tuple[Version, bytes]:
    row = conn.execute(
        "SELECT id, document_id, filename, content_type, size_bytes, uploaded_at, blob_name, "
        "key_hex FROM document_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Document file {version_id} not found.")
    version = Version(*tuple(row)[:7])
    path = folder / version.blob_name
    try:
        blob = path.read_bytes()
        data = AESGCM(bytes.fromhex(row["key_hex"])).decrypt(
            blob[:NONCE_BYTES], blob[NONCE_BYTES:], version.blob_name.encode()
        )
    except FileNotFoundError:
        raise LookupError(f"{version.filename} is missing from {folder}.") from None
    except (InvalidTag, ValueError):
        raise LookupError(f"{version.filename} is damaged and can't be opened.") from None
    return version, data


def delete_version(conn: sqlite3.Connection, folder: Path, version_id: int) -> int:
    """Delete one uploaded file; returns the document it belonged to."""
    row = conn.execute(
        "SELECT document_id, blob_name FROM document_versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Document file {version_id} not found.")
    conn.execute("DELETE FROM document_versions WHERE id = ?", (version_id,))
    (folder / row["blob_name"]).unlink(missing_ok=True)
    return row["document_id"]


def delete_document(conn: sqlite3.Connection, folder: Path, document_id: int) -> None:
    document = get_document(conn, document_id)
    for version in document.versions:
        (folder / version.blob_name).unlink(missing_ok=True)
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def prune_orphans(conn: sqlite3.Connection, folder: Path) -> int:
    """Delete files in `folder` no version points at (a crash between write and commit)."""
    if not folder.is_dir():
        return 0
    known = {row["blob_name"] for row in conn.execute("SELECT blob_name FROM document_versions")}
    removed = 0
    for path in folder.glob("*.bin"):
        if path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def safe_filename(filename: str) -> str:
    """Just the file's own name: no directories, no control characters, never empty."""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip(" .")
    return name[:120] or "document"


def content_type(filename: str) -> str:
    return CONTENT_TYPES.get("." + filename.rsplit(".", 1)[-1].lower(), DEFAULT_TYPE)


def total_bytes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM document_versions").fetchone()[0]
