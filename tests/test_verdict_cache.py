#!/usr/bin/env python3
"""Unit tests for the bounded persistent terminal-verdict cache.

The cache is deliberately dumb about providers: it only persists explicit
INVALID_KEY / NO_QUOTA verdicts, expires them on read and on write compaction,
and fails open on every malformed/corrupt/stale state. Time is always injected,
so no test sleeps or races the wall clock.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from core.enums import ErrorReason
from storage.verdict_cache import (
    INVALID_KEY_TTL_SECONDS,
    NO_QUOTA_TTL_SECONDS,
    VERDICT_TTLS,
    VerdictCache,
    make_cache_key,
    make_fingerprint,
    normalize_base_url,
)

TASK = "mimo-cn"
KEY = "sk-test-0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.com/v1"
ENDPOINT = "/chat/completions"
PROBE_MODEL = "mimo-v2.6-pro"
TIER = "flagship"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _identity(**overrides):
    values = {
        "task": TASK,
        "base_url": BASE_URL,
        "endpoint": ENDPOINT,
        "probe_model": PROBE_MODEL,
        "tier": TIER,
    }
    values.update(overrides)
    return values


def _store_kwargs(**overrides):
    values = {"key": KEY}
    values.update(_identity(**overrides))
    return values


def _lookup_kwargs(**overrides):
    return _store_kwargs(**overrides)


class VerdictCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name
        self.clock = FakeClock()
        self.cache = VerdictCache(self.workspace, clock=self.clock)

    def _new_cache(self) -> VerdictCache:
        return VerdictCache(self.workspace, clock=self.clock)

    def _entries(self, cache: VerdictCache | None = None, task: str = TASK):
        cache = cache or self.cache
        with open(cache.task_path(task), encoding="utf-8") as handle:
            data = json.load(handle)
        assert isinstance(data, dict)
        return data

    def _temp_files(self, cache: VerdictCache | None = None):
        cache = cache or self.cache
        if not os.path.isdir(cache.directory):
            return []
        return sorted(name for name in os.listdir(cache.directory) if name.endswith(".tmp"))

    def test_ttl_constants(self):
        self.assertEqual(INVALID_KEY_TTL_SECONDS, 30 * 24 * 60 * 60)
        self.assertEqual(NO_QUOTA_TTL_SECONDS, 14 * 24 * 60 * 60)
        self.assertEqual(
            VERDICT_TTLS,
            {
                ErrorReason.INVALID_KEY.value: INVALID_KEY_TTL_SECONDS,
                ErrorReason.NO_QUOTA.value: NO_QUOTA_TTL_SECONDS,
            },
        )

    def test_round_trip_and_persisted_shape(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))

        entry = self.cache.lookup(**_lookup_kwargs())
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.verdict, ErrorReason.INVALID_KEY.value)
        self.assertEqual(entry.observed_at, self.clock.now)
        expected_fingerprint = make_fingerprint(**_identity())
        self.assertEqual(entry.fingerprint, expected_fingerprint)

        raw = self._entries()
        self.assertEqual(len(raw), 1)
        cache_key, value = next(iter(raw.items()))
        self.assertEqual(cache_key, make_cache_key(**_store_kwargs()))
        self.assertIsInstance(value, dict)
        assert isinstance(value, dict)
        self.assertEqual(set(value), {"verdict", "observed_at", "fingerprint"})
        self.assertEqual(value["verdict"], ErrorReason.INVALID_KEY.value)
        self.assertEqual(value["observed_at"], self.clock.now)
        self.assertEqual(value["fingerprint"], expected_fingerprint)
        self.assertEqual(self._temp_files(), [])

    def test_no_quota_round_trip(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.NO_QUOTA))
        entry = self.cache.lookup(**_lookup_kwargs())
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.verdict, ErrorReason.NO_QUOTA.value)

    def test_exact_ttl_boundaries(self):
        cases = (
            (ErrorReason.INVALID_KEY, INVALID_KEY_TTL_SECONDS),
            (ErrorReason.NO_QUOTA, NO_QUOTA_TTL_SECONDS),
        )
        for verdict, ttl in cases:
            with self.subTest(verdict=verdict):
                self.clock.now = 1_000_000.0
                with tempfile.TemporaryDirectory() as workspace:
                    cache = VerdictCache(workspace, clock=self.clock)
                    self.assertTrue(cache.store(**_store_kwargs(), verdict=verdict))

                    self.clock.now += ttl - 1
                    self.assertIsNotNone(cache.lookup(**_lookup_kwargs()))

                    self.clock.now += 1  # exactly at the TTL: the lifetime is over
                    self.assertIsNone(cache.lookup(**_lookup_kwargs()))

                    self.clock.now += 1
                    self.assertIsNone(cache.lookup(**_lookup_kwargs()))

    def test_expired_lookup_keeps_the_file(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        path = self.cache.task_path(TASK)
        self.clock.now += INVALID_KEY_TTL_SECONDS

        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(self._entries()), 1)

    def test_write_compaction_prunes_expired_entries(self):
        start = self.clock.now
        key_invalid_old = "sk-old-invalid"
        key_no_quota = "sk-no-quota"
        key_invalid_new = "sk-new-invalid"
        key_no_quota_new = "sk-new-no-quota"

        self.assertTrue(
            self.cache.store(**_store_kwargs(key=key_invalid_old), verdict=ErrorReason.INVALID_KEY)
        )
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=key_no_quota), verdict=ErrorReason.NO_QUOTA)
        )
        self.assertEqual(len(self._entries()), 2)

        # At exactly 14d the NO_QUOTA entry has expired; INVALID_KEY is fresh.
        self.clock.now = start + NO_QUOTA_TTL_SECONDS
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=key_invalid_new), verdict=ErrorReason.INVALID_KEY)
        )
        compacted = self._entries()
        self.assertIn(make_cache_key(**_store_kwargs(key=key_invalid_old)), compacted)
        self.assertNotIn(make_cache_key(**_store_kwargs(key=key_no_quota)), compacted)
        self.assertIn(make_cache_key(**_store_kwargs(key=key_invalid_new)), compacted)

        # At exactly 30d the first INVALID_KEY entry has expired too.
        self.clock.now = start + INVALID_KEY_TTL_SECONDS
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=key_no_quota_new), verdict=ErrorReason.NO_QUOTA)
        )
        compacted = self._entries()
        self.assertNotIn(make_cache_key(**_store_kwargs(key=key_invalid_old)), compacted)
        self.assertIn(make_cache_key(**_store_kwargs(key=key_invalid_new)), compacted)
        self.assertIn(make_cache_key(**_store_kwargs(key=key_no_quota_new)), compacted)

    def test_model_region_address_and_endpoint_invalidation(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))

        variants = {
            "probe_model": {"probe_model": "mimo-v2.0"},
            "tier": {"tier": "legacy"},
            "region": {"base_url": "https://eu.api.example.com/v1"},
            "address": {"base_url": "https://api.example.com/v2"},
            "endpoint": {"endpoint": "/responses"},
            "task": {"task": "qwen-cn"},
        }
        for name, overrides in variants.items():
            with self.subTest(name=name):
                self.assertIsNone(self.cache.lookup(**_lookup_kwargs(**overrides)))

        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs()))

    def test_safe_url_normalization_variants_hit(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))

        self.assertIsNotNone(
            self.cache.lookup(**_lookup_kwargs(base_url="HTTPS://API.EXAMPLE.COM:443/v1/"))
        )
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(endpoint="chat/completions")))
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(probe_model="  mimo-v2.6-pro  ")))
        self.assertEqual(normalize_base_url("HTTPS://API.EXAMPLE.COM:443/v1/"), "https://api.example.com/v1")

    def test_corrupt_json_fails_open_without_raising(self):
        path = self.cache.task_path(TASK)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"broken":')
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00not-utf8")
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[]")
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        self.assertTrue(os.path.exists(path))

    def test_fingerprint_mismatch_fails_open(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        path = self.cache.task_path(TASK)
        raw = self._entries()
        cache_key = next(iter(raw))
        value = raw[cache_key]
        assert isinstance(value, dict)
        value["fingerprint"] = "0" * 64
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))
        self.assertTrue(os.path.exists(path))

    def test_tampered_forbidden_verdict_fails_open(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        path = self.cache.task_path(TASK)
        raw = self._entries()
        cache_key = next(iter(raw))
        value = raw[cache_key]
        assert isinstance(value, dict)
        value["verdict"] = ErrorReason.NO_ACCESS.value
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

    def test_forbidden_verdicts_are_never_stored(self):
        forbidden = [
            ErrorReason.UNKNOWN,
            ErrorReason.NO_MODEL,
            ErrorReason.NO_ACCESS,
            ErrorReason.BAD_REQUEST,
            ErrorReason.RATE_LIMITED,
            ErrorReason.TIMEOUT,
            ErrorReason.NETWORK_ERROR,
            ErrorReason.SERVER_ERROR,
            ErrorReason.SERVICE_UNAVAILABLE,
            ErrorReason.UNAUTHORIZED,
            ErrorReason.FORBIDDEN,
            ErrorReason.NOT_FOUND,
            ErrorReason.INVALID_TOKEN,
            "valid",
            "available",
            "",
        ]
        for verdict in forbidden:
            with self.subTest(verdict=verdict):
                self.assertFalse(self.cache.store(**_store_kwargs(), verdict=verdict))
                self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        self.assertFalse(os.path.exists(self.cache.directory))

    def test_malformed_identity_inputs_fail_open(self):
        self.assertFalse(self.cache.store(**_store_kwargs(key=""), verdict=ErrorReason.INVALID_KEY))
        self.assertFalse(self.cache.store(**_store_kwargs(task="   "), verdict=ErrorReason.INVALID_KEY))
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs(key="")))
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs(task="")))

    def test_concurrent_writers_never_lose_entries(self):
        writer_count = 8
        per_writer = 20
        barrier = threading.Barrier(writer_count)

        def writer(index: int) -> None:
            barrier.wait()
            for item in range(per_writer):
                stored = self.cache.store(
                    **_store_kwargs(key=f"sk-concurrent-{index}-{item}"),
                    verdict=ErrorReason.INVALID_KEY,
                )
                if not stored:
                    raise AssertionError(f"store failed for writer {index} item {item}")

        with ThreadPoolExecutor(max_workers=writer_count) as pool:
            futures = [pool.submit(writer, index) for index in range(writer_count)]
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(len(self._entries()), writer_count * per_writer)
        for index in range(writer_count):
            for item in range(per_writer):
                entry = self.cache.lookup(**_lookup_kwargs(key=f"sk-concurrent-{index}-{item}"))
                self.assertIsNotNone(entry)

    def test_atomic_temp_cleanup_on_failure_and_recovery(self):
        with mock.patch("storage.atomic.os.replace", side_effect=OSError("simulated replace failure")):
            self.assertFalse(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))

        self.assertEqual(self._temp_files(), [])
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        self.assertEqual(self._temp_files(), [])
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs()))

    def test_failed_write_preserves_previous_entries(self):
        kept_key = "sk-kept-before-failure"
        new_key = "sk-attempted-during-failure"
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=kept_key), verdict=ErrorReason.INVALID_KEY)
        )

        with mock.patch("storage.atomic.os.replace", side_effect=OSError("simulated replace failure")):
            self.assertFalse(
                self.cache.store(**_store_kwargs(key=new_key), verdict=ErrorReason.INVALID_KEY)
            )

        self.assertEqual(self._temp_files(), [])
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(key=kept_key)))
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs(key=new_key)))

        self.assertTrue(
            self.cache.store(**_store_kwargs(key=new_key), verdict=ErrorReason.INVALID_KEY)
        )
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(key=kept_key)))
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(key=new_key)))

    def test_missing_lookup_does_not_create_storage(self):
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))
        self.assertFalse(os.path.exists(self.cache.directory))

    def test_no_arbitrary_entry_cap_keeps_fresh_entries(self):
        total = 200
        for index in range(total):
            self.assertTrue(
                self.cache.store(
                    **_store_kwargs(key=f"sk-cap-{index}"), verdict=ErrorReason.INVALID_KEY
                )
            )

        self.assertEqual(len(self._entries()), total)
        for index in range(total):
            self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs(key=f"sk-cap-{index}")))

    def test_malformed_file_is_replaced_by_the_next_write(self):
        path = self.cache.task_path(TASK)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(self.cache.lookup(**_lookup_kwargs()))

        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        self.assertIsNotNone(self.cache.lookup(**_lookup_kwargs()))
        self.assertEqual(len(self._entries()), 1)

    def test_corrupt_observed_at_is_skipped_during_compaction(self):
        kept_key = "sk-kept-entry"
        new_key = "sk-new-entry"
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=kept_key), verdict=ErrorReason.INVALID_KEY)
        )

        path = self.cache.task_path(TASK)
        raw = self._entries()
        raw["f" * 64] = {
            "verdict": ErrorReason.INVALID_KEY.value,
            "observed_at": 10**400,
            "fingerprint": "f" * 64,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

        self.assertTrue(
            self.cache.store(**_store_kwargs(key=new_key), verdict=ErrorReason.INVALID_KEY)
        )
        compacted = self._entries()
        self.assertIn(make_cache_key(**_store_kwargs(key=kept_key)), compacted)
        self.assertIn(make_cache_key(**_store_kwargs(key=new_key)), compacted)
        self.assertNotIn("f" * 64, compacted)

    def test_same_identity_updates_verdict_and_timestamp(self):
        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.INVALID_KEY))
        self.clock.now += 30

        self.assertTrue(self.cache.store(**_store_kwargs(), verdict=ErrorReason.NO_QUOTA))
        entry = self.cache.lookup(**_lookup_kwargs())
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.verdict, ErrorReason.NO_QUOTA.value)
        self.assertEqual(entry.observed_at, 1_000_030.0)
        self.assertEqual(len(self._entries()), 1)

    def test_tasks_are_stored_in_independent_files(self):
        self.assertTrue(
            self.cache.store(**_store_kwargs(task="mimo-cn"), verdict=ErrorReason.INVALID_KEY)
        )
        self.assertTrue(
            self.cache.store(**_store_kwargs(task="qwen-cn"), verdict=ErrorReason.NO_QUOTA)
        )

        self.assertNotEqual(
            self.cache.task_path("mimo-cn"),
            self.cache.task_path("qwen-cn"),
        )
        mimo = self.cache.lookup(**_lookup_kwargs(task="mimo-cn"))
        qwen = self.cache.lookup(**_lookup_kwargs(task="qwen-cn"))
        self.assertIsNotNone(mimo)
        self.assertIsNotNone(qwen)
        assert mimo is not None and qwen is not None
        self.assertEqual(mimo.verdict, ErrorReason.INVALID_KEY.value)
        self.assertEqual(qwen.verdict, ErrorReason.NO_QUOTA.value)

    def test_no_plaintext_key_or_body_bytes_are_persisted(self):
        secret_key = "sk-proj-SECRETMATERIAL0123456789abcdef"
        self.assertTrue(
            self.cache.store(**_store_kwargs(key=secret_key), verdict=ErrorReason.INVALID_KEY)
        )
        self.assertTrue(
            self.cache.store(
                **_store_kwargs(key=secret_key + "-two"), verdict=ErrorReason.NO_QUOTA
            )
        )

        blobs: list[bytes] = []
        for root, _dirs, files in os.walk(self.cache.directory):
            for name in files:
                self.assertNotIn(secret_key, name)
                with open(os.path.join(root, name), "rb") as handle:
                    blobs.append(handle.read())

        self.assertTrue(blobs)
        needle = secret_key.encode("utf-8")
        for blob in blobs:
            self.assertNotIn(needle, blob)
            self.assertNotIn(b"SECRETMATERIAL", blob)


if __name__ == "__main__":
    unittest.main()