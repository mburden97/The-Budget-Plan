"""In-memory SQLite database and schema migrations.

The database lives only in memory while the vault is unlocked. It is loaded from
and saved to the encrypted vault via sqlite3's serialize/deserialize.
"""

from __future__ import annotations

import sqlite3
from importlib import resources


def create() -> sqlite3.Connection:
    """Create a fresh database with the latest schema and seed data."""
    conn = _connect()
    migrate(conn)
    return conn


def load(data: bytes) -> sqlite3.Connection:
    """Load a serialized database and bring its schema up to date."""
    conn = _connect()
    conn.deserialize(data)
    _configure(conn)
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        conn.close()
        raise sqlite3.DatabaseError("Database failed its integrity check.")
    migrate(conn)
    return conn


def dump(conn: sqlite3.Connection) -> bytes:
    return conn.serialize()


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    return _migrations()[-1][0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations/NNN_*.sql newer than PRAGMA user_version. Returns count applied.

    Foreign-key enforcement is switched off while migrating so tables can be rebuilt
    (SQLite can't ALTER a CHECK constraint); integrity is verified before each commit.
    """
    current = schema_version(conn)
    pending = [(version, sql) for version, sql in _migrations() if version > current]
    if not pending:
        return 0
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for version, sql in pending:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(
                    f"Migration {version} broke foreign keys: {broken[:3]}"
                )
            conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return len(pending)


def _migrations() -> list[tuple[int, str]]:
    folder = resources.files("budgetapp") / "migrations"
    found = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return sorted(found)


def _connect() -> sqlite3.Connection:
    # One connection shared across server threads; Store serializes access with a lock.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _configure(conn)
    return conn


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
