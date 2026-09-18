import pytest

from budgetapp import planning
from budgetapp.store import Locked, Store
from budgetapp.vault import BadPassphrase, BadRecoveryCode, NoRecoveryCode, VaultError, unseal
from tests.conftest import FAST_KDF, PASSPHRASE


def _add_rent(store):
    with store.write() as conn:
        cat = planning.list_categories(conn)[1].id
        planning.add_line_item(conn, category_id=cat, name="Rent", amount_cents=150000)


def test_data_persists_across_lock(store):
    store.create(PASSPHRASE)
    _add_rent(store)
    store.lock()
    with pytest.raises(Locked), store.read():
        pass
    store.unlock(PASSPHRASE)
    with store.read() as conn:
        names = [i.name for g in planning.grouped(conn) for i in g.items]
    assert names == ["Rent"]


def test_vault_file_has_no_plaintext(store):
    store.create(PASSPHRASE)
    _add_rent(store)
    blob = store.vault_path.read_bytes()
    assert b"Rent" not in blob
    assert b"SQLite format" not in blob


def test_wrong_passphrase_stays_locked(store):
    store.create(PASSPHRASE)
    store.lock()
    with pytest.raises(BadPassphrase):
        store.unlock("not the passphrase")
    assert not store.is_unlocked


def test_failed_write_rolls_back(store):
    store.create(PASSPHRASE)
    with pytest.raises(ValueError), store.write() as conn:
        cat = planning.list_categories(conn)[1].id
        planning.add_line_item(conn, category_id=cat, name="Temp", amount_cents=1)
        raise ValueError("boom")
    with store.read() as conn:
        assert all(not g.items for g in planning.grouped(conn))


def test_backups_rotate(tmp_path):
    store = Store(
        tmp_path / "budget.vault", backup_dir=tmp_path / "b", keep_backups=2, kdf=FAST_KDF
    )
    store.create(PASSPHRASE)
    for i in range(4):
        (tmp_path / "b").mkdir(exist_ok=True)
        (tmp_path / "b" / f"budget-2000010{i}-000000.vault").write_bytes(b"old")
    store.unlock(PASSPHRASE)
    assert len(list((tmp_path / "b").glob("budget-*.vault"))) == 2


def test_change_passphrase(store):
    store.create(PASSPHRASE)
    store.change_passphrase("a brand new passphrase")
    store.lock()
    with pytest.raises(BadPassphrase):
        store.unlock(PASSPHRASE)
    store.unlock("a brand new passphrase")


def test_token_changes_each_unlock(store):
    store.create(PASSPHRASE)
    first = store.token
    store.unlock(PASSPHRASE)
    assert store.token and store.token != first


def test_idle_lock(store):
    store.create(PASSPHRASE)
    assert not store.lock_if_idle(3600)
    assert store.lock_if_idle(-1)
    assert not store.is_unlocked


def test_unlock_blank_only_opens_blank_vaults(store, tmp_path):
    store.create(PASSPHRASE)
    store.lock()
    assert not store.unlock_blank() and not store.is_unlocked
    assert not store.unlock_blank()  # remembered; no second key derivation

    blank = Store(tmp_path / "blank.vault", kdf=FAST_KDF)
    blank.create("")
    blank.lock()
    assert blank.unlock_blank() and blank.passphrase_blank
    assert not blank.lock_if_idle(-1)
    blank.change_passphrase(PASSPHRASE)
    blank.lock()
    assert not blank.unlock_blank()
    blank.unlock(PASSPHRASE)
    blank.change_passphrase("")
    blank.lock()
    assert blank.unlock_blank()


def test_lock_clears_transient_data(store):
    store.create(PASSPHRASE)
    store.scratch["imports"] = {"token": "pending bank rows"}
    store.lock()
    assert store.scratch == {}


def test_first_passphrase_for_a_vault_without_one(tmp_path):
    from budgetapp.vault import VaultError
    from tests.conftest import FAST_KDF, PASSPHRASE

    blank = Store(tmp_path / "blank.vault", backup_dir=tmp_path / "backups", kdf=FAST_KDF)
    blank.create("")
    blank.lock()
    assert blank.opens_without_passphrase() and not blank.is_unlocked
    blank.set_first_passphrase(PASSPHRASE)
    assert blank.is_unlocked and not blank.passphrase_blank
    assert not list((tmp_path / "backups").glob("*"))  # the unprotected copy isn't duplicated
    blank.lock()
    assert not blank.opens_without_passphrase()
    blank.unlock(PASSPHRASE)
    try:
        blank.set_first_passphrase("another long passphrase")
    except VaultError:
        pass
    else:
        raise AssertionError("a vault with a passphrase can't be given a first one")
    assert blank.verify_passphrase(PASSPHRASE)


def test_recovery_code_resets_a_lost_passphrase(store):
    store.create(PASSPHRASE)
    _add_rent(store)
    assert not store.has_recovery_code and not store.recovery_available()
    code = store.create_recovery_code()
    assert store.has_recovery_code and store.recovery_available()
    store.lock()
    with pytest.raises(BadRecoveryCode):
        store.unlock_with_recovery_code("AAAA-BBBB-CCCC")
    store.unlock_with_recovery_code(code)
    assert store.needs_new_passphrase
    store.change_passphrase("a brand new passphrase")
    assert not store.needs_new_passphrase
    store.lock()
    store.unlock("a brand new passphrase")
    with store.read() as conn:
        assert [i.name for g in planning.grouped(conn) for i in g.items] == ["Rent"]
    store.lock()
    store.unlock_with_recovery_code(code)  # still valid after the reset
    store.lock()
    store.unlock("a brand new passphrase")
    store.remove_recovery_code()
    store.lock()
    assert not store.recovery_available()
    with pytest.raises(NoRecoveryCode):
        store.unlock_with_recovery_code(code)


def test_protecting_a_blank_vault_changes_its_data_key(tmp_path):
    blank = Store(tmp_path / "b.vault", kdf=FAST_KDF)
    blank.create("")
    with pytest.raises(VaultError):
        blank.create_recovery_code()  # a passphrase comes first
    blank.lock()
    _plain, old = unseal((tmp_path / "b.vault").read_bytes(), "")
    blank.set_first_passphrase(PASSPHRASE)
    new_blob = (tmp_path / "b.vault").read_bytes()
    assert unseal(new_blob, PASSPHRASE)[1].key != old.key  # the old, unprotected key opens nothing
    with pytest.raises(BadPassphrase):
        unseal(new_blob, "")
