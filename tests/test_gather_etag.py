#!/usr/bin/env python3

"""ETag conditional raw fetch + candidate replay for the GATHER stage.

Production measurement 2026-10-22: consecutive runs re-discover the SAME links
(overlap 78.8-92.2% — nvidia 92.2 / glm 84.1 / tavily 83.8 / exa 81.6 / brave
81.4) and every one of them was re-downloaded and re-extracted.
raw.githubusercontent.com answers ``If-None-Match`` with a **304 and a 0-byte
body** (probed this session), so the body download can go — but dropping the
extraction with it would cut ``materials_total`` / ``valid_keys_found`` by the
overlap fraction and stop the pool's periodic re-validation of known keys.

These tests pin the eleven acceptance cases of plan todo 3
(`.omo/plans/harvester-prod-fixes-20261022.md`):

  (i)    a first fetch stores the etag AND both rows, and returns the body;
  (ii)   a second fetch sends ``If-None-Match`` and a 304 RE-EMITS the stored
         candidates to CHECK with no download and no extractor call;
  (iii)  a 304 with no stored outcome for that (provider, pattern_fp) performs
         a normal full fetch;
  (iv)   a changed pattern fingerprint forces a full fetch even with a stored
         etag (and never replays the stale row);
  (v)    ``responses.db`` (the API-only body cache) is never touched;
  (vi)   a store-open failure degrades to a normal fetch (fail-open) and logs
         once;
  (vii)  a 404 is not cached;
  (viii) a first fetch that extracts NOTHING stores an explicit empty outcome
         row (``candidates='[]'`` + ``has_candidates=0``);
  (ix)   a second fetch that 304s on that empty row downloads NOTHING, never
         calls the extractor and emits NOTHING to CHECK (the R1 proof: the
         ~99% zero-candidate bulk case costs one request / zero body bytes);
  (x)    a changed pattern fingerprint with an empty row still forces a full
         fetch (the empty marker is keyed like any other outcome);
  (xi)   counters: ``gather_304_empty`` increments while
         ``gather_304_replayed`` stays 0 for the empty case, and the once-per
         run summary log line reports both.

Plus the two cross-run guardrails the plan lists as MUST NOT: no replay across
providers, and no skip on the strength of an empty etag.

Every test mocks the HTTP layer at ``search.client.request`` — the single seam
both ``http_get_raw`` and ``http_get`` go through — so no test touches the
network, while the real extraction (``client.collect``) still runs.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest import mock

import requests

from config.schemas import StageConfig, TaskConfig
from core.models import AcquisitionTask, Service
from search import client
from search.github.fetch_store import FetchStore, default_directory
from stage.base import StageOutput, StageResources
from stage.definition import AcquisitionStage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BLOB_URL = "https://github.com/owner/repo/blob/main/config/app.env"
RAW_URL = "https://raw.githubusercontent.com/owner/repo/main/config/app.env"
KEY = "sk-test-0123456789abcdef"
BODY = f'DEPSEEK_API_KEY="{KEY}"\nOTHER="nothing"\n'
PATTERN = r"sk-test-[0-9a-f]{16}"
# A fingerprint-different pattern that matches NOTHING in BODY: a replay of the
# stored row would still hand back KEY, so an empty result proves the body was
# re-fetched and re-extracted under the new patterns.
OTHER_PATTERN = r"sk-nomatch-[0-9a-f]{16}"
ETAG = 'W/"6d6561737572656431"'
# A NON-EMPTY body that yields no candidate: the R1 bulk case. Its length is
# deliberately > 0 so ``body_bytes_served == 0`` on a later 304 is proof that
# the body was skipped, not an artifact of a zero-length fixture.
NO_KEY_BODY = 'README="no keys here"\nOTHER="still nothing"\n'


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in for ``search.client.request``."""

    def __init__(self, status_code: int, content: bytes = b"", headers: Optional[dict] = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")
        self.reason = "OK" if status_code == 200 else f"status {status_code}"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error for url", response=self
            )

    def close(self) -> None:  # managed_network() calls this
        pass


class _FakeHttp:
    """The mocked HTTP layer: serves BODY once, then 304s on a matching etag.

    Records every request's headers so a test can assert ``If-None-Match`` was
    (or was not) sent, and counts the body bytes actually put on the wire —
    the quantity GAP-1 exists to reduce.
    """

    def __init__(self, body: str = BODY, etag: str = ETAG, not_found: bool = False):
        self.body = body
        self.etag = etag
        self.not_found = not_found
        self.calls: List[Tuple[str, dict]] = []
        self.body_bytes_served = 0

    def __call__(self, method: str, url: str, timeout: float = 10, use_proxy: bool = True, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        self.calls.append((url, headers))
        if self.not_found:
            response = _FakeResponse(404)
            response.raise_for_status()  # the real request() raises before returning
        validator = headers.get("If-None-Match")
        if validator and validator == self.etag:
            # 304 carries a 0-byte body BY CONTRACT — nothing is served.
            return _FakeResponse(304, b"", {"ETag": self.etag})
        self.body_bytes_served += len(self.body.encode("utf-8"))
        return _FakeResponse(
            200, self.body.encode("utf-8"), {"ETag": self.etag, "Content-Type": "text/plain"}
        )

    @property
    def validators_sent(self) -> List[str]:
        return [h.get("If-None-Match", "") for _u, h in self.calls]


class _Auth:
    """Minimal IAuthProvider stub (the gather worker needs no credentials)."""

    def get_session(self) -> Optional[str]:
        return ""

    def get_token(self) -> Optional[str]:
        return "token"

    def get_credential(self, prefer_token: bool = True) -> Tuple[str, str]:
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


class _RecordingHandler:
    def __init__(self) -> None:
        self.outputs: List[StageOutput] = []

    def __call__(self, output: StageOutput) -> None:
        self.outputs.append(output)


class EtagStoreTestCase(unittest.TestCase):
    """Shared scaffolding: a temp workspace, a real store, a mocked wire."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name
        self.store_dir = default_directory(self.workspace)
        self.db_path = os.path.join(self.store_dir, "fetch_store.db")
        ua = mock.patch("stage.definition.get_user_agent", return_value="test-agent")
        ua.start()
        self.addCleanup(ua.stop)

    # -- helpers ---------------------------------------------------------

    def _stage(self, provider: str = "deepseek") -> AcquisitionStage:
        resources = StageResources(
            limiter=mock.MagicMock(),
            providers={},
            config=SimpleNamespace(global_config=SimpleNamespace(workspace=self.workspace)),
            task_configs={
                provider: TaskConfig(name=provider, provider_type="openai_like", stages=StageConfig())
            },
            auth=_Auth(),
        )
        stage = AcquisitionStage(resources, handler=_RecordingHandler())
        # Every stage owns a sqlite connection: retire it before the temp
        # workspace is removed (Windows will not unlink an open file). Cleanups
        # run LIFO, so this lands before setUp's TemporaryDirectory.cleanup.
        self.addCleanup(stage.stop)
        return stage

    def _task(self, provider: str = "deepseek", pattern: str = PATTERN) -> AcquisitionTask:
        return AcquisitionTask(
            provider=provider, url=BLOB_URL, key_pattern=pattern, retries=1
        )

    def _run(
        self,
        stage: AcquisitionStage,
        http: _FakeHttp,
        task: Optional[AcquisitionTask] = None,
    ) -> Tuple[Optional[StageOutput], List[str]]:
        """One gather fetch over the mocked wire; returns (output, counters)."""
        outcomes: List[str] = []
        with mock.patch("search.client.request", http), mock.patch(
            "stage.definition.record_gather_outcome",
            side_effect=lambda _provider, outcome: outcomes.append(outcome),
        ):
            output = stage._acquisition_worker(task or self._task())
        return output, outcomes

    def _rows(self, table: str) -> int:
        """Row count read back through a SECOND connection (what a new run sees)."""
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def _fingerprint(self, pattern: str = PATTERN) -> str:
        return FetchStore.pattern_fingerprint(pattern, "", "", "")

    def _open_store(self) -> FetchStore:
        store = FetchStore(self.store_dir)
        self.addCleanup(store.close)
        return store


# ---------------------------------------------------------------------------
# (i) first fetch: etag stored, outcome stored, body returned
# ---------------------------------------------------------------------------


class TestFirstFetchStoresStateAndOutcome(EtagStoreTestCase):
    def test_i_first_fetch_stores_etag_and_outcome_and_returns_body(self):
        # Given an empty store and a raw file that carries one candidate
        http = _FakeHttp()
        stage = self._stage()

        # When the gather worker fetches it
        output, outcomes = self._run(stage, http)

        # Then the body was downloaded once, unconditionally
        self.assertEqual(1, len(http.calls))
        self.assertEqual(RAW_URL, http.calls[0][0])
        self.assertEqual([""], http.validators_sent)  # no If-None-Match on a first fetch
        self.assertEqual(len(BODY.encode("utf-8")), http.body_bytes_served)

        # And the extraction reached CHECK + MATERIAL (the body was returned)
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual([("deepseek", "material")], [(p, r) for p, r, _ in output.results])
        self.assertEqual([("deepseek", [BLOB_URL])], output.links)  # links.txt keeps the blob URL
        self.assertEqual(["gather_ok"], outcomes)

        # And BOTH rows exist: fetch_state (resource fact) + fetch_outcome (extraction fact)
        store = stage._fetch_store_instance()
        assert store is not None
        state = store.get_state(RAW_URL)
        self.assertIsNotNone(state)
        self.assertEqual(ETAG, state.etag)
        self.assertEqual(200, state.http_status)
        replayed = store.get_outcome("deepseek", RAW_URL, self._fingerprint())
        self.assertIsNotNone(replayed)
        self.assertEqual([KEY], [s.key for s in replayed])
        self.assertEqual(1, self._rows("fetch_state"))
        self.assertEqual(1, self._rows("fetch_outcome"))

    def test_viii_empty_extraction_stores_an_explicit_empty_outcome_row(self):
        # Given a body that extracts nothing
        http = _FakeHttp(body=NO_KEY_BODY)
        stage = self._stage()

        # When the gather worker fetches it
        output, outcomes = self._run(stage, http)

        # Then it was a normal empty gather ...
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertEqual([], output.results)
        self.assertEqual(["gather_empty"], outcomes)

        # ... and the extraction fact is stored EXPLICITLY, not omitted: a row
        # with candidates='[]' and has_candidates=0. This is the difference
        # between "we extracted and found nothing" and "we never extracted".
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertIsNotNone(store.get_state(RAW_URL))
        self.assertEqual([], store.get_outcome("deepseek", RAW_URL, self._fingerprint()))
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT candidates, has_candidates FROM fetch_outcome"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual("[]", row[0])
        self.assertEqual(0, int(row[1]))
        self.assertEqual(1, self._rows("fetch_state"))
        self.assertEqual(1, self._rows("fetch_outcome"))


# ---------------------------------------------------------------------------
# (ii) second fetch: If-None-Match -> 304 -> replay, no download, no extractor
# ---------------------------------------------------------------------------


class TestRevalidationReplaysStoredCandidates(EtagStoreTestCase):
    def test_ii_304_replays_candidates_without_download_or_extractor(self):
        # Given a previous run that fetched the file and stored both rows
        http = _FakeHttp()
        first = self._stage()
        self._run(first, http)
        first.stop()  # pipeline teardown: prune + close (the data must survive)
        bytes_after_first = http.body_bytes_served

        # When the NEXT run (a fresh stage over the same workspace) re-fetches it
        http2 = _FakeHttp()
        second = self._stage()
        with mock.patch.object(client, "collect") as collect_mock:
            output, outcomes = self._run(second, http2)
        second.stop()

        # Then the request was conditional and answered 304
        self.assertEqual(1, len(http2.calls), "a 304 must not trigger a second request")
        self.assertEqual([ETAG], http2.validators_sent)
        self.assertEqual(0, http2.body_bytes_served, "a 304 must download zero body bytes")
        self.assertEqual(bytes_after_first, http.body_bytes_served)

        # And the extractor never ran, yet the stored candidates were RE-EMITTED
        collect_mock.assert_not_called()
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual([("deepseek", "material")], [(p, r) for p, r, _ in output.results])
        self.assertEqual([KEY], [s.key for _p, _r, services in output.results for s in services])
        self.assertEqual([("deepseek", [BLOB_URL])], output.links)

        # And the counters show the 304 next to the work it produced
        self.assertIn("gather_304", outcomes)
        self.assertIn("gather_ok", outcomes)

    def test_replay_survives_a_third_run_and_refreshes_retention(self):
        # Given two completed runs
        for _ in range(2):
            stage = self._stage()
            self._run(stage, _FakeHttp())
            stage.stop()

        # When a third run revalidates
        http = _FakeHttp()
        stage = self._stage()
        with mock.patch.object(client, "collect") as collect_mock:
            output, outcomes = self._run(stage, http)
        stage.stop()

        # Then it still replays (no drift, no re-download)
        collect_mock.assert_not_called()
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual(0, http.body_bytes_served)
        self.assertIn("gather_304", outcomes)

    def test_stop_logs_the_run_summary_once_and_closes_the_store(self):
        # Given a run that saw one 304
        self._run(self._stage(), _FakeHttp())
        http = _FakeHttp()
        stage = self._stage()
        self._run(stage, http)

        # When the stage is torn down
        with self.assertLogs("stage", level="INFO") as logs:
            stage.stop()

        # Then exactly one summary line carries the grep-able counter
        summary = [r.getMessage() for r in logs.records if "gather_304=" in r.getMessage()]
        self.assertEqual(1, len(summary), summary)
        self.assertIn("gather_304=1", summary[0])
        self.assertIn("gather_304_replayed=1", summary[0])
        self.assertIn("gather_304_empty=0", summary[0])
        # And the store is closed (a second stop is a safe no-op)
        stage.stop()


# ---------------------------------------------------------------------------
# (ix)(x)(xi) 304 on an explicit empty outcome: no download, no extract, no CHECK
# ---------------------------------------------------------------------------


class TestEmptyOutcomeSkipsTheNextFetch(EtagStoreTestCase):
    """R1 proof: the zero-candidate bulk case costs one request, 0 body bytes."""

    def test_ix_304_on_an_empty_outcome_skips_download_extractor_and_check(self):
        # Given a previous run that fetched a body and extracted NOTHING
        first = self._stage()
        self._run(first, _FakeHttp(body=NO_KEY_BODY))
        first.stop()

        # When the next run revalidates the unchanged file
        http = _FakeHttp(body=NO_KEY_BODY)
        stage = self._stage()
        with mock.patch.object(client, "collect") as collect_mock:
            output, outcomes = self._run(stage, http)

        # Then the wire saw exactly ONE conditional request and ZERO body bytes
        self.assertEqual(1, len(http.calls), "an empty outcome must not fall back to a full fetch")
        self.assertEqual([ETAG], http.validators_sent)
        self.assertEqual(0, http.body_bytes_served)
        self.assertLess(0, len(NO_KEY_BODY.encode("utf-8")), "fixture must be non-trivial")
        collect_mock.assert_not_called()

        # And nothing was emitted to CHECK (an empty outcome has no candidates)
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertEqual([], output.results)
        self.assertEqual([("deepseek", [BLOB_URL])], output.links)

        # And the counters split it apart: 304 yes, empty skip yes, replay no.
        self.assertEqual(1, outcomes.count("gather_304"))
        self.assertEqual(1, outcomes.count("gather_304_empty"))
        self.assertNotIn("gather_304_replayed", outcomes)
        self.assertEqual(1, outcomes.count("gather_empty"))
        self.assertNotIn("gather_ok", outcomes)

    def test_x_changed_fingerprint_never_reuses_an_empty_outcome(self):
        # Given a stored empty outcome under PATTERN
        first = self._stage()
        self._run(first, _FakeHttp(body=NO_KEY_BODY))
        first.stop()

        # When the next run asks under DIFFERENT patterns
        http = _FakeHttp(body=NO_KEY_BODY)
        stage = self._stage()
        output, outcomes = self._run(stage, http, self._task(pattern=OTHER_PATTERN))

        # Then the marker is not reused: a full fetch ran (2 requests, body served)
        self.assertEqual(2, len(http.calls), "an empty row must not launder a fingerprint change")
        self.assertEqual([ETAG, ""], http.validators_sent)
        self.assertEqual(len(NO_KEY_BODY.encode("utf-8")), http.body_bytes_served)
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertIn("gather_304", outcomes)
        self.assertNotIn("gather_304_empty", outcomes)
        self.assertNotIn("gather_304_replayed", outcomes)

        # And each fingerprint owns its own (empty) row
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertEqual([], store.get_outcome("deepseek", RAW_URL, self._fingerprint(OTHER_PATTERN)))
        self.assertEqual([], store.get_outcome("deepseek", RAW_URL, self._fingerprint()))
        self.assertEqual(2, self._rows("fetch_outcome"))

    def test_xi_summary_separates_empty_skips_from_replays(self):
        # Given a run that 304-skipped one known-empty link
        first = self._stage()
        self._run(first, _FakeHttp(body=NO_KEY_BODY))
        first.stop()
        stage = self._stage()
        _, outcomes = self._run(stage, _FakeHttp(body=NO_KEY_BODY))

        # Then the counter surface records the empty bucket only ...
        self.assertEqual(1, outcomes.count("gather_304_empty"))
        self.assertEqual(0, outcomes.count("gather_304_replayed"))

        # ... and the once-per-run summary reports both splits next to gather_304
        with self.assertLogs("stage", level="INFO") as logs:
            stage.stop()
        summary = [r.getMessage() for r in logs.records if "gather_304=" in r.getMessage()]
        self.assertEqual(1, len(summary), summary)
        self.assertIn("gather_304=1", summary[0])
        self.assertIn("gather_304_replayed=0", summary[0])
        self.assertIn("gather_304_empty=1", summary[0])


# ---------------------------------------------------------------------------
# (iii) 304 with no stored outcome -> normal full fetch
# ---------------------------------------------------------------------------


class TestRevalidationWithoutOutcomeFallsBack(EtagStoreTestCase):
    def test_iii_304_without_stored_outcome_does_a_full_fetch(self):
        # Given a resource fact (etag) but NO extraction fact for this
        # (provider, pattern_fp) — a state row whose outcome row does not
        # exist (e.g. written by a build that predates the outcome table)
        seeded = self._open_store()
        self.assertTrue(seeded.put_state(RAW_URL, ETAG, 200))
        seeded.close()

        # When the gather worker revalidates
        http = _FakeHttp()
        stage = self._stage()
        output, outcomes = self._run(stage, http)

        # Then it got a 304, fell back to a normal UNCONDITIONAL full fetch,
        # and extracted from the downloaded body
        self.assertEqual(2, len(http.calls), "304 + full fetch = two requests")
        self.assertEqual([ETAG, ""], http.validators_sent)
        self.assertEqual(len(BODY.encode("utf-8")), http.body_bytes_served)
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertIn("gather_304", outcomes)
        self.assertIn("gather_ok", outcomes)

        # And the outcome is stored now, so the NEXT run replays instead
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertEqual([KEY], [s.key for s in store.get_outcome("deepseek", RAW_URL, self._fingerprint())])
        stage.stop()

        http2 = _FakeHttp()
        next_run = self._stage()
        with mock.patch.object(client, "collect") as collect_mock:
            output2, _ = self._run(next_run, http2)
        collect_mock.assert_not_called()
        assert output2 is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output2.new_tasks])
        self.assertEqual(0, http2.body_bytes_served)


# ---------------------------------------------------------------------------
# (iv) a changed pattern fingerprint forces a full fetch
# ---------------------------------------------------------------------------


class TestPatternFingerprintChange(EtagStoreTestCase):
    def test_iv_changed_fingerprint_forces_full_fetch_and_never_replays(self):
        # Given a run that stored candidates under PATTERN
        first = self._stage()
        self._run(first, _FakeHttp())
        first.stop()

        # When the next run fetches the same URL under DIFFERENT patterns
        # (the etag is unchanged, so the wire answers 304)
        http = _FakeHttp()
        stage = self._stage()
        output, outcomes = self._run(stage, http, self._task(pattern=OTHER_PATTERN))

        # Then the stale row was NOT replayed: a full fetch ran and the new
        # patterns genuinely matched nothing in the body
        self.assertEqual(2, len(http.calls), "a fingerprint change must re-download")
        self.assertEqual([ETAG, ""], http.validators_sent)
        self.assertEqual(len(BODY.encode("utf-8")), http.body_bytes_served)
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertIn("gather_304", outcomes)
        self.assertIn("gather_empty", outcomes)

        # And the old row is still there, untouched, while the empty extraction
        # under OTHER_PATTERN wrote its own explicit empty row: one outcome row
        # per fingerprint, so an empty marker can never launder a pattern change
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertEqual([], store.get_outcome("deepseek", RAW_URL, self._fingerprint(OTHER_PATTERN)))
        self.assertEqual([KEY], [s.key for s in store.get_outcome("deepseek", RAW_URL, self._fingerprint())])
        self.assertEqual(2, self._rows("fetch_outcome"))

    def test_stale_row_under_a_different_fingerprint_is_invisible(self):
        # Given an outcome row written under some OTHER fingerprint
        store = self._open_store()
        store.put_state(RAW_URL, ETAG, 200)
        store.put_outcome("deepseek", RAW_URL, "f" * 64, [Service(key="sk-stale-deadbeef")])
        store.close()

        # When the worker revalidates under the real fingerprint
        http = _FakeHttp()
        stage = self._stage()
        output, _ = self._run(stage, http)

        # Then nothing stale is emitted; the body is fetched and extracted anew
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertNotIn("sk-stale-deadbeef", [t.service.key for t, _ in output.new_tasks])
        self.assertEqual(2, len(http.calls))


# ---------------------------------------------------------------------------
# MUST NOT: no replay across providers
# ---------------------------------------------------------------------------


class TestNoCrossProviderReplay(EtagStoreTestCase):
    def test_another_provider_does_not_replay_the_first_ones_candidates(self):
        # Given deepseek stored candidates for this URL
        first = self._stage(provider="deepseek")
        self._run(first, _FakeHttp(), self._task(provider="deepseek"))
        first.stop()

        # When kimi fetches the SAME url with the SAME patterns (304 on the wire)
        http = _FakeHttp()
        stage = self._stage(provider="kimi")
        output, outcomes = self._run(stage, http, self._task(provider="kimi"))

        # Then kimi performs its own full fetch (the outcome PK includes provider)
        self.assertEqual(2, len(http.calls))
        self.assertEqual([ETAG, ""], http.validators_sent)
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual([("kimi", [BLOB_URL])], output.links)
        self.assertIn("gather_304", outcomes)

        # And both providers now hold their own row for the same url hash
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertIsNotNone(store.get_outcome("deepseek", RAW_URL, self._fingerprint()))
        self.assertIsNotNone(store.get_outcome("kimi", RAW_URL, self._fingerprint()))
        self.assertEqual(1, self._rows("fetch_state"))
        self.assertEqual(2, self._rows("fetch_outcome"))


# ---------------------------------------------------------------------------
# MUST NOT: an empty etag never authorizes a skip
# ---------------------------------------------------------------------------


class TestEmptyEtagNeverSkips(EtagStoreTestCase):
    def test_empty_etag_row_sends_no_validator_and_fetches(self):
        # Given a state row whose etag is empty (legacy/corrupt shape)
        os.makedirs(self.store_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fetch_state ("
            "url_sha TEXT PRIMARY KEY, etag TEXT, http_status INTEGER NOT NULL DEFAULT 200,"
            " fetched_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO fetch_state(url_sha, etag, http_status, fetched_at) VALUES(?,?,?,?)",
            (FetchStore.url_sha(RAW_URL), "", 200, time.time()),
        )
        conn.commit()
        conn.close()

        # When the worker fetches
        http = _FakeHttp()
        stage = self._stage()
        output, outcomes = self._run(stage, http)

        # Then NO conditional header was sent and the body was downloaded
        self.assertEqual([""], http.validators_sent)
        self.assertEqual(1, len(http.calls))
        self.assertEqual(len(BODY.encode("utf-8")), http.body_bytes_served)
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual(["gather_ok"], outcomes)
        self.assertNotIn("gather_304", outcomes)

    def test_put_state_refuses_an_empty_etag(self):
        store = self._open_store()
        self.assertFalse(store.put_state(RAW_URL, "", 200))
        self.assertFalse(store.put_state(RAW_URL, "   ", 200))
        self.assertIsNone(store.get_state(RAW_URL))
        self.assertEqual(0, self._rows("fetch_state"))


# ---------------------------------------------------------------------------
# (v) responses.db is never touched
# ---------------------------------------------------------------------------


class TestResponseCacheUntouched(EtagStoreTestCase):
    def test_v_no_responses_db_is_created_or_read(self):
        # Given the API-only body cache is not configured for this process
        before = client._RESPONSE_CACHE

        # When two runs fetch the same URL (200 then 304 replay)
        first = self._stage()
        self._run(first, _FakeHttp())
        first.stop()
        second = self._stage()
        with mock.patch.object(client, "collect") as collect_mock:
            self._run(second, _FakeHttp())
        collect_mock.assert_not_called()
        second.stop()

        # Then no responses.db (or its WAL siblings) exists anywhere in the
        # workspace: the raw path uses fetch_store.db only
        created = []
        for root, _dirs, files in os.walk(self.workspace):
            for name in files:
                if name.startswith("responses.db"):
                    created.append(os.path.join(root, name))
        self.assertEqual([], created)
        self.assertIs(before, client._RESPONSE_CACHE)
        # The store directory holds the fetch store and nothing else
        self.assertTrue(os.path.isdir(self.store_dir))
        for name in os.listdir(self.store_dir):
            self.assertTrue(name.startswith("fetch_store.db"), name)
        self.assertFalse(os.path.isdir(os.path.join(self.workspace, "cache", "github_api")))


# ---------------------------------------------------------------------------
# (vi) store-open failure: fail-open, normal fetch, one warning
# ---------------------------------------------------------------------------


class TestStoreFailureFailsOpen(EtagStoreTestCase):
    def test_vi_unwritable_store_degrades_to_a_normal_fetch_and_logs_once(self):
        # Given <workspace>/cache/fetch_store is a FILE, so the store cannot open
        os.makedirs(os.path.dirname(self.store_dir), exist_ok=True)
        with open(self.store_dir, "w", encoding="utf-8") as blocker:
            blocker.write("not a directory")

        http = _FakeHttp()
        stage = self._stage()

        # When two fetches run on that stage
        with self.assertLogs("search", level="WARNING") as logs:
            output, outcomes = self._run(stage, http)
            output2, _ = self._run(stage, http)

        # Then both fetched normally (no exception, real candidates) ...
        assert output is not None and output2 is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual([KEY], [t.service.key for t, _ in output2.new_tasks])
        self.assertEqual(["gather_ok"], outcomes)
        # ... unconditionally (no validator can be known without a store) ...
        self.assertEqual(2, len(http.calls))
        self.assertEqual(["", ""], http.validators_sent)
        # ... and the failure was reported exactly ONCE for the run
        unavailable = [r for r in logs.records if "[fetch-store] unavailable" in r.getMessage()]
        self.assertEqual(1, len(unavailable), [r.getMessage() for r in logs.records])
        self.assertEqual(unavailable[0].levelname, "WARNING")
        # Teardown is safe with no store open
        stage.stop()

    def test_no_workspace_disables_the_store_silently(self):
        # Given a stage whose config carries no usable workspace (the shape
        # every pre-existing stage test uses: a MagicMock config)
        resources = StageResources(
            limiter=mock.MagicMock(),
            providers={},
            config=mock.MagicMock(),
            task_configs={"deepseek": TaskConfig(name="deepseek", provider_type="openai_like", stages=StageConfig())},
            auth=_Auth(),
        )
        stage = AcquisitionStage(resources, handler=_RecordingHandler())
        self.addCleanup(stage.stop)
        http = _FakeHttp()

        # When it fetches
        outcomes: List[str] = []
        with mock.patch("search.client.request", http), mock.patch(
            "stage.definition.record_gather_outcome",
            side_effect=lambda _p, o: outcomes.append(o),
        ):
            output = stage._acquisition_worker(self._task())

        # Then nothing was created anywhere and the fetch was a plain one
        assert output is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output.new_tasks])
        self.assertEqual(["gather_ok"], outcomes)
        self.assertEqual([], os.listdir(self.workspace))
        stage.stop()


# ---------------------------------------------------------------------------
# (vii) a 404 is not cached
# ---------------------------------------------------------------------------


class TestNotFoundIsNeverCached(EtagStoreTestCase):
    def test_vii_404_stores_nothing_and_a_later_fetch_still_works(self):
        # Given the CDN misses (a transient 404 on a file that exists)
        http = _FakeHttp(not_found=True)
        stage = self._stage()

        # When the worker fetches it
        output, outcomes = self._run(stage, http)

        # Then the run degrades like today's collect() does: no candidates, but
        # the link is still recorded, and NOTHING was cached
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertEqual([("deepseek", [BLOB_URL])], output.links)
        self.assertEqual(["gather_empty"], outcomes)
        store = stage._fetch_store_instance()
        assert store is not None
        self.assertIsNone(store.get_state(RAW_URL))
        self.assertEqual(0, self._rows("fetch_state"))
        self.assertEqual(0, self._rows("fetch_outcome"))

        # And when the file comes back, the very next fetch succeeds and stores
        http2 = _FakeHttp()
        output2, outcomes2 = self._run(stage, http2)
        assert output2 is not None
        self.assertEqual([KEY], [t.service.key for t, _ in output2.new_tasks])
        self.assertEqual(["gather_ok"], outcomes2)
        self.assertIsNotNone(store.get_state(RAW_URL))

    def test_404_after_a_stored_etag_does_not_replay_stale_candidates(self):
        # Given a URL that once produced candidates
        first = self._stage()
        self._run(first, _FakeHttp())
        first.stop()

        # When it later 404s (deleted upstream)
        stage = self._stage()
        output, outcomes = self._run(stage, _FakeHttp(not_found=True))

        # Then the dead file's keys are NOT re-emitted to CHECK: a 404 is a
        # fresh answer, and replaying would keep validating a removed file.
        # The stored rows stay (the file may come back), but they are only ever
        # read behind a 304.
        assert output is not None
        self.assertEqual([], output.new_tasks)
        self.assertEqual(["gather_empty"], outcomes)


# ---------------------------------------------------------------------------
# Retention: the disk guard the plan asks for
# ---------------------------------------------------------------------------


class TestRetentionPrune(EtagStoreTestCase):
    def test_prune_drops_aged_rows_and_keeps_fresh_ones(self):
        store = self._open_store()
        store.put_state(RAW_URL, ETAG, 200)
        store.put_outcome("deepseek", RAW_URL, self._fingerprint(), [Service(key=KEY)])
        aged = time.time() - 30 * 86400
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE fetch_state SET fetched_at=?", (aged,))
        conn.execute("UPDATE fetch_outcome SET fetched_at=?", (aged,))
        conn.commit()
        conn.close()

        self.assertEqual(2, store.prune(retention_days=14, max_rows=2_000_000))
        self.assertIsNone(store.get_state(RAW_URL))
        self.assertIsNone(store.get_outcome("deepseek", RAW_URL, self._fingerprint()))

    def test_prune_caps_rows_at_max(self):
        store = self._open_store()
        for i in range(5):
            store.put_state(f"https://raw.githubusercontent.com/o/r/main/f{i}.env", ETAG, 200)
        removed = store.prune(retention_days=14, max_rows=2)
        self.assertEqual(3, removed)
        self.assertEqual(2, self._rows("fetch_state"))


if __name__ == "__main__":
    unittest.main()
