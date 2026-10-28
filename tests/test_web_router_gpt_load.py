#!/usr/bin/env python3

"""Tests for web/router_gpt_load.py list_groups envelope normalization.

gpt-load v1 returned ``{code: 0, data: [...]}``; v2 returns a paginated
envelope ``{code: 0, data: {items: [...], ...}}``. The push-config UI iterates
the response as a bare list, so the proxy must normalize both shapes.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from web.crypto import encrypt_str
from web.db import init_db as _init_db_async


def _seed_config(db_path: str) -> int:
    asyncio.run(_init_db_async(db_path))
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO gpt_load_config (name, base_url, auth_key_encrypted) "
            "VALUES (?, ?, ?)",
            ("test", "http://127.0.0.1:9", encrypt_str("test-auth-key")),
        )
        conn.commit()
        rowid = cur.lastrowid
        assert rowid is not None
        return int(rowid)
    finally:
        conn.close()


def _resp200(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class TestListGroupsEnvelope(unittest.TestCase):
    def _call(self, payload: dict) -> list:
        from web.router_gpt_load import list_groups

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "t.db")
            config_id = _seed_config(db_path)
            with (
                patch("web.router_gpt_load.resolve_db_path", return_value=db_path),
                patch(
                    "web.router_gpt_load.requests.get",
                    return_value=_resp200(payload),
                ),
            ):
                return asyncio.run(list_groups(config_id, _user=True))

    def test_v1_bare_list_shape_passes_through(self) -> None:
        groups = [{"id": 2, "name": "deepseek"}]
        result = self._call({"code": 0, "data": groups})
        self.assertEqual(result, groups)

    def test_v2_envelope_items_unwrapped(self) -> None:
        items = [{"id": 3, "name": "deepseek"}]
        payload = {
            "code": 0,
            "data": {
                "observed_at_ms": 1,
                "summary": {"total": 1},
                "items": items,
                "pagination": {"page": 1},
            },
        }
        result = self._call(payload)
        self.assertEqual(result, items)

    def test_v2_empty_items(self) -> None:
        result = self._call({"code": 0, "data": {"items": []}})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
