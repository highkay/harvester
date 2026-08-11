#!/usr/bin/env python3

"""AES-256-GCM encryption manager for sensitive token storage.

- AES-256-GCM via ``cryptography.hazmat.primitives.ciphers.aead.AESGCM``
- 12-byte random nonce prepended to ciphertext; stored as ``base64(nonce + ct + tag)``
- Master key from ``ENCRYPTION_KEY`` env var (32-byte hex, or any string → SHA-256
  derived to 32 bytes).  When absent, a one-time key is generated in-process and a
  warning is printed.
- ``hash_token()`` returns SHA-256 hex digest first 16 chars for dedup indexing.
- Module-level ``encrypt_str`` / ``decrypt_str`` use an internal singleton.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES: Final[int] = 12
_KEY_BYTES: Final[int] = 32
_HASH_PREFIX_LEN: Final[int] = 16


def _derive_key(raw: str) -> bytes:
    """Hash *raw* with SHA-256 and return the resulting 32 bytes."""
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _load_or_generate_key() -> bytes:
    """Return a 32-byte AES key from ``ENCRYPTION_KEY`` or generate a warning-only key."""
    env_val = os.environ.get("ENCRYPTION_KEY")
    if env_val:
        candidate = env_val.strip()
        # 64 hex chars = 32 bytes
        if len(candidate) == 64:
            try:
                return bytes.fromhex(candidate)
            except ValueError:
                pass
        # Any other value → SHA-256 derive
        return _derive_key(candidate)

    # Generate a one-time key; print warning
    generated = bytes.fromhex(secrets.token_hex(_KEY_BYTES))
    print(
        "WARNING: ENCRYPTION_KEY env var not set. "
        "Generated a one-time key valid for this process only. "
        "Please set and back up ENCRYPTION_KEY, otherwise encrypted data "
        "will be unrecoverable after restart.",
    )
    return generated


class CryptoManager:
    """Encrypts/decrypts tokens with AES-256-GCM and computes hash indices."""

    def __init__(self) -> None:
        self._key: Final[bytes] = _load_or_generate_key()

    def encrypt(self, plaintext: str) -> str:
        """Return ``base64(nonce || ciphertext || tag)``.

        Each call uses a fresh random 12-byte nonce, so the same *plaintext*
        produces a different output every time.
        """
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aesgcm = AESGCM(self._key)
        data = plaintext.encode("utf-8")
        ct = aesgcm.encrypt(nonce, data, associated_data=None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a *token* previously produced by :meth:`encrypt`.

        Raises ``ValueError`` for tampered or truncated input.
        """
        try:
            raw = base64.b64decode(token.encode("ascii"))
        except Exception as exc:
            raise ValueError("Invalid base64 encoding") from exc
        if len(raw) < _NONCE_BYTES + 16:  # 16 = GCM tag minimum
            raise ValueError("Token too short: missing nonce or tag")
        nonce = raw[:_NONCE_BYTES]
        ct = raw[_NONCE_BYTES:]
        aesgcm = AESGCM(self._key)
        try:
            plaintext = aesgcm.decrypt(nonce, ct, associated_data=None)
        except Exception as exc:
            raise ValueError("Decryption failed: token tampered or key mismatch") from exc
        return plaintext.decode("utf-8")

    @staticmethod
    def hash_token(value: str) -> str:
        """Return SHA-256 hex digest, first 16 chars — stable dedup index."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_PREFIX_LEN]


# ---------------------------------------------------------------------------
# Module-level singleton convenience
# ---------------------------------------------------------------------------

_crypto: CryptoManager | None = None


def _get_crypto() -> CryptoManager:
    """Lazy-initialise the module-level singleton."""
    global _crypto
    if _crypto is None:
        _crypto = CryptoManager()
    return _crypto


def encrypt_str(plaintext: str) -> str:
    """Encrypt *plaintext* via the internal singleton ``CryptoManager``."""
    return _get_crypto().encrypt(plaintext)


def decrypt_str(token: str) -> str:
    """Decrypt *token* via the internal singleton ``CryptoManager``."""
    return _get_crypto().decrypt(token)
