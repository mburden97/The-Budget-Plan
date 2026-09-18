"""Unlocked-session state: the decrypted in-memory database plus the vault key.

Every committed write is immediately re-encrypted and saved, so the vault on disk
is always current and locking never loses data.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from budgetapp import db
from budgetapp.vault import (
    BadPassphrase,
    KdfParams,
    VaultError,
    VaultKey,
    has_recovery_code,
    new_recovery_code,
    seal,
    unseal,
    unseal_with_recovery_code,
    write_atomic,
)


class Locked(Exception):
    """The vault is locked; unlock it first."""


class Store:
    def __init__(
        self,
        vault_path: Path,
        *,
        backup_dir: Path | None = None,
        keep_backups: int = 30,
        kdf: dict | None = None,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.keep_backups = keep_backups
        self._kdf_overrides = kdf or {}
        self._mutex = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._key: VaultKey | None = None
        self._last_activity = 0.0
        # Changes on every unlock; browser sessions are bound to it.
        self.token: str | None = None
        # Transient in-memory state (e.g. a CSV awaiting column mapping). Never
        # persisted; cleared on lock.
        self.scratch: dict = {}
        self._blank = False  # vault currently has an empty passphrase
        self._blank_rejected = False  # tried "" on this vault and it failed
        self._blank_confirmed = False  # tried "" on this vault and it worked
        # Opened with the recovery code: a new passphrase has to be set before anything else.
        self.needs_new_passphrase = False

    # ------------------------------------------------------------ lifecycle
    @property
    def exists(self) -> bool:
        return self.vault_path.exists()

    @property
    def documents_dir(self) -> Path:
        """Uploaded documents, each encrypted with its own key (see documents.py)."""
        return self.vault_path.parent / "documents"

    @property
    def is_unlocked(self) -> bool:
        return self._conn is not None

    def create(self, passphrase: str) -> None:
        with self._mutex:
            if self.exists:
                raise VaultError(f"A vault already exists at {self.vault_path}.")
            key = VaultKey.from_passphrase(passphrase, KdfParams.fresh(**self._kdf_overrides))
            self._activate(db.create(), key)
            self._blank, self._blank_rejected = passphrase == "", False
            self._blank_confirmed = passphrase == ""
            self._save()

    def unlock(self, passphrase: str, *, backup: bool = True, persist: bool = True) -> None:
        with self._mutex:
            self.lock()
            blob = self.vault_path.read_bytes()
            plaintext, key = unseal(blob, passphrase)
            try:
                conn = db.load(plaintext)
            except sqlite3.DatabaseError as exc:
                raise VaultError("The vault decrypted but its database is damaged.") from exc
            if backup:
                self._backup(blob)
            self._activate(conn, key)
            self._blank = passphrase == ""
            if persist:
                self._save()  # persists any migrations (and a format upgrade)

    def unlock_blank(self) -> bool:
        """Open a vault that has no passphrase (temporary no-passphrase mode).

        Returns False, without prompting, if the vault has a real passphrase. A failed
        attempt is remembered so it costs one key derivation, not one per request.
        """
        with self._mutex:
            if self.is_unlocked:
                return self._blank
            if self._blank_rejected or not self.exists:
                return False
            try:
                self.unlock("")
            except BadPassphrase:
                self._blank_rejected = True
                return False
            except (VaultError, OSError):
                return False
            return True

    def opens_without_passphrase(self) -> bool:
        """Whether the vault still has no passphrase (made in the old no-passphrase mode),
        without unlocking it. One key derivation the first time; the answer is remembered."""
        with self._mutex:
            if self.is_unlocked:
                return self._blank
            if self._blank_confirmed:
                return True
            if self._blank_rejected or not self.exists:
                return False
            try:
                unseal(self.vault_path.read_bytes(), "")
            except BadPassphrase:
                self._blank_rejected = True
                return False
            except (VaultError, OSError):
                return False
            self._blank_confirmed = True
            return True

    def set_first_passphrase(self, passphrase: str) -> None:
        """Give a vault that has no passphrase its first one. Unlocks it without taking
        another (unprotected) backup copy, then re-encrypts it with the new passphrase."""
        with self._mutex:
            if not passphrase:
                raise VaultError("Choose a passphrase.")
            if not self.is_unlocked:
                self.unlock("", backup=False, persist=False)
            elif not self._blank:
                raise VaultError("This vault already has a passphrase.")
            self.change_passphrase(passphrase)

    @property
    def passphrase_blank(self) -> bool:
        return self.is_unlocked and self._blank

    def lock(self) -> None:
        with self._mutex:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._key = None
            self.token = None
            self.scratch.clear()
            self.needs_new_passphrase = False

    def verify_passphrase(self, passphrase: str) -> bool:
        with self._mutex:
            if self._key is None:
                return False
            return self._key.verify(passphrase)

    def change_passphrase(self, new_passphrase: str) -> None:
        """Re-wraps the same data key, so a recovery code keeps working. A vault that had no
        passphrase gets a brand-new data key instead, since its old one was never protected."""
        with self._mutex:
            self._require()
            kdf = KdfParams.fresh(**self._kdf_overrides)
            if self._blank:
                self._key = VaultKey.from_passphrase(new_passphrase, kdf)
            else:
                self._key = self._key.with_passphrase(new_passphrase, kdf)
            self._blank, self._blank_rejected = new_passphrase == "", False
            self._blank_confirmed = new_passphrase == ""
            self.needs_new_passphrase = False
            self._save()

    # ------------------------------------------------------------ recovery code
    @property
    def has_recovery_code(self) -> bool:
        return self._key is not None and self._key.recovery is not None

    def recovery_code_created(self) -> str | None:
        return self._key.recovery.created if self.has_recovery_code else None

    def recovery_available(self) -> bool:
        """Whether the vault on disk can be opened with a recovery code (no unlocking)."""
        with self._mutex:
            try:
                return self.exists and has_recovery_code(self.vault_path.read_bytes())
            except OSError:
                return False

    def create_recovery_code(self) -> str:
        """Make (or replace) the recovery code. It's returned once; only the data key wrapped
        under it is kept, so the app can't show it again."""
        with self._mutex:
            self._require()
            if self._blank:
                raise VaultError("Set a passphrase before creating a recovery code.")
            code = new_recovery_code()
            self._key = self._key.with_recovery(code, KdfParams.fresh(**self._kdf_overrides))
            self._save()
            return code

    def remove_recovery_code(self) -> None:
        with self._mutex:
            self._require()
            self._key = self._key.without_recovery()
            self._save()

    def unlock_with_recovery_code(self, code: str) -> None:
        """Open the vault with its recovery code. A new passphrase must be set next."""
        with self._mutex:
            self.lock()
            blob = self.vault_path.read_bytes()
            plaintext, key = unseal_with_recovery_code(blob, code)
            try:
                conn = db.load(plaintext)
            except sqlite3.DatabaseError as exc:
                raise VaultError("The vault decrypted but its database is damaged.") from exc
            self._backup(blob)
            self._activate(conn, key)
            self._blank = False
            self.needs_new_passphrase = True
            self._save()

    # ------------------------------------------------------------ activity / auto-lock
    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def lock_if_idle(self, max_idle_seconds: float) -> bool:
        with self._mutex:
            idle = time.monotonic() - self._last_activity > max_idle_seconds
            if self.is_unlocked and not self._blank and idle:  # nothing to lock without one
                self.lock()
                return True
            return False

    # ------------------------------------------------------------ data access
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            yield self._require()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a transaction; on success it is committed and the vault saved."""
        with self._mutex:
            conn = self._require()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._save()

    # ------------------------------------------------------------ internals
    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise Locked()
        return self._conn

    def _activate(self, conn: sqlite3.Connection, key: VaultKey) -> None:
        self._conn = conn
        self._key = key
        self.token = secrets.token_urlsafe(24)
        self.touch()

    def _save(self) -> None:
        assert self._conn is not None and self._key is not None
        write_atomic(self.vault_path, seal(db.dump(self._conn), self._key))

    def _backup(self, blob: bytes) -> None:
        """Keep a rolling set of (still encrypted) copies, one per unlock."""
        if self.backup_dir is None or self.keep_backups <= 0:
            return
        target = self.backup_dir / f"{self.vault_path.stem}-{datetime.now():%Y%m%d-%H%M%S}.vault"
        if not target.exists():
            write_atomic(target, blob)
        backups = sorted(self.backup_dir.glob(f"{self.vault_path.stem}-*.vault"))
        for old in backups[: -self.keep_backups]:
            old.unlink()
