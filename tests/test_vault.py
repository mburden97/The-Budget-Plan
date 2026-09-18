import base64
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from budgetapp.vault import (
    MAGIC,
    BadPassphrase,
    BadRecoveryCode,
    CorruptVault,
    KdfParams,
    NoRecoveryCode,
    VaultKey,
    has_recovery_code,
    new_recovery_code,
    seal,
    unseal,
    unseal_with_recovery_code,
    write_atomic,
)
from tests.conftest import FAST_KDF


@pytest.fixture
def key():
    return VaultKey.from_passphrase("hunter2hunter2", KdfParams.fresh(**FAST_KDF))


def test_roundtrip(key):
    blob = seal(b"secret budget", key)
    plaintext, recovered = unseal(blob, "hunter2hunter2")
    assert plaintext == b"secret budget"
    assert recovered.key == key.key
    assert b"secret budget" not in blob


def test_fresh_nonce_each_seal(key):
    assert seal(b"same", key) != seal(b"same", key)


def test_wrong_passphrase(key):
    with pytest.raises(BadPassphrase):
        unseal(seal(b"x", key), "wrong passphrase")


def test_tampered_ciphertext_detected(key):
    blob = bytearray(seal(b"secret budget", key))
    blob[-1] ^= 0x01
    with pytest.raises(BadPassphrase):
        unseal(bytes(blob), "hunter2hunter2")


def test_tampered_header_detected(key):
    blob = seal(b"secret budget", key)
    start = len(MAGIC) + 4
    hlen = int.from_bytes(blob[len(MAGIC) : start], "big")
    header = json.loads(blob[start : start + hlen])
    header["slots"]["passphrase"]["kdf"]["iterations"] = 2  # weaken the KDF without re-keying
    raw = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    forged = MAGIC + len(raw).to_bytes(4, "big") + raw + blob[start + hlen :]
    with pytest.raises(BadPassphrase):
        unseal(forged, "hunter2hunter2")


@pytest.mark.parametrize("blob", [b"", b"not a vault at all", MAGIC + b"\xff\xff\xff\xff"])
def test_corrupt_files(blob):
    with pytest.raises(CorruptVault):
        unseal(blob, "anything")


def test_write_atomic_replaces(tmp_path):
    target = tmp_path / "sub" / "v.vault"
    write_atomic(target, b"one")
    write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert not (tmp_path / "sub" / "v.vault.tmp").exists()


def test_recovery_code_opens_the_vault(key):
    code = new_recovery_code()
    assert len(code.replace("-", "")) == 32 and code.count("-") == 7
    keyed = key.with_recovery(code, KdfParams.fresh(**FAST_KDF))
    blob = seal(b"secret budget", keyed)
    assert has_recovery_code(blob) and not has_recovery_code(seal(b"x", key))
    plaintext, opened = unseal_with_recovery_code(blob, code.lower().replace("-", " "))
    assert plaintext == b"secret budget" and opened.key == key.key and opened.recovery
    with pytest.raises(BadRecoveryCode):
        unseal_with_recovery_code(blob, new_recovery_code())
    with pytest.raises(NoRecoveryCode):
        unseal_with_recovery_code(seal(b"x", key), code)

    renewed = keyed.with_passphrase("a different passphrase", KdfParams.fresh(**FAST_KDF))
    blob = seal(b"later", renewed)
    assert unseal(blob, "a different passphrase")[0] == b"later"
    assert unseal_with_recovery_code(blob, code)[0] == b"later"  # survives a new passphrase
    with pytest.raises(BadPassphrase):
        unseal(blob, "hunter2hunter2")
    assert renewed.verify("a different passphrase") and not renewed.verify("hunter2hunter2")
    plain = seal(b"x", renewed.without_recovery())
    assert unseal(plain, "a different passphrase")[1].recovery is None


def test_format_1_vaults_still_open_and_get_a_new_data_key():
    kdf = KdfParams.fresh(**FAST_KDF)
    old_key = kdf.derive("hunter2hunter2")
    nonce = os.urandom(12)
    header = json.dumps(
        {"v": 1, "kdf": kdf.to_json(), "nonce": base64.b64encode(nonce).decode()},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    prefix = MAGIC + len(header).to_bytes(4, "big") + header
    blob = prefix + AESGCM(old_key).encrypt(nonce, b"legacy budget", prefix)

    plaintext, vkey = unseal(blob, "hunter2hunter2")
    assert plaintext == b"legacy budget" and vkey.key != old_key  # re-keyed on open
    resealed = seal(plaintext, vkey)
    assert b'"v":2' in resealed[:600] and unseal(resealed, "hunter2hunter2")[0] == b"legacy budget"
    with pytest.raises(BadPassphrase):
        unseal(blob, "wrong passphrase")
    assert not has_recovery_code(blob)
