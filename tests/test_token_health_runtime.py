#!/usr/bin/env python3

"""TDD tests for runtime detection + quarantine of revoked GitHub tokens.

Covers ``tools/state.py`` (dead registry + 401 strikes), ``tools/credential.py``
(skip dead credentials; loud all-dead ERROR) and ``search/client.py`` (three
consecutive API 401s quarantine the credential; a success revives it).

No network: the search client's ``request`` is patched.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from constant.system import SERVICE_TYPE_GITHUB_API
from tools.state import github_credential_state


def _reset_gh_state() -> None:
    """Clear the process-wide credential registry between tests."""
    github_credential_state._dead.clear()
    github_credential_state._strikes.clear()
    github_credential_state._items.clear()


def _resp(status: int, headers: dict | None = None, content: bytes = b"{}"):
    """Build a minimal fake requests.Response."""
    fake = MagicMock()
    fake.status_code = status
    fake.headers = headers or {}
    fake.content = content
    try:
        fake.text = content.decode("utf-8")
    except Exception:
        fake.text = ""
    fake.reason = ""
    return fake


# ---------------------------------------------------------------------------
# C. Runtime quarantine — tools/state
# ---------------------------------------------------------------------------


class TestCredentialStateQuarantine(unittest.TestCase):
    """C. mark_dead / is_dead / mark_alive + strike counter."""

    def setUp(self) -> None:
        _reset_gh_state()

    def test_mark_dead_and_alive_roundtrip(self) -> None:
        self.assertFalse(github_credential_state.is_dead("svc", "cred"))
        github_credential_state.mark_dead("svc", "cred")
        self.assertTrue(github_credential_state.is_dead("svc", "cred"))
        github_credential_state.mark_alive("svc", "cred")
        self.assertFalse(github_credential_state.is_dead("svc", "cred"))

    def test_strikes_increment_and_reset_on_alive(self) -> None:
        self.assertEqual(github_credential_state.mark_strike("svc", "cred"), 1)
        self.assertEqual(github_credential_state.mark_strike("svc", "cred"), 2)
        github_credential_state.mark_alive("svc", "cred")
        self.assertEqual(github_credential_state.mark_strike("svc", "cred"), 1)

    def test_mark_dead_clears_strikes(self) -> None:
        github_credential_state.mark_strike("svc", "cred")
        github_credential_state.mark_strike("svc", "cred")
        github_credential_state.mark_dead("svc", "cred")
        self.assertTrue(github_credential_state.is_dead("svc", "cred"))
        self.assertEqual(github_credential_state.mark_strike("svc", "cred"), 1)

    def test_all_dead(self) -> None:
        self.assertFalse(github_credential_state.all_dead("svc", ["a", "b"]))
        github_credential_state.mark_dead("svc", "a")
        self.assertFalse(github_credential_state.all_dead("svc", ["a", "b"]))
        github_credential_state.mark_dead("svc", "b")
        self.assertTrue(github_credential_state.all_dead("svc", ["a", "b"]))
        self.assertFalse(github_credential_state.all_dead("svc", []))

    def test_is_dead_isolated_per_service(self) -> None:
        github_credential_state.mark_dead("svc1", "cred")
        self.assertTrue(github_credential_state.is_dead("svc1", "cred"))
        self.assertFalse(github_credential_state.is_dead("svc2", "cred"))


class TestCredentialDeadSkip(unittest.TestCase):
    """C. Credentials._get_available never hands out a dead credential."""

    def setUp(self) -> None:
        _reset_gh_state()

    def test_dead_credential_never_handed_out(self) -> None:
        from tools.credential import Credentials

        creds = Credentials([], ["tok_dead_11111111", "tok_live_22222222"])
        github_credential_state.mark_dead(SERVICE_TYPE_GITHUB_API, "tok_dead_11111111")

        seen = {creds.get_token() for _ in range(8)}
        self.assertEqual(seen, {"tok_live_22222222"})

    def test_all_dead_logs_error_and_waits(self) -> None:
        from tools.credential import Credentials

        creds = Credentials([], ["tok_a_11111111", "tok_b_22222222"])
        github_credential_state.mark_dead(SERVICE_TYPE_GITHUB_API, "tok_a_11111111")
        github_credential_state.mark_dead(SERVICE_TYPE_GITHUB_API, "tok_b_22222222")

        class _StopLoop(Exception):
            pass

        with patch(
            "tools.credential.time.sleep", side_effect=_StopLoop()
        ), self.assertLogs("manager", level="ERROR") as cm:
            with self.assertRaises(_StopLoop):
                creds.get_token()

        joined = "\n".join(cm.output)
        self.assertIn("DEAD", joined)
        self.assertNotIn("tok_a_11111111", joined)
        self.assertNotIn("tok_b_22222222", joined)


# ---------------------------------------------------------------------------
# C. Runtime quarantine — search/client.py
# ---------------------------------------------------------------------------


class TestClientDeadTokenDetection(unittest.TestCase):
    """C. 3 consecutive API 401s quarantine the credential; a success revives."""

    def setUp(self) -> None:
        _reset_gh_state()

    def _client(self):
        from search.client import GitHubClient

        return GitHubClient()

    def test_three_consecutive_401s_mark_dead(self) -> None:
        import search.client as sc
        from core.exceptions import NetworkError

        cred = "ghp_revoked_token_ABCDEF"
        headers = {"Authorization": f"Bearer {cred}"}
        client = self._client()

        with patch.object(sc, "request", return_value=_resp(401)):
            for _ in range(3):
                with self.assertRaises(NetworkError):
                    client.get_with_headers(
                        "https://api.github.com/user", headers=headers
                    )

        self.assertTrue(github_credential_state.is_dead(SERVICE_TYPE_GITHUB_API, cred))

    def test_two_401s_not_dead(self) -> None:
        import search.client as sc
        from core.exceptions import NetworkError

        cred = "ghp_half_bad_token_ABCDEF"
        headers = {"Authorization": f"Bearer {cred}"}
        client = self._client()

        with patch.object(sc, "request", return_value=_resp(401)):
            for _ in range(2):
                with self.assertRaises(NetworkError):
                    client.get_with_headers(
                        "https://api.github.com/user", headers=headers
                    )

        self.assertFalse(github_credential_state.is_dead(SERVICE_TYPE_GITHUB_API, cred))

    def test_success_clears_dead_state(self) -> None:
        import search.client as sc

        cred = "ghp_recovered_token_ABCDEF"
        headers = {"Authorization": f"Bearer {cred}"}
        github_credential_state.mark_dead(SERVICE_TYPE_GITHUB_API, cred)
        client = self._client()

        with patch.object(sc, "request", return_value=_resp(200, content=b'{"ok":true}')), patch.object(
            sc, "_QUOTA_TRACKER", None
        ):
            client.get_with_headers(
                "https://api.github.com/user", headers=headers, credential=cred
            )

        self.assertFalse(github_credential_state.is_dead(SERVICE_TYPE_GITHUB_API, cred))

    def test_403_does_not_strike(self) -> None:
        import search.client as sc
        from core.exceptions import NetworkError

        cred = "ghp_forbidden_token_ABCDEF"
        headers = {"Authorization": f"Bearer {cred}"}
        client = self._client()

        with patch.object(sc, "request", return_value=_resp(403)):
            for _ in range(3):
                with self.assertRaises(NetworkError):
                    client.get_with_headers(
                        "https://api.github.com/user", headers=headers
                    )

        self.assertFalse(github_credential_state.is_dead(SERVICE_TYPE_GITHUB_API, cred))


if __name__ == "__main__":
    unittest.main()