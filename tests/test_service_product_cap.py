#!/usr/bin/env python3

"""Service cross-product cap + de-duplication in search.client.collect().

``collect()`` used to build candidates with an unbounded
``itertools.product(keys, addresses, endpoints, models)``: one broadly
matching metadata pattern can explode the candidate count per page
(production measured ~84 candidates per link from a single broad branch),
and every extra Service is a validation request. The product is now capped
at ``MAX_SERVICES_PER_PAGE`` with the budget shared fairly across keys, and
identical Services are emitted once. Key EXTRACTION semantics are untouched
- the cap only applies to the combination fan-out.
"""

from __future__ import annotations

import unittest
from unittest import mock

from search import client as search_client


class TestBuildServiceCandidates(unittest.TestCase):
    def test_below_cap_keeps_full_product_and_legacy_order(self):
        candidates = search_client._build_service_candidates(
            ["k1", "k2"], ["a1"], ["e1"], ["m1", "m2"]
        )

        self.assertEqual(4, len(candidates))
        # key slowest-varying, model fastest - identical to itertools.product
        self.assertEqual(
            [("k1", "m1"), ("k1", "m2"), ("k2", "m1"), ("k2", "m2")],
            [(c.key, c.model) for c in candidates],
        )

    def test_cap_limits_total_and_shares_budget_across_keys(self):
        keys = [f"k{i}" for i in range(3)]
        addresses = [f"http://a{i}" for i in range(20)]

        with mock.patch.object(search_client.logger, "warning") as warn_mock:
            candidates = search_client._build_service_candidates(
                keys, addresses, [""], [""], max_services=6
            )

        self.assertEqual(6, len(candidates))
        # fairness: truncation drops metadata breadth before key coverage
        self.assertEqual(set(keys), {c.key for c in candidates})
        warn_mock.assert_called_once()
        self.assertIn("truncated", warn_mock.call_args[0][0])

    def test_identical_services_are_deduplicated(self):
        candidates = search_client._build_service_candidates(
            ["k", "k"], ["a", "a"], ["e"], ["m"]
        )

        self.assertEqual(1, len(candidates))

    def test_cap_below_key_count_still_enforces_hard_limit(self):
        candidates = search_client._build_service_candidates(
            ["k1", "k2"], ["a1", "a2"], ["e1"], ["m1"], max_services=1
        )

        self.assertEqual(1, len(candidates))

    def test_empty_dimension_yields_nothing(self):
        self.assertEqual([], search_client._build_service_candidates([], ["a"], ["e"], ["m"]))
        self.assertEqual([], search_client._build_service_candidates(["k"], [], ["e"], ["m"]))


class TestCollectCap(unittest.TestCase):
    def test_collect_respects_module_constant(self):
        text = " ".join(f"key{i}" for i in range(4))
        text += " " + " ".join(f"http://addr{i}" for i in range(50))

        with mock.patch.object(search_client, "MAX_SERVICES_PER_PAGE", 4):
            candidates = search_client.collect(
                key_pattern=r"key\d+",
                address_pattern=r"http://addr\d+",
                text=text,
            )

        self.assertEqual(4, len(candidates))
        self.assertEqual({f"key{i}" for i in range(4)}, {c.key for c in candidates})

    def test_collect_uncapped_normal_path_unchanged(self):
        candidates = search_client.collect(
            key_pattern=r"key\d+",
            address_pattern=r"http://addr\d+",
            endpoint_pattern=r"v\d+",
            text="key0 key1 http://addr0 v1",
        )

        self.assertEqual(2, len(candidates))
        self.assertEqual({"key0", "key1"}, {c.key for c in candidates})
        self.assertEqual({"http://addr0"}, {c.address for c in candidates})
        self.assertEqual({"v1"}, {c.endpoint for c in candidates})

    def test_default_cap_constant(self):
        self.assertEqual(512, search_client.MAX_SERVICES_PER_PAGE)


if __name__ == "__main__":
    unittest.main()
