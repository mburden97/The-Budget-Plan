"""Key/value settings. They live inside the encrypted vault, API keys included."""

from __future__ import annotations

import json
import sqlite3

DEFAULTS: dict[str, str] = {
    "budget_mode": "two_paycheck",
    "emergency_fund_months": "6",
    "networth_reminder_days": "30",
    "allocation_targets": "{}",
}


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key)


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def get_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(get(conn, key) or default)
    except ValueError:
        return default


def get_json(conn: sqlite3.Connection, key: str, default):
    try:
        return json.loads(get(conn, key) or "null") or default
    except json.JSONDecodeError:
        return default


def put_json(conn: sqlite3.Connection, key: str, value) -> None:
    put(conn, key, json.dumps(value, sort_keys=True))
