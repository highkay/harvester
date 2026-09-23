#!/usr/bin/env python3

"""Finding D regression: singleton-metadata cap truncation must be deterministic.

``extract()`` returns ``list(set)``, so when all three metadata axes are
singletons — true for every shipped config, which leaves
address_pattern/endpoint_pattern/model_pattern empty — the old
``fair_share = max(1, budget // len(keys))`` collapse to 1 turned the
MAX_SERVICES_PER_PAGE cap into "first 512 keys in set-iteration order": WHICH
keys were dropped changed per run. Keys are now sorted before the cap in that
shape, the WARNING states how many were dropped, and the multi-axis fair-share
path (currently unreachable with shipped configs) keeps input order untouched.
"""

from __future__ import annotations

import random
import unittest
from unittest import mock

from search import client as search_client


def _keys(n: int = 600) -> list:
    return [f"k{i:04d}" for i in range(n)]


class TestSingletonMetadataTruncationDeterminism(unittest.TestCase):
    def test_same_key_set_different_input_order_keeps_same_512(self):
        # Given the same key universe in two different (set-like) orders
        keys = _keys()
        order_a = keys[:]
        order_b = keys[:]
        random.Random(1).shuffle(order_a)
        random.Random(2).shuffle(order_b)
        self.assertNotEqual(order_a, order_b)  # guard: the orders really differ

        # When the cap truncates both
        with mock.patch.object(search_client.logger, "warning"):
            kept_a = search_client._build_service_candidates(order_a, [""], [""], [""], max_services=512)
            kept_b = search_client._build_service_candidates(order_b, [""], [""], [""], max_services=512)

        # Then exactly the same keys survive, in sorted order = sorted prefix
        self.assertEqual([c.key for c in kept_a], [c.key for c in kept_b])
        self.assertEqual(sorted(keys)[:512], [c.key for c in kept_a])
        self.assertEqual(512, len(kept_a))

    def test_warning_states_kept_and_dropped_counts(self):
        keys = _keys()  # 600 keys, cap 512 -> 88 dropped
        with mock.patch.object(search_client.logger, "warning") as warn:
            search_client._build_service_candidates(keys, [""], [""], [""], max_services=512)

        warn.assert_called_once()
        message = warn.call_args[0][0]
        self.assertIn("truncated", message)  # keeps the existing pin in test_service_product_cap
        self.assertIn("kept 512", message)
        self.assertIn("dropped ~88", message)
        self.assertIn("sorted", message)  # determinism is stated in the warning

    def test_no_warning_below_cap(self):
        with mock.patch.object(search_client.logger, "warning") as warn:
            candidates = search_client._build_service_candidates(_keys(10), [""], [""], [""], max_services=512)

        self.assertEqual(10, len(candidates))
        warn.assert_not_called()

    def test_below_cap_singleton_output_is_sorted(self):
        candidates = search_client._build_service_candidates(["k3", "k1", "k2"], ["a"], ["e"], ["m"])

        self.assertEqual(["k1", "k2", "k3"], [c.key for c in candidates])

    def test_multi_axis_metadata_keeps_input_key_order(self):
        # The fair-share path (multi-value metadata axes — unreachable with
        # shipped configs) must NOT gain the sort: existing pins rely on
        # input/product order there.
        candidates = search_client._build_service_candidates(["k2", "k1"], ["a"], ["e"], ["m1", "m2"])

        self.assertEqual(["k2", "k2", "k1", "k1"], [c.key for c in candidates])
        self.assertEqual(["m1", "m2", "m1", "m2"], [c.model for c in candidates])

    def test_default_cap_truncates_deterministically(self):
        # Same assertion through the real module constant (MAX_SERVICES_PER_PAGE=512)
        keys = _keys(600)
        reversed_keys = keys[::-1]
        with mock.patch.object(search_client.logger, "warning"):
            kept = search_client._build_service_candidates(reversed_keys, [""], [""], [""], max_services=None)

        self.assertEqual(512, len(kept))
        self.assertEqual(sorted(keys)[:512], [c.key for c in kept])


if __name__ == "__main__":
    unittest.main()
