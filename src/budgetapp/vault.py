"""Encrypted single-file vault.

The entire SQLite database is serialized, encrypted and written as one file, so
plaintext financial data never touches the disk.

File layout (integers are big-endian)::

    magic    8 bytes   b"BGTVAULT"
    hlen     4 bytes   length of header
    header   hlen      UTF-8 JSON (below)
    body     rest      AES-256-GCM ciphertext of the database (includes the 16-byte auth tag)

Format 2 (written): the database is encrypted with a random 256-bit *data key*. The header
holds that key wrapped with AES-256-GCM under a key derived by Argon2id from the passphrase,
and optionally a second copy wrapped under a key derived from a recovery code, so either
secret opens the vault. Changing the passphrase re-wraps the same data key, so a recovery
code stays valid::

    {"v": 2, "nonce": ..., "slots": {"passphrase": slot, "recovery": slot}}
    slot = {"kdf": {argon2id params + salt}, "nonce": ..., "key": wrapped, "created": date}

Format 1 (read only): the database key was derived straight from the passphrase. Opening
one hands back a fresh random data key, so the next save is format 2 and nothing derived
from the old file (or its backups) can open the new one.

magic + hlen + header are bound to the ciphertext as associated data, so any change to the
header (weakening KDF parameters, swapping a slot) fails decryption.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

MAGIC = b"BGTVAULT"
FORMAT_VERSION = 2
KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16
RECOVERY_BYTES = 20  # 160 bits, shown as 32 base32 characters in groups of four
_HLEN = struct.Struct(">I")
_MAX_HEADER = 4096
_ROLES = {"passphrase": b"budget-vault/passphrase", "recovery": b"budget-vault/recovery"}


class VaultError(Exception):
    """Base class for vault problems."""


class BadPassphrase(VaultError):
    """Decryption failed: wrong passphrase, or the file was modified."""


class BadRecoveryCode(VaultError):
    """Decryption failed: wrong recovery code, or the file was modified."""


class NoRecoveryCode(VaultError):
    """The vault has no recovery code."""


class CorruptVault(VaultError):
    """The file is not a readable vault."""


@dataclass(frozen=True)
class KdfParams:
    salt: bytes
    iterations: int = 3
    lanes: int = 4
    memory_kib: int = 64 * 1024

    @classmethod
    def fresh(cls, **overrides: int) -> KdfParams:
        return cls(salt=secrets.token_bytes(SALT_BYTES), **overrides)

    def like(self) -> KdfParams:
        """The same cost with a fresh salt."""
        return replace(self, salt=secrets.token_bytes(SALT_BYTES))

    def derive(self, secret: str) -> bytes:
        kdf = Argon2id(
            salt=self.salt,
            length=KEY_BYTES,
            iterations=self.iterations,
            lanes=self.lanes,
            memory_cost=self.memory_kib,
        )
        return kdf.derive(secret.encode("utf-8"))

    def to_json(self) -> dict:
        return {
            "name": "argon2id",
            "salt": _b64(self.salt),
            "iterations": self.iterations,
            "lanes": self.lanes,
            "memory_kib": self.memory_kib,
        }

    @classmethod
    def from_json(cls, data: dict) -> KdfParams:
        try:
            if data["name"] != "argon2id":
                raise CorruptVault(f"Unsupported KDF: {data['name']!r}")
            params = cls(
                salt=_unb64(data["salt"]),
                iterations=int(data["iterations"]),
                lanes=int(data["lanes"]),
                memory_kib=int(data["memory_kib"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptVault("Vault header has invalid KDF parameters.") from exc
        # Sanity bounds so a hostile header can't make us allocate absurd memory.
        if not (1 <= params.iterations <= 64 and 1 <= params.lanes <= 64):
            raise CorruptVault("Vault header has out-of-range KDF parameters.")
        if not (8 * params.lanes <= params.memory_kib <= 4 * 1024 * 1024):
            raise CorruptVault("Vault header has out-of-range KDF parameters.")
        return params


@dataclass(frozen=True)
class Slot:
    """The data key, wrapped under a key derived from one secret."""

    kdf: KdfParams
    nonce: bytes
    wrapped: bytes = field(repr=False)
    created: str = ""

    @classmethod
    def wrap(cls, role: str, secret: str, data_key: bytes, kdf: KdfParams) -> Slot:
        nonce = secrets.token_bytes(NONCE_BYTES)
        wrapped = AESGCM(kdf.derive(secret)).encrypt(nonce, data_key, _ROLES[role])
        return cls(kdf, nonce, wrapped, date.today().isoformat())

    def unwrap(self, role: str, secret: str) -> bytes | None:
        try:
            return AESGCM(self.kdf.derive(secret)).decrypt(self.nonce, self.wrapped, _ROLES[role])
        except InvalidTag:
            return None

    def to_json(self) -> dict:
        return {
            "kdf": self.kdf.to_json(),
            "nonce": _b64(self.nonce),
            "key": _b64(self.wrapped),
            "created": self.created,
        }

    @classmethod
    def from_json(cls, data: dict) -> Slot:
        try:
            return cls(
                KdfParams.from_json(data["kdf"]),
                _unb64(data["nonce"]),
                _unb64(data["key"]),
                str(data.get("created", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptVault("Vault header has an invalid key slot.") from exc


@dataclass(frozen=True)
class VaultKey:
    key: bytes = field(repr=False)  # the data key that encrypts the database
    passphrase: Slot
    recovery: Slot | None = None

    @classmethod
    def from_passphrase(cls, passphrase: str, kdf: KdfParams) -> VaultKey:
        """A new key: a fresh random data key, wrapped under the passphrase."""
        data_key = secrets.token_bytes(KEY_BYTES)
        return cls(data_key, Slot.wrap("passphrase", passphrase, data_key, kdf))

    def verify(self, passphrase: str) -> bool:
        opened = self.passphrase.unwrap("passphrase", passphrase)
        return opened is not None and hmac.compare_digest(opened, self.key)

    def with_passphrase(self, passphrase: str, kdf: KdfParams) -> VaultKey:
        """Same data key under a new passphrase; a recovery code keeps working."""
        return replace(self, passphrase=Slot.wrap("passphrase", passphrase, self.key, kdf))

    def with_recovery(self, code: str, kdf: KdfParams) -> VaultKey:
        """Add or replace the recovery code (a replaced one stops working)."""
        slot = Slot.wrap("recovery", normalize_recovery_code(code), self.key, kdf)
        return replace(self, recovery=slot)

    def without_recovery(self) -> VaultKey:
        return replace(self, recovery=None)


def new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(RECOVERY_BYTES)).decode("ascii")
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def normalize_recovery_code(code: str) -> str:
    """Forgiving about case, spaces and dashes, and digits typed for look-alike letters."""
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    return cleaned.translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))


def seal(plaintext: bytes, vkey: VaultKey) -> bytes:
    """Encrypt plaintext into vault file bytes. A fresh nonce is used every time."""
    slots = {"passphrase": vkey.passphrase.to_json()}
    if vkey.recovery is not None:
        slots["recovery"] = vkey.recovery.to_json()
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = json.dumps(
        {"v": FORMAT_VERSION, "nonce": _b64(nonce), "slots": slots},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    prefix = MAGIC + _HLEN.pack(len(header)) + header
    return prefix + AESGCM(vkey.key).encrypt(nonce, plaintext, prefix)


def unseal(blob: bytes, passphrase: str) -> tuple[bytes, VaultKey]:
    """Decrypt vault file bytes. Returns (plaintext, key) so the caller can re-seal."""
    prefix, header, body = _parse(blob)
    if header["v"] == 1:
        kdf = KdfParams.from_json(header.get("kdf", {}))
        plaintext = _decrypt(prefix, header, body, kdf.derive(passphrase), BadPassphrase)
        return plaintext, VaultKey.from_passphrase(passphrase, kdf.like())
    slots = _slots(header)
    data_key = slots["passphrase"].unwrap("passphrase", passphrase)
    if data_key is None:
        raise BadPassphrase("Wrong passphrase, or the vault file was modified.")
    plaintext = _decrypt(prefix, header, body, data_key, BadPassphrase)
    return plaintext, VaultKey(data_key, slots["passphrase"], slots.get("recovery"))


def unseal_with_recovery_code(blob: bytes, code: str) -> tuple[bytes, VaultKey]:
    """Decrypt with the recovery code instead of the passphrase."""
    prefix, header, body = _parse(blob)
    slots = _slots(header) if header["v"] >= 2 else {}
    slot = slots.get("recovery")
    if slot is None:
        raise NoRecoveryCode("This vault has no recovery code.")
    data_key = slot.unwrap("recovery", normalize_recovery_code(code))
    if data_key is None:
        raise BadRecoveryCode("Wrong recovery code, or the vault file was modified.")
    plaintext = _decrypt(prefix, header, body, data_key, BadRecoveryCode)
    return plaintext, VaultKey(data_key, slots["passphrase"], slot)


def has_recovery_code(blob: bytes) -> bool:
    """Whether the file carries a recovery code (reads the header only)."""
    try:
        _prefix, header, _body = _parse(blob)
    except CorruptVault:
        return False
    slots = header.get("slots")
    return header["v"] >= 2 and isinstance(slots, dict) and "recovery" in slots


def write_atomic(path: Path, data: bytes, *, retries: int = 5) -> None:
    """Write via temp file + fsync + rename so a crash never leaves a half-written vault.

    Retries the rename briefly because OneDrive/antivirus can hold a transient lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _decrypt(prefix: bytes, header: dict, body: bytes, key: bytes, error: type) -> bytes:
    try:
        nonce = _unb64(header["nonce"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptVault("Vault header is missing its nonce.") from exc
    try:
        return AESGCM(key).decrypt(nonce, body, prefix)
    except InvalidTag:
        raise error("The key doesn't open this vault, or the file was modified.") from None


def _slots(header: dict) -> dict[str, Slot]:
    raw = header.get("slots")
    if not isinstance(raw, dict) or "passphrase" not in raw:
        raise CorruptVault("Vault header has no passphrase slot.")
    return {role: Slot.from_json(raw[role]) for role in _ROLES if role in raw}


def _parse(blob: bytes) -> tuple[bytes, dict, bytes]:
    if len(blob) < len(MAGIC) + _HLEN.size or not blob.startswith(MAGIC):
        raise CorruptVault("Not a budget vault file.")
    (hlen,) = _HLEN.unpack_from(blob, len(MAGIC))
    start = len(MAGIC) + _HLEN.size
    if hlen > _MAX_HEADER or start + hlen > len(blob):
        raise CorruptVault("Vault header is truncated or too large.")
    raw = blob[start : start + hlen]
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptVault("Vault header is not valid JSON.") from exc
    if not isinstance(header, dict) or header.get("v") not in (1, FORMAT_VERSION):
        raise CorruptVault("Unsupported vault format version.")
    return blob[: start + hlen], header, blob[start + hlen :]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
