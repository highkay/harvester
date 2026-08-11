#!/usr/bin/env python3

"""Unit tests for web/crypto.py — AES-256-GCM encryption manager."""

from __future__ import annotations

import unittest

from web.crypto import CryptoManager, decrypt_str, encrypt_str


class TestCryptoManagerRoundTrip(unittest.TestCase):
    """Given a CryptoManager instance,
    When we encrypt plaintext and then decrypt the result,
    Then the decrypted value equals the original plaintext.
    """

    def test_decrypt_inverts_encrypt_ascii(self) -> None:
        mgr = CryptoManager()
        plaintext = "ghp_abc123def456ghi789jkl"
        token = mgr.encrypt(plaintext)
        decrypted = mgr.decrypt(token)
        self.assertEqual(decrypted, plaintext)

    def test_decrypt_inverts_encrypt_unicode(self) -> None:
        mgr = CryptoManager()
        plaintext = "sk-密钥-テスト-🔑"
        token = mgr.encrypt(plaintext)
        decrypted = mgr.decrypt(token)
        self.assertEqual(decrypted, plaintext)

    def test_decrypt_inverts_encrypt_empty_string(self) -> None:
        mgr = CryptoManager()
        plaintext = ""
        token = mgr.encrypt(plaintext)
        decrypted = mgr.decrypt(token)
        self.assertEqual(decrypted, plaintext)


class TestCryptoManagerNonce(unittest.TestCase):
    """Given a CryptoManager instance,
    When we encrypt the same plaintext twice,
    Then the two ciphertexts differ (random nonce).
    """

    def test_encrypt_produces_different_ciphertexts(self) -> None:
        mgr = CryptoManager()
        plaintext = "ghp_same_value"
        token1 = mgr.encrypt(plaintext)
        token2 = mgr.encrypt(plaintext)
        self.assertNotEqual(token1, token2)
        # Both must decrypt to the same plaintext
        self.assertEqual(mgr.decrypt(token1), plaintext)
        self.assertEqual(mgr.decrypt(token2), plaintext)


class TestCryptoManagerHash(unittest.TestCase):
    """Given a CryptoManager instance,
    When we hash a token value,
    Then the hash is 16 hex chars and stable across calls.
    """

    def test_hash_token_is_stable(self) -> None:
        mgr = CryptoManager()
        value = "ghp_test_token_hash"
        h1 = mgr.hash_token(value)
        h2 = mgr.hash_token(value)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_hash_token_differs_for_different_inputs(self) -> None:
        mgr = CryptoManager()
        h1 = mgr.hash_token("value_alpha")
        h2 = mgr.hash_token("value_beta")
        self.assertNotEqual(h1, h2)


class TestCryptoManagerInvalidToken(unittest.TestCase):
    """Given a tampered or truncated token,
    When we attempt to decrypt,
    Then decrypt raises a ValueError.
    """

    def test_decrypt_raises_on_tampered_token(self) -> None:
        mgr = CryptoManager()
        token = mgr.encrypt("secret")
        tampered = token[:-4] + "AAAA"
        with self.assertRaises(ValueError):
            mgr.decrypt(tampered)


class TestModuleLevelConvenience(unittest.TestCase):
    """Given the module-level convenience functions,
    When we call encrypt_str / decrypt_str,
    Then they round-trip correctly (internal singleton).
    """

    def test_encrypt_str_decrypt_str_roundtrip(self) -> None:
        plaintext = "ghp_module_level_test"
        token = encrypt_str(plaintext)
        decrypted = decrypt_str(token)
        self.assertEqual(decrypted, plaintext)

    def test_encrypt_str_produces_different_ciphertexts(self) -> None:
        plaintext = "ghp_module_test2"
        t1 = encrypt_str(plaintext)
        t2 = encrypt_str(plaintext)
        self.assertNotEqual(t1, t2)
