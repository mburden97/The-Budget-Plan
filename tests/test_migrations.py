import sqlite3

import pytest

from budgetapp import db, networth, planning


def _v1_database() -> bytes:
    """A database as the first release created it, with some user data."""
    conn = sqlite3.connect(":memory:")
    version, sql = db._migrations()[0]
    assert version == 1
    conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'cash')")
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, as_of, balance_cents) "
        "VALUES (1, '2026-01-01', 12345)"
    )
    conn.execute("INSERT INTO line_items (category_id, name, amount_cents) VALUES (2, 'Rent', 1)")
    conn.commit()
    data = conn.serialize()
    conn.close()
    return data


def test_upgrade_preserves_data():
    conn = db.load(_v1_database())
    assert db.schema_version(conn) == db.latest_version()
    [account] = networth.list_accounts(conn)
    assert (account.name, account.balance_cents) == ("Checking", 12345)
    assert not account.emergency_fund
    networth.add_account(conn, name="Coinbase", type="crypto")  # new type accepted
    assert [i.name for g in planning.grouped(conn) for i in g.items] == ["Rent"]


def test_foreign_keys_enforced_after_migration():
    conn = db.load(_v1_database())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO balance_snapshots (account_id, as_of, balance_cents) "
            "VALUES (999, '2026-01-01', 1)"
        )


def test_fresh_database_is_latest(conn):
    assert db.schema_version(conn) == db.latest_version()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
