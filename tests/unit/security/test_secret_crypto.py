from __future__ import annotations

import gc
import hashlib
from dataclasses import replace

import pytest

from autotrader.security.secret_crypto import MasterKeyRing, SecretEnvelope

CURRENT_KEY = b"c" * 32
PREVIOUS_KEY = b"p" * 32
PLAINTEXT = b"synthetic-sensitive-value"
AAD = b"kis-live|v1|KIS|LIVE|1"


def _ring() -> MasterKeyRing:
    return MasterKeyRing(
        current_key=CURRENT_KEY,
        current_version=2,
        previous_key=PREVIOUS_KEY,
        previous_version=1,
    )


def test_encrypt_uses_random_twelve_byte_nonces_and_round_trips() -> None:
    ring = _ring()

    first = ring.encrypt(plaintext=PLAINTEXT, aad=AAD)
    second = ring.encrypt(plaintext=PLAINTEXT, aad=AAD)

    assert len(first.nonce) == 12
    assert len(second.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert ring.decrypt(envelope=first, aad=AAD) == PLAINTEXT
    assert ring.decrypt(envelope=second, aad=AAD) == PLAINTEXT


def test_fingerprint_is_stable_domain_separated_and_non_reversible() -> None:
    first = _ring().encrypt(plaintext=PLAINTEXT, aad=AAD)
    rotated_ring = MasterKeyRing(current_key=b"n" * 32, current_version=3)
    second = rotated_ring.encrypt(plaintext=PLAINTEXT, aad=b"different-aad")
    expected = hashlib.sha256(
        b"autotrader.backoffice.secret-fingerprint.v1\x00" + PLAINTEXT
    ).digest()

    assert first.fingerprint == second.fingerprint == expected
    assert len(first.fingerprint) == 32
    assert first.fingerprint != PLAINTEXT
    assert PLAINTEXT not in first.fingerprint


def test_aad_is_authenticated_without_leaking_plaintext() -> None:
    ring = _ring()
    envelope = ring.encrypt(plaintext=PLAINTEXT, aad=AAD)

    with pytest.raises(ValueError, match="authentication failed") as exc_info:
        ring.decrypt(envelope=envelope, aad=b"other-scope")

    assert PLAINTEXT.decode() not in str(exc_info.value)


@pytest.mark.parametrize(
    "field", ["ciphertext", "nonce", "master_key_version", "fingerprint"]
)
def test_decrypt_rejects_tampering_in_every_envelope_field(field: str) -> None:
    shared_key = b"k" * 32
    ring = MasterKeyRing(
        current_key=shared_key,
        current_version=2,
        previous_key=shared_key,
        previous_version=1,
    )
    envelope = ring.encrypt(plaintext=PLAINTEXT, aad=AAD)
    mutations = {
        "ciphertext": replace(
            envelope,
            ciphertext=bytes([envelope.ciphertext[0] ^ 1]) + envelope.ciphertext[1:],
        ),
        "nonce": replace(
            envelope, nonce=bytes([envelope.nonce[0] ^ 1]) + envelope.nonce[1:]
        ),
        "master_key_version": replace(envelope, master_key_version=1),
        "fingerprint": replace(
            envelope,
            fingerprint=bytes([envelope.fingerprint[0] ^ 1]) + envelope.fingerprint[1:],
        ),
    }

    with pytest.raises(ValueError, match="authentication failed") as exc_info:
        ring.decrypt(envelope=mutations[field], aad=AAD)

    assert PLAINTEXT.decode() not in str(exc_info.value)


def test_key_ring_does_not_retain_plaintext_after_decryption() -> None:
    ring = _ring()
    envelope = ring.encrypt(plaintext=PLAINTEXT, aad=AAD)

    assert ring.decrypt(envelope=envelope, aad=AAD) == PLAINTEXT

    direct_referents = gc.get_referents(ring)
    assert PLAINTEXT not in direct_referents
    assert all(
        PLAINTEXT not in referent.values()
        for referent in direct_referents
        if isinstance(referent, dict)
    )
    assert not hasattr(ring, "__dict__")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"current_key": b"short", "current_version": 1}, "32 bytes"),
        ({"current_key": bytearray(b"k" * 32), "current_version": 1}, "bytes"),
        ({"current_key": b"k" * 32, "current_version": True}, "positive"),
        (
            {
                "current_key": b"k" * 32,
                "current_version": 2,
                "previous_key": b"p" * 32,
            },
            "together",
        ),
        (
            {
                "current_key": b"k" * 32,
                "current_version": 2,
                "previous_key": b"p" * 32,
                "previous_version": 2,
            },
            "distinct",
        ),
    ],
)
def test_key_ring_requires_exact_valid_key_material(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MasterKeyRing(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "ciphertext": b"x" * 16,
            "nonce": b"n" * 11,
            "master_key_version": 1,
            "fingerprint": b"f" * 32,
        },
        {
            "ciphertext": b"x" * 15,
            "nonce": b"n" * 12,
            "master_key_version": 1,
            "fingerprint": b"f" * 32,
        },
        {
            "ciphertext": b"x" * 16,
            "nonce": b"n" * 12,
            "master_key_version": 0,
            "fingerprint": b"f" * 32,
        },
        {
            "ciphertext": b"x" * 16,
            "nonce": b"n" * 12,
            "master_key_version": 1,
            "fingerprint": b"f" * 31,
        },
    ],
)
def test_envelope_rejects_invalid_field_shapes(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid"):
        SecretEnvelope(**kwargs)  # type: ignore[arg-type]


def test_crypto_validation_errors_do_not_include_plaintext() -> None:
    ring = _ring()

    with pytest.raises(ValueError) as exc_info:
        ring.encrypt(plaintext="synthetic-sensitive-value", aad=AAD)  # type: ignore[arg-type]

    assert PLAINTEXT.decode() not in str(exc_info.value)
