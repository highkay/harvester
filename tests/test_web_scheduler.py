#!/usr/bin/env python3

"""Unit tests for web/scheduler.py — APScheduler with MemoryJobStore."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on sys.path so "web" and "tools" resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_async(coro):
    """Helper to run an async test from a sync unittest method."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1: Cron expression validation
# ---------------------------------------------------------------------------


class TestCronValidation(unittest.TestCase):
    """Given cron expressions,
    When validated via CronTrigger.from_crontab,
    Then valid expressions pass and invalid ones raise ValueError.
    """

    def test_valid_cron_passes(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        # Must not raise
        trigger = CronTrigger.from_crontab("0 3 * * *")
        self.assertIsNotNone(trigger)

    def test_garbage_string_raises_valueerror(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        with self.assertRaises(ValueError):
            CronTrigger.from_crontab("not-a-cron")


# ---------------------------------------------------------------------------
# Test 2: Seed data on empty database
# ---------------------------------------------------------------------------


class TestSeedData(unittest.TestCase):
    """Given an empty schedule_config table,
    When init_scheduler is called,
    Then 27 default provider schedules are inserted.
    """

    _EXPECTED_PROVIDERS = frozenset(
        {
            "deepseek",
            "kimi",
            "kimi-ai",
            "kimi-coding",
            "mimo-cn",
            "mimo-sg",
            "qwen-cn",
            "qwen-intl",
            "glm",
            "glm-ai",
            "brave",
            "exa",
            "modelscope",
            "tavily",
            "github",
            "serpapi",
            "agnes-ai",
            # 2026-09-22: seeded providers that prod only had via manual/UI
            # inserts (the seed list ran against an EMPTY table on prod long
            # before these existed, so fresh installs never scheduled them).
            # 2026-09-24: groq removed — GitHub secret-scanning partner +
            # push protection auto-revokes leaked gsk_ keys within minutes,
            # so 22 completed prod runs yielded 0 valid keys (structural).
            "ollama",
            "openrouter",
            "nvidia",
            # 2026-09-28: opencode seeded — endpoint live-verified the same
            # day (public /models list, auth-gated chat probe).
            "opencode",
            # 2026-10-29: commandcode seeded — /provider/v1/models is a public
            # trap (same 18,461-byte 200 for no-key/bad-key); the judge is a
            # minimal chat probe (401 UNAUTHORIZED = invalid).
            "commandcode",
            # 2026-10-29: clinepass seeded — /api/v1/models is a public
            # trap (same 41,064-byte 200 for no-key/bad-key on both exits);
            # the judge is a minimal chat probe (401 = invalid).
            "clinepass",
            # 2026-10-29 wave-1: siliconflow seeded — /v1/models is a clean
            # auth gate (401 for no-key and bad-key, measured wave-0); the
            # free tier is confirmed by the docs' server-rendered text.
            "siliconflow",
            # 2026-10-29 wave-1: gemini seeded — the /v1beta/models list gate
            # is clean (403 no-key, 400 API_KEY_INVALID for every bad key on
            # both exits, query and header forms; never 200).
            "gemini",
            # 2026-10-29 wave-1: doubao seeded - ark.cn-beijing.volces.com
            # /api/v3 is a clean 401 auth gate; keys are UUID v4 (measured
            # 2026-10-29); volcengine_ark_api_key is GitHub push-protected,
            # so supply is suppressed and the cadence is the weekly floor.
            "doubao",
            # 2026-10-29 wave-1: mistral seeded - /v1/models answers 401
            # {"detail":"Invalid API Key"} for no-key and bad-key from both
            # prod socks exits (a real application gate, not a trap).
            "mistral",
        }
    )
    _EXPECTED_CRONS = {
        # 2026-10-24 audit: these four were seeded every 4 hours on a
        # fast-churn theory; measured weekly-era yield is 0-2 net-new keys per run
        # (70 d: deepseek 17, kimi 4, mimo-cn 2) against 2-13 h runs each, and
        # daily→weekly had already cost ~20 keys/fortnight. Weekly floor per the
        # 2026-10-18 house rule; depth is bounded per config instead.
        "deepseek": "0 2 * * sun",
        "kimi": "0 4 * * mon",
        "mimo-cn": "0 6 * * tue",
        "qwen-cn": "0 7 * * wed",
        "glm": "0 14 * * mon,wed,fri",
        # 2026-10-09 frequency policy: saturated providers → weekly floor or
        # 2-3x/week (measured 0-2 net-new/day, 98-100% cross-run overlap).
        # Day-of-week entries are NAME-based on purpose: APScheduler's
        # from_crontab reads numeric dow as 0=mon…6=sun (NOT standard crontab's
        # 0=sun), which silently shifted the first numeric version of this
        # policy by one weekday (and made opencode's old "0 22 * * 6" fire on
        # Sunday while docs said Saturday).
        "modelscope": "0 3 * * wed",
        "tavily": "40 */6 * * *",
        "github": "50 */6 * * *",
        "serpapi": "0 6 * * mon,wed,fri",
        "agnes-ai": "30 17 * * fri",
        "glm-ai": "0 16 * * wed",
        # Brave Search: daily 15:00 — NO day-of-week field on purpose (the
        # seed stays daily; a supply probe may only adjust the prod row).
        "brave": "0 20 * * *",
        # Exa Search: daily 16:00 — one hour behind brave's 15:00 firing;
        # NO day-of-week field on purpose (same daily-seed policy).
        "exa": "0 22 * * *",
        # 2026-09-24: moved off the 14:00-17:00 Beijing window — measured prod
        # token-cooldown storm concentrated at 10:00-16:00 Beijing (4529
        # warnings on 2026-09-24), which starved these four providers' search
        # stages (qwen-intl completed with 0 links). Evening starts land after
        # the morning chain's search phases have drained the pool.
        # 2026-10-24 de-collision (measured on prod): mimo-sg 20:00 sat fired
        # inside brave 20:00 daily (41 min shared search), kimi-coding 19:00 fri
        # cleared brave by 5 min, qwen-intl 21:00 sun cleared exa 22:00 by 14 min.
        "kimi-ai": "0 18 * * thu",
        "kimi-coding": "0 16 * * fri",
        "mimo-sg": "0 15 * * sat",
        "qwen-intl": "0 16 * * sun",
        "ollama": "0 3 * * mon",
        "openrouter": "30 7 * * thu",
        "nvidia": "50 11 * * *",
        "opencode": "0 18 * * sat",
        # 2026-10-29 wave-1: commandcode weekly floor; Monday 05:00 sits
        # between kimi 04:00 (capped ~45 min) and serpapi 06:00.
        "commandcode": "0 5 * * mon",
        # 2026-10-29 wave-1: clinepass weekly floor; Saturday 23:00 sits
        # after mimo-sg 15:00 and opencode 18:00.
        "clinepass": "0 23 * * sat",
        # 2026-10-29 wave-1: siliconflow weekly floor; Saturday 05:00 sits
        # before mimo-sg 15:00 and opencode 18:00.
        "siliconflow": "0 5 * * sat",
        # 2026-10-29 wave-1: gemini weekly floor; Friday 05:00 sits before
        # serpapi's 06:00 window and after the Thursday chain.
        "gemini": "0 5 * * fri",
        # 2026-10-29 wave-1: doubao weekly floor; Tuesday 05:00 sits before
        # mimo-cn 06:00 tue. Push protection suppresses fresh supply.
        "doubao": "0 5 * * tue",
        # 2026-10-29 wave-1: mistral weekly floor; Sunday 05:00 follows
        # deepseek 02:00 (capped) with no same-window overlap.
        "mistral": "0 5 * * sun",
    }
    _EXPECTED_CONFIG_FILES = {
        "deepseek": "examples/config-deepseek.yaml",
        "kimi": "examples/config-kimi.yaml",
        "mimo-cn": "examples/config-mimo.yaml",
        "qwen-cn": "examples/config-qwen.yaml",
        "glm": "examples/config-glm.yaml",
        "modelscope": "examples/config-modelscope.yaml",
        "tavily": "examples/config-tavily.yaml",
        "github": "examples/config-github.yaml",
        "serpapi": "examples/config-serpapi.yaml",
        "agnes-ai": "examples/config-agnes-ai.yaml",
        "glm-ai": "examples/config-glm-ai.yaml",
        "brave": "examples/config-brave.yaml",
        "exa": "examples/config-exa.yaml",
        "kimi-ai": "examples/config-kimi-ai.yaml",
        "kimi-coding": "examples/config-kimi-coding.yaml",
        "mimo-sg": "examples/config-mimo-sg.yaml",
        "qwen-intl": "examples/config-qwen-intl.yaml",
        "ollama": "examples/config-ollama.yaml",
        "openrouter": "examples/config-openrouter.yaml",
        "nvidia": "examples/config-nvidia.yaml",
        "opencode": "examples/config-opencode.yaml",
        "commandcode": "examples/config-commandcode.yaml",
        "clinepass": "examples/config-clinepass.yaml",
        "siliconflow": "examples/config-siliconflow.yaml",
        "gemini": "examples/config-gemini.yaml",
        "doubao": "examples/config-doubao.yaml",
        "mistral": "examples/config-mistral.yaml",
    }

    def test_seeds_defaults_on_empty_table(self) -> None:
        from web.db import init_db, get_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"

                # -- Given: an empty database with the schedule_config table --
                await init_db(db_path)

                # Verify truly empty
                db = await get_db(db_path)
                cursor = await db.execute("SELECT COUNT(*) FROM schedule_config")
                row = await cursor.fetchone()
                self.assertEqual(row[0], 0, "schedule_config should be empty before seed")
                await db.close()

                # -- When: init_scheduler runs against this DB --
                from web.scheduler import init_scheduler

                settings = _make_settings(db_path)
                svc = await init_scheduler(settings)

                # -- Then: 27 default rows inserted --
                db2 = await get_db(db_path)
                cursor2 = await db2.execute(
                    "SELECT provider_name, cron_expression, enabled, config_file "
                    "FROM schedule_config ORDER BY provider_name"
                )
                rows = await cursor2.fetchall()
                await db2.close()

                self.assertEqual(
                    len(rows),
                    len(self._EXPECTED_PROVIDERS),
                    f"Expected {len(self._EXPECTED_PROVIDERS)} rows, got {len(rows)}",
                )

                providers = {r[0] for r in rows}
                self.assertEqual(
                    providers,
                    self._EXPECTED_PROVIDERS,
                    f"Provider set mismatch: {providers ^ self._EXPECTED_PROVIDERS}",
                )

                for r in rows:
                    self.assertEqual(
                        r[1],
                        self._EXPECTED_CRONS[r[0]],
                        f"Provider {r[0]} cron mismatch",
                    )
                    self.assertEqual(r[2], 1, f"Provider {r[0]} should be enabled")
                    self.assertEqual(
                        r[3],
                        self._EXPECTED_CONFIG_FILES[r[0]],
                        f"Provider {r[0]} config_file mismatch",
                    )

                # -- Clean up --
                if svc is not None:
                    await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 2b: The seed constant itself (count, uniqueness, exact new tuples)
# ---------------------------------------------------------------------------


class TestSeedListShape(unittest.TestCase):
    """Given the _DEFAULT_SCHEDULES seed constant,
    When inspected directly,
    Then providers are unique, every cron parses, every config file exists on
    disk, and the 2026-09-22 additions carry the exact tuples pinned here.
    """

    # (provider, cron, config_file) — the 2026-09-22 seed additions, plus the
    # github self-bootstrap entry whose cron AGENTS.md documents (already
    # seeded before the change; pinned so it cannot drift silently).
    # 2026-10-09: ollama/openrouter moved to 2x/week (saturated pools, the
    # weekly-floor policy for cold providers); weekdays NAME-based — see the
    # APScheduler numeric-dow footgun note in _EXPECTED_CRONS.
    _PINNED_ENTRIES = {
        "ollama": ("0 3 * * mon", "examples/config-ollama.yaml"),
        "openrouter": ("30 7 * * thu", "examples/config-openrouter.yaml"),
        "nvidia": ("50 11 * * *", "examples/config-nvidia.yaml"),
        "github": ("50 */6 * * *", "examples/config-github.yaml"),
    }

    def test_groq_is_not_seeded(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        providers = {entry[0] for entry in _DEFAULT_SCHEDULES}
        self.assertNotIn(
            "groq",
            providers,
            "groq must not be seeded — GitHub partner auto-revocation makes "
            "its scans structurally zero-yield (see AGENTS.md groq section)",
        )

    def test_no_duplicate_provider_names(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        providers = [entry[0] for entry in _DEFAULT_SCHEDULES]
        duplicates = {p for p in providers if providers.count(p) > 1}
        self.assertEqual(
            duplicates, set(), f"duplicate providers in seed list: {duplicates}"
        )

    def test_pinned_tuples_exact(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        entries = {p: (cron, cfg) for p, cron, cfg in _DEFAULT_SCHEDULES}
        for provider, expected in self._PINNED_ENTRIES.items():
            self.assertIn(provider, entries, f"{provider} missing from seed list")
            self.assertEqual(
                entries[provider], expected, f"{provider} seed tuple drifted"
            )

    def test_dow_crons_fire_on_intended_weekdays(self) -> None:
        """APScheduler's from_crontab reads NUMERIC day-of-week with its own
        native numbering (0=mon … 5=sat, 6=sun), NOT standard crontab (0=sun).
        On 2026-10-09 this silently shifted the whole weekly-floor policy by
        one weekday ("1,3,5" fired tue/thu/sat), and opencode's old
        "0 22 * * 6" had been firing on SUNDAY while every doc said Saturday.
        Seed crons must therefore spell weekdays BY NAME, and this test pins
        the semantics end-to-end: every seeded dow fires only on intended days.
        """
        from datetime import datetime, timedelta, timezone

        from apscheduler.triggers.cron import CronTrigger

        from web.scheduler import _DEFAULT_SCHEDULES

        # A Friday-noon vantage: a misread numeric dow surfaces within the
        # next few fire times (weekend firings the intent never asked for).
        # tz-aware: APScheduler converts to the trigger's timezone, and a
        # noon-UTC vantage stays a Friday in every plausible local zone.
        vantage = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)  # a Friday
        name_to_weekday = {
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        }
        checked = 0
        for provider, cron, _cfg in _DEFAULT_SCHEDULES:
            dow = cron.split()[4]
            if dow == "*":
                continue
            checked += 1
            self.assertFalse(
                any(ch.isdigit() for ch in dow),
                f"{provider}: numeric day-of-week {dow!r} is ambiguous under "
                "APScheduler from_crontab (0=mon…6=sun, NOT crontab's 0=sun) "
                "— spell weekdays by name (mon,tue,…)",
            )
            expected = {name_to_weekday[d.strip()] for d in dow.split(",")}
            trigger = CronTrigger.from_crontab(cron)
            seen: set[int] = set()
            nxt = vantage
            for _ in range(10):
                fire = trigger.get_next_fire_time(None, nxt)
                self.assertIsNotNone(fire, f"{provider}: no next fire from {nxt}")
                assert fire is not None
                seen.add(fire.weekday())
                nxt = fire + timedelta(seconds=60)
            self.assertEqual(
                seen,
                expected,
                f"{provider}: cron {cron!r} fires on python-weekdays "
                f"{sorted(seen)}, expected {sorted(expected)}",
            )
        self.assertGreaterEqual(
            checked, 6, "expected at least the 6 weekday-carrying seed crons"
        )

    def test_every_seed_cron_parses(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        from web.scheduler import _DEFAULT_SCHEDULES

        for provider, cron, _cfg in _DEFAULT_SCHEDULES:
            try:
                CronTrigger.from_crontab(cron)  # raises ValueError on garbage
            except ValueError:
                self.fail(f"{provider}: invalid cron expression {cron!r}")

    def test_every_seed_config_file_exists(self) -> None:
        from pathlib import Path

        from web.scheduler import _DEFAULT_SCHEDULES

        repo_root = Path(__file__).resolve().parents[1]
        for provider, _cron, cfg in _DEFAULT_SCHEDULES:
            self.assertTrue(
                (repo_root / cfg).is_file(),
                f"{provider}: seed config {cfg} does not exist",
            )


# ---------------------------------------------------------------------------
# Test 3: Re-entrancy guard on trigger_manual
# ---------------------------------------------------------------------------


class TestReentrancyGuard(unittest.TestCase):
    """Given a SchedulerService with a _running set,
    When trigger_manual is called for an already-running provider,
    Then HTTPException(409) is raised.
    When trigger_manual is called for a non-running provider,
    Then "triggered" is returned.
    """

    def test_raises_409_when_already_running(self) -> None:
        from fastapi import HTTPException

        async def _scenario() -> None:
            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(
                scheduler=mock_scheduler,
                db_path=":memory:",
            )
            svc._running.add("deepseek")

            with self.assertRaises(HTTPException) as ctx:
                await svc.trigger_manual("deepseek")

            self.assertEqual(ctx.exception.status_code, 409)

        _run_async(_scenario())

    def test_returns_triggered_when_not_running(self) -> None:
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )

                # trigger_manual now AWAITS the scan start (via start_scan);
                # the fake runner stands in for web.runner.PipelineRunner and
                # the watcher task it arms is cancelled by shutdown().
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")

                await svc.shutdown()

        _run_async(_scenario())

    def test_is_running_returns_correct_bool(self) -> None:
        from unittest.mock import MagicMock

        from web.scheduler import SchedulerService

        mock_scheduler = MagicMock()
        svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

        self.assertFalse(svc.is_running("deepseek"))
        svc._running.add("deepseek")
        self.assertTrue(svc.is_running("deepseek"))


# ---------------------------------------------------------------------------
# Test 3b: Guard held for the scan's FULL lifetime (2026-09-22 overlap bug)
# ---------------------------------------------------------------------------


class TestGuardHeldForScanLifetime(unittest.TestCase):
    """Regression contract for the anti-overlap guard:

    ``PipelineRunner.run_scan`` returns as soon as the scan *thread* starts.
    The old ``_run_provider_job`` released ``_running`` in its ``finally``,
    so during a live scan ``is_running()`` claimed idle, ``trigger_manual``
    answered 202 "triggered", and the runner's own 409 was swallowed by a
    broad except — the UI reported success for a run that never happened.

    Fixed contract (pinned below): the scheduler guard lives while the
    run_records row reads 'running' (watcher task polls ``get_run``),
    start failures release the guard and propagate to the caller.
    """

    def test_guard_held_while_run_is_live_and_second_trigger_409s(self) -> None:
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()  # status stays "running" until flipped

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")

                    # Regression: the guard SURVIVES run_scan returning ...
                    self.assertTrue(svc.is_running("deepseek"))
                    # ... and stays held across watcher polls of a live run.
                    for _ in range(5):
                        await asyncio.sleep(0.02)
                    self.assertTrue(svc.is_running("deepseek"))

                    # A second manual trigger while live must fail honestly.
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 409)
                    self.assertEqual(
                        len(fake.run_scan_calls), 1, "no second scan may start"
                    )

                await svc.shutdown()

        _run_async(_scenario())

    def test_guard_released_when_run_reaches_terminal_status(self) -> None:
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))

                    # -- When: the run_records row turns terminal --
                    fake.status = "completed"
                    for _ in range(200):
                        await asyncio.sleep(0.01)
                        if not svc.is_running("deepseek"):
                            break

                    # -- Then: guard released, provider triggerable again --
                    self.assertFalse(svc.is_running("deepseek"))
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")
                    self.assertEqual(len(fake.run_scan_calls), 2)

                await svc.shutdown()

        _run_async(_scenario())

    def test_trigger_manual_propagates_runner_409(self) -> None:
        """The runner's own guard (scan thread alive but watcher released, or
        a runner row from a pre-fix process) must surface as 409 — never a
        false "triggered"."""
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.run_scan_error = HTTPException(
                status_code=409, detail="Provider 'deepseek' is already running"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 409)
                    # A failed start must not leak the scheduler guard.
                    self.assertFalse(svc.is_running("deepseek"))

                await svc.shutdown()

        _run_async(_scenario())

    def test_trigger_manual_maps_missing_config_template_to_404(self) -> None:
        """run_scan raises ValueError when the source YAML is missing — the
        route must see a 404, not a silent success or a bare 500."""
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.run_scan_error = ValueError(
                "No example config for provider 'deepseek': expected examples/none.yaml"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/none.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 404)
                    self.assertIn("No example config", str(ctx.exception.detail))
                    self.assertFalse(svc.is_running("deepseek"))

                await svc.shutdown()

        _run_async(_scenario())

    def test_run_provider_job_holds_guard_for_scheduled_scan(self) -> None:
        """The cron path must hold the guard too — the next firing during a
        live scan skips instead of double-starting."""
        async def _scenario() -> None:
            import web.scheduler
            from web.scheduler import _run_provider_job

            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                web.scheduler._scheduler_service = svc
                try:
                    with patch(
                        "web.scheduler._lazy_get_runner", return_value=fake
                    ), patch("web.scheduler._WATCH_POLL_SECONDS", 0.01):
                        await _run_provider_job("deepseek", "examples/config-deepseek.yaml")

                        self.assertEqual(
                            fake.run_scan_calls,
                            [("deepseek", "examples/config-deepseek.yaml")],
                        )
                        self.assertTrue(svc.is_running("deepseek"))

                        # A concurrent firing while live must skip (no 2nd scan).
                        await _run_provider_job("deepseek", "examples/config-deepseek.yaml")
                        self.assertEqual(len(fake.run_scan_calls), 1)

                        fake.status = "failed"
                        for _ in range(200):
                            await asyncio.sleep(0.01)
                            if not svc.is_running("deepseek"):
                                break
                        self.assertFalse(svc.is_running("deepseek"))
                finally:
                    web.scheduler._scheduler_service = None
                await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 3c: Watch-deadline — a stuck 'running' row must not wedge the guard
# ---------------------------------------------------------------------------


async def _released_within(svc, provider: str, attempts: int, delay: float = 0.01):
    """Poll until the guard for *provider* is released; False on timeout.

    Wall-clock bounded, not attempt bounded: on some platforms (Windows /
    Python 3.12) a tiny ``asyncio.sleep(delay)`` returns almost immediately
    when a concurrent task is also sleeping, so a pure attempt count can
    exhaust the budget before the watcher's (millisecond-scale) deadline has
    had any real time to elapse. Enforce the intended wall-clock budget so the
    watcher always gets real time to reach its deadline.
    """
    budget = max(attempts * delay, 2.0)
    start = time.monotonic()
    while time.monotonic() - start < budget:
        await asyncio.sleep(delay)
        if not svc.is_running(provider):
            return True
    return False


class TestWatchRunDeadline(unittest.TestCase):
    """F7 leak contract: ``_watch_run`` polled with NO timeout, so a row
    stuck in 'running' (terminal DB write lost — sqlite locked / disk full)
    held the scheduler guard until process restart, blocking every future
    cron for the provider and inflating ``_active_run_count``.

    Fixed contract (pinned below): the watch expires at the row's deadline —
    ``started_at`` + ``_RUN_WATCH_MAX_HOURS`` (env
    HARVESTER_RUN_WATCH_MAX_HOURS, default 36; missing/unparseable
    ``started_at`` falls back to watcher start) — releasing the guard,
    logging ERROR (shortened run id), and attempting a best-effort
    ``running→failed`` flip through the runner's update path with
    ``only_if_running`` semantics.
    """

    def test_stuck_row_releases_guard_at_deadline_and_flips_failed(self) -> None:
        """Given a row that NEVER leaves 'running' (no started_at → watcher-start
        fallback) and a tiny patched deadline,
        When the watcher passes the deadline,
        Then the guard is released, an ERROR is logged with the short run id,
        and one terminal flip is attempted: status 'failed' + only_if_running."""
        async def _scenario() -> None:
            fake = _FakeRunner()  # status stays "running" forever

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                mock_logger = MagicMock()
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ), patch(  # 1e-5 h = 36 ms — a few polls past watcher start
                    "web.scheduler.logger", mock_logger
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))

                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(
                        released,
                        "watcher must release the guard at the deadline, not "
                        "loop forever on a stuck 'running' row",
                    )

                    # ERROR logged, masked to the short run id
                    self.assertTrue(mock_logger.error.called, "expiry must log ERROR")
                    logged = " ".join(
                        str(c.args[0])
                        for c in mock_logger.error.call_args_list
                        if c.args
                    )
                    self.assertIn(fake.run_id[:8], logged)

                    # Best-effort terminal flip: running → failed, conditional
                    self.assertEqual(len(fake.update_run_calls), 1)
                    args, kwargs = fake.update_run_calls[0]
                    self.assertEqual(args[:2], (fake.run_id, "failed"))
                    self.assertIs(kwargs.get("only_if_running"), True)

                await svc.shutdown()

        _run_async(_scenario())

    def test_row_started_long_ago_expires_on_first_poll(self) -> None:
        """Given the DEFAULT deadline (36 h) and a row whose started_at is
        100 h old, When the watcher polls it, Then it expires on the first
        poll (guard released within a few poll intervals) and flips failed."""
        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.started_at = (
                datetime.now(timezone.utc) - timedelta(hours=100)
            ).strftime("%Y-%m-%d %H:%M:%S")  # the datetime('now') UTC shape

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))

                    # No deadline patch: the module default applies, and a
                    # 100 h-old started_at is already past it → the FIRST poll
                    # (≈10 ms) must release. A 20-poll (0.2 s) budget pins
                    # "immediately", not "eventually".
                    released = await _released_within(svc, "deepseek", attempts=20)
                    self.assertTrue(
                        released,
                        "a started_at older than the deadline must expire on "
                        "the first poll",
                    )
                    self.assertEqual(len(fake.update_run_calls), 1)
                    args, kwargs = fake.update_run_calls[0]
                    self.assertEqual(args[:2], (fake.run_id, "failed"))
                    self.assertIs(kwargs.get("only_if_running"), True)

                await svc.shutdown()

        _run_async(_scenario())

    def test_unparseable_started_at_falls_back_to_watch_start(self) -> None:
        """Given garbage started_at and a tiny patched deadline,
        When the watcher polls, Then it neither crashes nor loops forever —
        the deadline falls back to watcher start and expires normally."""
        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.started_at = "not-a-timestamp"

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ):
                    await svc.trigger_manual("deepseek")
                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(
                        released,
                        "unparseable started_at must fall back to the watcher "
                        "start, not wedge the guard",
                    )
                    self.assertEqual(len(fake.update_run_calls), 1)

                await svc.shutdown()

        _run_async(_scenario())

    def test_terminal_before_deadline_releases_quietly(self) -> None:
        """Regression: the normal path is untouched — a row that goes
        terminal BEFORE the deadline releases the guard with NO ERROR log
        and NO flip attempt (the row already carries its own terminal
        status)."""
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                mock_logger = MagicMock()
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 24.0, create=True
                ), patch(  # huge vs the test's millisecond runtime
                    "web.scheduler.logger", mock_logger
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))

                    await asyncio.sleep(0.05)  # let it poll a few times
                    fake.status = "completed"  # -- When: terminal pre-deadline
                    released = await _released_within(svc, "deepseek", attempts=100)
                    self.assertTrue(released)

                    self.assertFalse(
                        mock_logger.error.called,
                        "a pre-deadline terminal row must not log ERROR",
                    )
                    self.assertEqual(
                        fake.update_run_calls, [],
                        "a pre-deadline terminal row must not be flipped",
                    )

                await svc.shutdown()

        _run_async(_scenario())

    def test_deadline_flip_records_validated_key_count(self) -> None:
        """Given a stuck row and a runner whose failed-run count helper
        reports 17 validated keys, When the watcher passes the deadline,
        Then the ``running→failed`` flip carries ``valid_keys_found=17`` —
        startup push recovery (``find_unpushed_terminal_runs`` gates on
        ``COALESCE(valid_keys_found,0) > 0``) would otherwise never see the
        stranded run's validated keys."""
        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.failed_run_valid_keys = 17

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ):
                    await svc.trigger_manual("deepseek")
                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(released)

                    self.assertEqual(len(fake.update_run_calls), 1)
                    args, kwargs = fake.update_run_calls[0]
                    self.assertEqual(args[:2], (fake.run_id, "failed"))
                    self.assertEqual(
                        kwargs.get("valid_keys_found"),
                        17,
                        "the deadline flip must record the provider-directory "
                        "valid-key count so push recovery can see the run",
                    )
                    self.assertEqual(
                        fake.count_valid_keys_calls,
                        [("deepseek", None)],
                        "the count must be scoped to THIS provider (no temp "
                        "config in the deadline path)",
                    )

                await svc.shutdown()

        _run_async(_scenario())

    def test_deadline_flip_proceeds_when_count_helper_raises(self) -> None:
        """Given the failed-run count helper RAISES, When the watcher passes
        the deadline, Then the flip still proceeds (best-effort) with
        ``valid_keys_found`` omitted — matching the runner's own failure path,
        which never lets the counter block a terminal write."""
        async def _scenario() -> None:
            fake = _FakeRunner()

            def _raise(provider_name: str, temp_yaml_path: object = None) -> int:
                raise RuntimeError("count unavailable")

            fake._count_valid_keys_for_failed_run = _raise  # type: ignore[method-assign]

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ):
                    await svc.trigger_manual("deepseek")
                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(
                        released,
                        "a failing count helper must not wedge the guard",
                    )

                    self.assertEqual(len(fake.update_run_calls), 1)
                    args, kwargs = fake.update_run_calls[0]
                    self.assertEqual(args[:2], (fake.run_id, "failed"))
                    self.assertIsNone(
                        kwargs.get("valid_keys_found"),
                        "an unavailable count must be omitted (None), never "
                        "block the terminal flip",
                    )

                await svc.shutdown()

        _run_async(_scenario())

    def test_deadline_flip_proceeds_when_count_helper_returns_none(self) -> None:
        """Given the failed-run count helper returns None, When the watcher
        passes the deadline, Then the flip still proceeds with
        ``valid_keys_found`` None — ``_update_run_sync`` skips a None column,
        keeping the write compatible with rows lacking the counter."""
        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.failed_run_valid_keys = None

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ):
                    await svc.trigger_manual("deepseek")
                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(released)

                    self.assertEqual(len(fake.update_run_calls), 1)
                    args, kwargs = fake.update_run_calls[0]
                    self.assertEqual(args[:2], (fake.run_id, "failed"))
                    self.assertIsNone(kwargs.get("valid_keys_found"))

                await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 4: next_run_time exists after add_job
# ---------------------------------------------------------------------------


class TestNextRunTime(unittest.TestCase):
    """Given a real AsyncIOScheduler,
    When a job is added via update_schedule,
    Then get_job returns a non-None job for that provider.
    """

    def test_get_job_returns_non_none_after_add(self) -> None:
        async def _scenario() -> None:
            import sqlite3

            from apscheduler.executors.asyncio import AsyncIOExecutor
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from web.scheduler import SchedulerService

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"

                # Manually create the schedule_config table
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schedule_config ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "provider_name TEXT NOT NULL UNIQUE, "
                    "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
                    "enabled INTEGER NOT NULL DEFAULT 1, "
                    "config_file TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                    "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
                    ")"
                )
                conn.execute(
                    "INSERT INTO schedule_config "
                    "(provider_name, cron_expression, enabled, config_file) "
                    "VALUES (?, ?, ?, ?)",
                    ("test-prov", "0 3 * * *", 1, "examples/config-test.yaml"),
                )
                conn.commit()
                conn.close()

                # Match production init_scheduler config: async job callback
                # (_run_provider_job) must run on AsyncIOExecutor, otherwise it
                # is never awaited and the job silently does nothing.
                scheduler = AsyncIOScheduler(executors={"default": AsyncIOExecutor()})
                scheduler.start()
                svc = SchedulerService(scheduler=scheduler, db_path=db_path)

                await svc.update_schedule(
                    provider_name="test-prov",
                    cron_expression="*/5 * * * *",
                    enabled=True,
                    config_file="examples/config-test.yaml",
                )

                job = scheduler.get_job("scan-test-prov")
                self.assertIsNotNone(job, "Job should exist after update_schedule")

                scheduler.shutdown(wait=False)

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 5: Mock PipelineRunner — job callback invokes runner
# ---------------------------------------------------------------------------


class TestJobCallbackInvokesRunner(unittest.TestCase):
    """Given a mocked PipelineRunner,
    When the job callback fires for a provider,
    Then get_runner().run_scan() is called with provider_name.
    """

    def test_job_callback_calls_runner(self) -> None:
        async def _scenario() -> None:
            mock_runner = MagicMock()
            mock_runner.run_scan = AsyncMock()

            # Set up a scheduler service so _run_provider_job doesn't bail early
            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner", return_value=mock_runner
                ) as mock_get_runner:
                    from web.scheduler import _run_provider_job

                    await _run_provider_job("deepseek")

                    mock_get_runner.assert_called_once()
                    mock_runner.run_scan.assert_called_once_with("deepseek", None)
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())

    def test_job_callback_passes_config_file_to_runner(self) -> None:
        """Given a mocked runner,
        When _run_provider_job fires with an explicit config_file,
        Then run_scan is called with (provider_name, config_file).
        """
        async def _scenario() -> None:
            mock_runner = MagicMock()
            mock_runner.run_scan = AsyncMock()

            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner", return_value=mock_runner
                ):
                    from web.scheduler import _run_provider_job

                    await _run_provider_job("mimo-cn", "examples/config-mimo.yaml")

                    mock_runner.run_scan.assert_called_once_with(
                        "mimo-cn", "examples/config-mimo.yaml"
                    )
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())

    def test_job_callback_handles_missing_runner_module(self) -> None:
        async def _scenario() -> None:
            from web.scheduler import SchedulerService
            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner",
                    side_effect=ImportError("No module named 'web.runner'"),
                ):
                    from web.scheduler import _run_provider_job

                    # Must not raise
                    await _run_provider_job("deepseek")
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 6: config_file threading through scheduler jobs
# ---------------------------------------------------------------------------


class TestConfigFileThreading(unittest.TestCase):
    """Given schedule rows carrying config_file,
    When update_schedule / trigger_manual run,
    Then config_file is threaded into the job args and the job call.
    """

    _SCHEDULE_TABLE_DDL = (
        "CREATE TABLE IF NOT EXISTS schedule_config ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "provider_name TEXT NOT NULL UNIQUE, "
        "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "config_file TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )

    def test_update_schedule_passes_config_file_in_job_args(self) -> None:
        """update_schedule must add the job with args=(provider, config_file)."""
        async def _scenario() -> None:
            from web.scheduler import SchedulerService

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"
                conn = sqlite3.connect(db_path)
                conn.execute(self._SCHEDULE_TABLE_DDL)
                conn.commit()
                conn.close()

                mock_scheduler = MagicMock()
                mock_scheduler.get_job.return_value = None
                svc = SchedulerService(scheduler=mock_scheduler, db_path=db_path)

                await svc.update_schedule(
                    provider_name="mimo-cn",
                    cron_expression="0 3 * * *",
                    enabled=True,
                    config_file="examples/config-mimo.yaml",
                )

                mock_scheduler.add_job.assert_called_once()
                self.assertEqual(
                    mock_scheduler.add_job.call_args.kwargs["args"],
                    ("mimo-cn", "examples/config-mimo.yaml"),
                )

        _run_async(_scenario())

    def test_trigger_manual_passes_config_file_to_job(self) -> None:
        """trigger_manual must read config_file from schedule_config and pass
        it to the runner's run_scan (via start_scan)."""
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )

                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")
                    self.assertEqual(
                        fake.run_scan_calls,
                        [("deepseek", "examples/config-deepseek.yaml")],
                    )

                await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 7: Missed-run catch-up at startup (2026-09-28 deferral-loss class)
# ---------------------------------------------------------------------------


class TestMissedRunCatchup(unittest.TestCase):
    """Given the scheduler restarting after cron slots passed while down,
    When _schedule_catchups runs over the enabled schedule_config rows,
    Then each provider whose LATEST fire has no run_records row started at or
    after it (in UTC — started_at is UTC, cron fires are scheduler-tz) and no
    live 'running' row gets ONE staggered one-shot catch-up job through the
    _run_provider_job path; disabled providers and providers with a run since
    the fire are skipped; the whole feature is env-gated.
    """

    @staticmethod
    async def _make_db(
        db_path: str,
        schedules: list[tuple[str, str, int, str]],
        run_inserts: list[tuple[str, str, str, str]] = (),
    ) -> str:
        """Full-schema DB with the given schedule rows + run_records rows.

        ``run_inserts`` entries are (id, provider, status, started_at_sql)
        where started_at_sql is a test-controlled SQLite expression.
        """
        from web.db import init_db

        await init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            for provider, cron, enabled, cfg in schedules:
                conn.execute(
                    "INSERT INTO schedule_config "
                    "(provider_name, cron_expression, enabled, config_file) "
                    "VALUES (?, ?, ?, ?)",
                    (provider, cron, int(enabled), cfg),
                )
            for run_id, provider, status, started_sql in run_inserts:
                conn.execute(
                    "INSERT INTO run_records "
                    "(id, provider_name, config_file, status, started_at) "
                    f"VALUES (?, ?, 'c.yaml', ?, {started_sql})",
                    (run_id, provider, status),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_schedules_catchup_for_missed_enabled_provider(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        ("deepseek", "0 3 * * *", 1, "examples/config-deepseek.yaml")
                    ],
                )
                sched = MagicMock()

                with self.assertLogs("web.scheduler", level="INFO") as logs:
                    count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 1)
                sched.add_job.assert_called_once()
                kwargs = sched.add_job.call_args.kwargs
                self.assertTrue(str(kwargs["id"]).startswith("catchup-deepseek-"))
                self.assertEqual(kwargs["args"][0], "deepseek")
                self.assertEqual(kwargs["args"][1], "examples/config-deepseek.yaml")
                # ISO UTC missed-fire reason travels to the WARNING callback.
                self.assertIn("T", kwargs["args"][2])
                self.assertEqual(kwargs["max_instances"], 1)
                self.assertIsNone(kwargs["misfire_grace_time"])
                # One-shot DateTrigger at now + one stagger slot (the constant,
                # not a literal: the 2026-10-24 wave raised it 60 s -> 900 s).
                import web.scheduler as scheduler_mod

                stagger = scheduler_mod._CATCHUP_STAGGER_SECONDS
                # Pin the POLICY value, not just the derivation: both tests below
                # read the constant, so without this a regression to 60 s would
                # still pass while the measured restart burst returned.
                self.assertEqual(900, stagger, "catch-up stagger policy (2026-10-24 wave)")
                run_date = kwargs["trigger"].run_date
                expected = datetime.now(run_date.tzinfo) + timedelta(seconds=stagger)
                self.assertLess(abs((run_date - expected).total_seconds()), 5)
                self.assertTrue(
                    any(
                        "Scheduled catch-up" in r.getMessage()
                        for r in logs.records
                    ),
                    [r.getMessage() for r in logs.records],
                )

        _run_async(_scenario())

    def test_skipped_when_run_exists_after_fire_time(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                # A row started NOW is at/after ANY latest fire (fires are
                # never in the future), whatever the current wall clock.
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "deepseek", "completed", "datetime('now')")],
                )
                sched = MagicMock()

                count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_skipped_when_provider_disabled(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 0, "cfg.yaml")],
                )
                sched = MagicMock()

                self.assertEqual(await _schedule_catchups(sched, db_path), 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_skipped_when_provider_currently_running(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                # The 'running' row started BEFORE the latest */5 fire, so
                # ONLY the live-run check can suppress this catch-up.
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "*/5 * * * *", 1, "cfg.yaml")],
                    run_inserts=[
                        ("r1", "deepseek", "running", "datetime('now','-1 day')")
                    ],
                )
                sched = MagicMock()

                self.assertEqual(await _schedule_catchups(sched, db_path), 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_staggered_in_provider_order(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        # Insertion order deliberately NOT alphabetical:
                        # ordering comes from ORDER BY provider_name.
                        ("kimi", "0 3 * * *", 1, "examples/config-kimi.yaml"),
                        ("deepseek", "0 3 * * *", 1, "examples/config-deepseek.yaml"),
                    ],
                )
                sched = MagicMock()

                count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 2)
                calls = sched.add_job.call_args_list
                self.assertEqual(calls[0].kwargs["args"][0], "deepseek")
                self.assertEqual(calls[1].kwargs["args"][0], "kimi")
                first = calls[0].kwargs["trigger"].run_date
                second = calls[1].kwargs["trigger"].run_date
                import web.scheduler as scheduler_mod

                stagger = scheduler_mod._CATCHUP_STAGGER_SECONDS
                # Pin the POLICY value, not just the derivation: both tests below
                # read the constant, so without this a regression to 60 s would
                # still pass while the measured restart burst returned.
                self.assertEqual(900, stagger, "catch-up stagger policy (2026-10-24 wave)")
                now = datetime.now(second.tzinfo)
                self.assertLess(
                    abs((first - (now + timedelta(seconds=stagger))).total_seconds()), 5
                )
                self.assertLess(
                    abs((second - (now + timedelta(seconds=stagger * 2))).total_seconds()), 5
                )

        _run_async(_scenario())

    def test_env_gate_off_disables_feature(self) -> None:
        import web.scheduler as scheduler_mod
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                )
                sched = MagicMock()

                with patch.object(scheduler_mod, "_CATCHUP_ENABLED", False):
                    count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_utc_started_at_converted_against_scheduler_tz_fire(self) -> None:
        """TIMEZONE TRAP: run_records.started_at is UTC while cron fires are
        computed in the scheduler tz. With a +5:30 zone, a run started AT the
        latest fire (stored as its UTC instant, 5:30 behind the local fire
        string) must count as RUN — a naive string compare would see
        '12:30' < '18:00' and schedule a spurious catch-up."""
        from apscheduler.triggers.cron import CronTrigger

        from web.scheduler import _schedule_catchups

        # Fixed-offset zone (no tzdata dependency on Windows CI): UTC+5:30.
        ist = timezone(timedelta(hours=5, minutes=30))

        def _fire_utc(now: datetime) -> datetime:
            """Latest '0 * * * *' fire at or before *now*, computed via the
            documented get_next_fire_time chain (independent of the module
            helper under test)."""
            trigger = CronTrigger.from_crontab("0 * * * *", timezone=ist)
            local_now = now.astimezone(ist)
            fire = trigger.get_next_fire_time(
                None, local_now - timedelta(minutes=120)
            )
            assert fire is not None
            while True:
                nxt = trigger.get_next_fire_time(fire, fire)
                if nxt is None or nxt > local_now:
                    break
                fire = nxt
            return fire.astimezone(timezone.utc)

        async def _scenario() -> None:
            now = datetime.now(timezone.utc)
            fire_utc = _fire_utc(now)
            ran_at_fire = fire_utc.strftime("%Y-%m-%d %H:%M:%S")
            before_fire = (fire_utc - timedelta(minutes=10)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                # Case 1: a run row started AT the latest fire (UTC string)
                # → correctly converted → SKIP.
                db_ran = await self._make_db(
                    os.path.join(tmpdir, "ran.db"),
                    schedules=[("p", "0 * * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "p", "completed", f"'{ran_at_fire}'")],
                )
                sched_ran = MagicMock()
                sched_ran.timezone = ist
                self.assertEqual(await _schedule_catchups(sched_ran, db_ran), 0)
                sched_ran.add_job.assert_not_called()

                # Case 2: the only row started BEFORE the fire → catch-up,
                # and the WARNING reason arg quotes the missed fire in UTC.
                db_missed = await self._make_db(
                    os.path.join(tmpdir, "missed.db"),
                    schedules=[("p", "0 * * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "p", "completed", f"'{before_fire}'")],
                )
                sched_missed = MagicMock()
                sched_missed.timezone = ist
                self.assertEqual(await _schedule_catchups(sched_missed, db_missed), 1)
                reason = sched_missed.add_job.call_args.kwargs["args"][2]
                self.assertEqual(reason, fire_utc.isoformat())

        _run_async(_scenario())

    def test_invalid_cron_row_does_not_break_the_pass(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        ("broken", "not-a-cron", 1, "cfg.yaml"),
                        ("deepseek", "0 3 * * *", 1, "cfg.yaml"),
                    ],
                )
                sched = MagicMock()

                # The unparseable row is skipped (its scan- job logged the
                # parse error during rebuild); the healthy row still catches up.
                self.assertEqual(await _schedule_catchups(sched, db_path), 1)
                self.assertEqual(
                    sched.add_job.call_args.kwargs["args"][0], "deepseek"
                )

        _run_async(_scenario())

    def test_catchup_job_logs_warning_and_delegates(self) -> None:
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            with patch.object(
                scheduler_mod, "_run_provider_job", new=AsyncMock()
            ) as mock_run:
                with self.assertLogs("web.scheduler", level="WARNING") as logs:
                    await _run_catchup_job(
                        "deepseek", "cfg.yaml", "2026-09-28T02:00:00+00:00"
                    )
            mock_run.assert_awaited_once_with("deepseek", "cfg.yaml")
            messages = [r.getMessage() for r in logs.records]
            self.assertTrue(
                any("Catch-up firing" in m for m in messages), messages
            )

        _run_async(_scenario())

    # -- execution-time re-check of the missed fire (T6, 2026-09-30) ----------
    #
    # _run_catchup_job fires 60s+ AFTER _schedule_catchups armed it, so the
    # provider's own cron may have satisfied the missed fire in between (the
    # stagger window). Without a re-check the catch-up 409s on the live run and
    # DEFERS it — and the deferred retry later starts a DUPLICATE scan for an
    # already-satisfied fire. These tests pin the re-check at execution time.

    def test_catchup_job_skips_when_missed_fire_already_satisfied(self) -> None:
        """Given a run started at/after the missed fire exists at EXECUTION
        time (the provider's own cron fired during the stagger delay),
        When the catch-up job runs,
        Then it does NOT delegate to _run_provider_job, creates no defer- job,
        and logs INFO 'already satisfied' (fail toward NOT running)."""
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                    # Started NOW >= any past missed fire.
                    run_inserts=[
                        ("r1", "deepseek", "completed", "datetime('now')")
                    ],
                )
                missed_fire = (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat()
                svc = MagicMock()
                svc._db_path = db_path
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        with self.assertLogs("web.scheduler", level="INFO") as logs:
                            await _run_catchup_job("deepseek", "cfg.yaml", missed_fire)
                finally:
                    scheduler_mod._scheduler_service = old_svc

                mock_run.assert_not_awaited()
                svc.schedule_deferred.assert_not_called()
                messages = [r.getMessage() for r in logs.records]
                self.assertTrue(
                    any("already satisfied" in m for m in messages), messages
                )

        _run_async(_scenario())

    def test_catchup_job_skips_when_provider_run_is_live(self) -> None:
        """Given a live 'running' row started BEFORE the missed fire (so only
        the live-run check can suppress the catch-up),
        When the catch-up job runs,
        Then it does NOT delegate — the live run covers the slot (and the
        provider guard would 409-then-DEFER into a duplicate retry anyway)."""
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                    run_inserts=[
                        ("r1", "deepseek", "running", "datetime('now','-1 day')")
                    ],
                )
                missed_fire = (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat()
                svc = MagicMock()
                svc._db_path = db_path
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        with self.assertLogs("web.scheduler", level="INFO") as logs:
                            await _run_catchup_job("deepseek", "cfg.yaml", missed_fire)
                finally:
                    scheduler_mod._scheduler_service = old_svc

                mock_run.assert_not_awaited()
                messages = [r.getMessage() for r in logs.records]
                self.assertTrue(
                    any("still live" in m for m in messages), messages
                )

        _run_async(_scenario())

    def test_catchup_job_delegates_when_no_run_since_missed_fire(self) -> None:
        """REGRESSION: Given the only run row started BEFORE the missed fire,
        When the catch-up job runs,
        Then it delegates to _run_provider_job exactly as before (WARNING
        'Catch-up firing' + the provider/config pair)."""
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                    run_inserts=[
                        ("r1", "deepseek", "completed", "datetime('now','-1 day')")
                    ],
                )
                missed_fire = (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat()
                svc = MagicMock()
                svc._db_path = db_path
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        with self.assertLogs("web.scheduler", level="WARNING") as logs:
                            await _run_catchup_job("deepseek", "cfg.yaml", missed_fire)
                finally:
                    scheduler_mod._scheduler_service = old_svc

                mock_run.assert_awaited_once_with("deepseek", "cfg.yaml")
                messages = [r.getMessage() for r in logs.records]
                self.assertTrue(
                    any("Catch-up firing" in m for m in messages), messages
                )

        _run_async(_scenario())

    def test_catchup_job_delegates_when_recheck_query_fails(self) -> None:
        """Given the re-check DB query raises (broken/unavailable database),
        When the catch-up job runs,
        Then it logs WARNING and PROCEEDS to delegate — pinned deliberately:
        at execution time a duplicate-risk run is safer than silently losing
        the missed firing (the runner's own guards still apply)."""
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            missed_fire = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()
            svc = MagicMock()
            svc._db_path = os.path.join("nonexistent-dir", "h.db")
            old_svc = scheduler_mod._scheduler_service
            scheduler_mod._scheduler_service = svc
            try:
                with patch.object(
                    scheduler_mod,
                    "get_db",
                    new=AsyncMock(side_effect=RuntimeError("db down")),
                ):
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        with self.assertLogs(
                            "web.scheduler", level="WARNING"
                        ) as logs:
                            await _run_catchup_job(
                                "deepseek", "cfg.yaml", missed_fire
                            )
            finally:
                scheduler_mod._scheduler_service = old_svc

            mock_run.assert_awaited_once_with("deepseek", "cfg.yaml")
            messages = [r.getMessage() for r in logs.records]
            self.assertTrue(
                any("re-check failed" in m for m in messages), messages
            )

        _run_async(_scenario())

    def test_catchup_job_delegates_when_missed_fire_unparseable(self) -> None:
        """LEGACY ARGS: Given an empty or non-ISO missed_fire_iso (a job armed
        by an older build, or a hand-triggered callback),
        When the catch-up job runs,
        Then it behaves as today — no re-check, straight delegation."""
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            for legacy_arg in ("", "not-an-iso-timestamp"):
                svc = MagicMock()
                svc._db_path = os.path.join("nonexistent-dir", "h.db")
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    with patch.object(
                        scheduler_mod, "get_db", new=AsyncMock()
                    ) as mock_get_db:
                        with patch.object(
                            scheduler_mod, "_run_provider_job", new=AsyncMock()
                        ) as mock_run:
                            await _run_catchup_job(
                                "deepseek", "cfg.yaml", legacy_arg
                            )
                    # No parseable fire → the re-check is never attempted.
                    mock_get_db.assert_not_awaited()
                    mock_run.assert_awaited_once_with("deepseek", "cfg.yaml")
                finally:
                    scheduler_mod._scheduler_service = old_svc

        _run_async(_scenario())

    def test_init_scheduler_invokes_catchups(self) -> None:
        import types

        import web.scheduler as scheduler_mod
        from web.scheduler import init_scheduler

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "sched.db")
                # Pre-seed ONE far-future row so the seed list is skipped.
                await self._make_db(
                    db_path,
                    schedules=[
                        ("probe", "0 0 1 1 *", 1, "examples/config-deepseek.yaml")
                    ],
                )
                settings = types.SimpleNamespace(db_path=db_path)

                with patch.object(
                    scheduler_mod, "_schedule_catchups", new=AsyncMock(return_value=0)
                ) as mock_catchups:
                    svc = await init_scheduler(settings)
                    try:
                        mock_catchups.assert_awaited_once()
                        self.assertEqual(
                            mock_catchups.await_args.args[1], db_path
                        )
                    finally:
                        await svc.shutdown()
                        # Don't leak the stopped service into the module
                        # global — later tests call the real
                        # shutdown_scheduler and would hit a dead instance.
                        scheduler_mod._scheduler_service = None

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test: reschedule/re-enable catch-up (todo 7, 2026-10-22)
# ---------------------------------------------------------------------------


def _noop_job() -> None:
    """Placeholder callback for jobs that must exist but never run in tests."""


class TestRescheduleCatchup(unittest.TestCase):
    """Given a schedule PUT that re-enables a provider,
    When ``SchedulerService.update_schedule`` rebuilds its cron job,
    Then it arms AT MOST ONE staggered catch-up for the latest missed fire
    through the existing ``_run_catchup_job`` path — and none when the last
    run already covers that fire, a run is live, or the env gate is off.
    """

    PROVIDER = "deepseek"
    CRON = "0 3 * * *"
    CONFIG = "examples/config-deepseek.yaml"

    def setUp(self) -> None:
        # The arm guard is module-level (same as _scheduler_service); each test
        # starts from scratch so one case's armed job cannot leak into another.
        import web.scheduler as scheduler_mod

        scheduler_mod._reschedule_catchup_armed.clear()

    def tearDown(self) -> None:
        import web.scheduler as scheduler_mod

        scheduler_mod._reschedule_catchup_armed.clear()

    @staticmethod
    def _start_service(db_path: str):
        """Real (started) AsyncIOScheduler + SchedulerService over *db_path*."""
        from apscheduler.executors.asyncio import AsyncIOExecutor
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from web.scheduler import SchedulerService

        scheduler = AsyncIOScheduler(executors={"default": AsyncIOExecutor()})
        scheduler.start()
        return SchedulerService(scheduler=scheduler, db_path=db_path)

    @staticmethod
    def _catchup_jobs(scheduler):
        return [
            job
            for job in scheduler.get_jobs()
            if str(job.id).startswith("catchup-")
        ]

    @staticmethod
    async def _make_db(
        db_path: str,
        run_inserts: list[tuple[str, str, str, str]] = (),
    ) -> str:
        """Full-schema temp DB (never the repo's data/) with run rows."""
        from web.db import init_db

        await init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            for run_id, provider, status, started_sql in run_inserts:
                conn.execute(
                    "INSERT INTO run_records "
                    "(id, provider_name, config_file, status, started_at) "
                    f"VALUES (?, ?, 'c.yaml', ?, {started_sql})",
                    (run_id, provider, status),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    async def _update_and_collect(self, db_path: str):
        """Run a real update_schedule and snapshot the scheduler's jobs."""
        from apscheduler.executors.asyncio import AsyncIOExecutor
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from web.scheduler import SchedulerService

        scheduler = AsyncIOScheduler(executors={"default": AsyncIOExecutor()})
        scheduler.start()
        svc = SchedulerService(scheduler=scheduler, db_path=db_path)
        try:
            result = await svc.update_schedule(
                provider_name=self.PROVIDER,
                cron_expression=self.CRON,
                enabled=True,
                config_file=self.CONFIG,
            )
            tz = scheduler.timezone
            jobs = [
                {
                    "id": job.id,
                    "func_name": getattr(job.func, "__name__", str(job.func)),
                    "args": list(job.args),
                    "run_date": getattr(job.trigger, "run_date", None),
                    "max_instances": job.max_instances,
                    "misfire_grace_time": job.misfire_grace_time,
                }
                for job in scheduler.get_jobs()
            ]
            scan_job_present = (
                scheduler.get_job(f"scan-{self.PROVIDER}") is not None
            )
        finally:
            # svc.shutdown() also clears the module-level arm guard.
            await svc.shutdown()
        return result, jobs, scan_job_present, tz

    async def _armed_catchup(self, svc):
        """Arm a reschedule catch-up and take its one-shot out of the store.

        APScheduler 3.11 removes a due DateTrigger job BEFORE invoking its
        callback (``base.py`` submit then remove), so calling the captured
        func/args after this removal models exactly the execution window the
        deferral guard must cover.
        """
        await svc.update_schedule(self.PROVIDER, self.CRON, True, self.CONFIG)
        jobs = self._catchup_jobs(svc._scheduler)
        self.assertEqual(len(jobs), 1, jobs)
        job = jobs[0]
        svc._scheduler.remove_job(job.id)
        return job

    def test_run_covering_latest_fire_arms_zero_catchups(self) -> None:
        """Case (i): the latest fire predates the last run → ZERO catch-ups.

        A run started NOW is at/after any latest fire (fires are never in the
        future), whatever the wall clock, so this pins the "already covered"
        branch rather than the live-run one.
        """

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        ("r1", self.PROVIDER, "completed", "datetime('now')")
                    ],
                )

                result, jobs, scan_present, _tz = await self._update_and_collect(
                    db_path
                )

                self.assertTrue(scan_present, "the cron job must be rebuilt")
                self.assertTrue(result["next_run_time"])
                self.assertEqual(
                    [j for j in jobs if j["id"].startswith("catchup-")], []
                )

        _run_async(_scenario())

    def test_missed_fire_arms_exactly_one_staggered_catchup(self) -> None:
        """Case (ii): latest fire POSTDATES the last run → EXACTLY ONE catch-up.

        The daily cron has fired many times in the 8-day lookback; only the
        latest fire may be armed (no backfill). The job must go through the
        one-shot ``_run_catchup_job`` (so cap + deferral ladder apply) at
        ``_CATCHUP_STAGGER_SECONDS`` from now.
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        (
                            "r1",
                            self.PROVIDER,
                            "completed",
                            "datetime('now','-2 day')",
                        )
                    ],
                )

                _result, jobs, scan_present, tz = await self._update_and_collect(
                    db_path
                )

                self.assertTrue(scan_present)
                catchups = [j for j in jobs if j["id"].startswith("catchup-")]
                self.assertEqual(len(catchups), 1, catchups)
                job = catchups[0]
                self.assertEqual(job["func_name"], "_run_catchup_job")
                self.assertEqual(job["args"][0], self.PROVIDER)
                self.assertEqual(job["args"][1], self.CONFIG)
                self.assertEqual(job["max_instances"], 1)
                self.assertIsNone(job["misfire_grace_time"])

                # The reason arg is the LATEST fire (UTC ISO), not an older one.
                expected_fire = scheduler_mod._previous_fire_time(
                    self.CRON, tz
                )
                self.assertIsNotNone(expected_fire)
                self.assertEqual(
                    job["args"][2],
                    expected_fire.astimezone(timezone.utc).isoformat(),
                )

                # One stagger slot after now, measured in the job's tz.
                run_date = job["run_date"]
                self.assertIsNotNone(run_date)
                expected_run = datetime.now(run_date.tzinfo) + timedelta(
                    seconds=scheduler_mod._CATCHUP_STAGGER_SECONDS
                )
                self.assertLess(
                    abs((run_date - expected_run).total_seconds()), 5
                )

        _run_async(_scenario())

    def test_live_run_suppresses_catchup(self) -> None:
        """Case (iii): a live 'running' row → ZERO catch-ups.

        The row started BEFORE the latest */5 fire, so only the
        ``_catchup_state`` live-run branch can suppress this arm.
        """

        async def _scenario() -> None:
            self.CRON = "*/5 * * * *"
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = await self._make_db(
                        os.path.join(tmpdir, "h.db"),
                        run_inserts=[
                            (
                                "r1",
                                self.PROVIDER,
                                "running",
                                "datetime('now','-1 day')",
                            )
                        ],
                    )

                    _result, jobs, scan_present, _tz = (
                        await self._update_and_collect(db_path)
                    )

                    self.assertTrue(scan_present)
                    self.assertEqual(
                        [j for j in jobs if j["id"].startswith("catchup-")],
                        [],
                    )
            finally:
                self.CRON = "0 3 * * *"

        _run_async(_scenario())

    def test_env_gate_off_disables_reschedule_catchup(self) -> None:
        """Case (iv): HARVESTER_SCHEDULER_CATCHUP=0 (``_CATCHUP_ENABLED``
        False) → the cron job is still rebuilt, but NO catch-up is armed.
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(os.path.join(tmpdir, "h.db"))

                with patch.object(scheduler_mod, "_CATCHUP_ENABLED", False):
                    _result, jobs, scan_present, _tz = (
                        await self._update_and_collect(db_path)
                    )

                self.assertTrue(scan_present)
                self.assertEqual(
                    [j for j in jobs if j["id"].startswith("catchup-")], []
                )

        _run_async(_scenario())

    # -- 2026-10-22 verifier probes: deterministic double-fire fixes ---------

    def test_pending_deferral_ladder_suppresses_catchup(self) -> None:
        """PROBE A (verifier 2026-10-22): an earlier firing was 429/409 and
        ``schedule_deferred`` already queued a ``defer-`` job. Arming a
        catch-up here starts run-1 now; when run-1 finishes the pending
        ladder retries and starts run-2 — TWO scans for ONE missed fire.
        The arm must be skipped with one INFO line naming the ladder.
        """

        async def _scenario() -> None:
            from apscheduler.triggers.date import DateTrigger

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(os.path.join(tmpdir, "h.db"))
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                try:
                    scheduler.add_job(
                        _noop_job,
                        trigger=DateTrigger(
                            run_date=datetime.now(scheduler.timezone)
                            + timedelta(minutes=30)
                        ),
                        id=f"defer-{self.PROVIDER}-{int(time.time() * 1000)}",
                    )

                    with self.assertLogs("web.scheduler", level="INFO") as logs:
                        await svc.update_schedule(
                            self.PROVIDER, self.CRON, True, self.CONFIG
                        )

                    self.assertEqual(
                        self._catchup_jobs(scheduler),
                        [],
                        "a pending deferral ladder already owns the slot — no "
                        "catch-up may be armed",
                    )
                    messages = [r.getMessage() for r in logs.records]
                    self.assertTrue(
                        any(
                            "deferral ladder already owns the slot" in message
                            for message in messages
                        ),
                        messages,
                    )
                finally:
                    await svc.shutdown()

        _run_async(_scenario())

    def test_two_updates_same_tick_arm_exactly_one_catchup(self) -> None:
        """PROBE B (verifier 2026-10-22): two schedule PUTs for the same
        provider run in the same event-loop tick and both pass the
        ``_catchup_state`` re-check before any run row exists. Exactly ONE
        catch-up may be armed — two callbacks would race, one would 409 into
        the deferral ladder, and the ladder would later start a second scan.

        The gate holds the first callback inside its state read until either
        the second PUT reaches the same point (unguarded: two arms) or the
        bounded wait expires (guarded: the second PUT is a no-op before the
        state read, so this call is the only entrant).
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            entered = 0
            release = asyncio.Event()

            async def _gated_state(db, provider):
                nonlocal entered
                entered += 1
                if entered == 1:
                    try:
                        await asyncio.wait_for(release.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                else:
                    release.set()
                return False, None

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        (
                            self.PROVIDER,
                            self.PROVIDER,
                            "completed",
                            "datetime('now','-2 day')",
                        )
                    ],
                )
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                try:
                    with patch.object(
                        scheduler_mod, "_catchup_state", _gated_state
                    ):
                        await asyncio.gather(
                            svc.update_schedule(
                                self.PROVIDER, self.CRON, True, self.CONFIG
                            ),
                            svc.update_schedule(
                                self.PROVIDER, self.CRON, True, self.CONFIG
                            ),
                        )

                    catchups = self._catchup_jobs(scheduler)
                    self.assertEqual(
                        len(catchups),
                        1,
                        "one missed fire must arm one catch-up, got "
                        f"{[str(j.id) for j in catchups]}",
                    )
                    self.assertEqual(
                        entered,
                        1,
                        "the same-tick second PUT must be a no-op BEFORE the "
                        "state re-check (the in-process guard serializes them)",
                    )
                    self.assertIsNotNone(
                        scheduler.get_job(f"scan-{self.PROVIDER}")
                    )
                finally:
                    await svc.shutdown()

        _run_async(_scenario())

    def test_guard_clears_after_armed_job_executes(self) -> None:
        """PROBE B follow-up: the in-process guard must not leak. Once the
        armed one-shot executes (and APScheduler removes it), a later PUT may
        arm again for the next missed slot.
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        (
                            self.PROVIDER,
                            self.PROVIDER,
                            "completed",
                            "datetime('now','-2 day')",
                        )
                    ],
                )
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    await svc.update_schedule(
                        self.PROVIDER, self.CRON, True, self.CONFIG
                    )
                    first = self._catchup_jobs(scheduler)
                    self.assertEqual(len(first), 1)
                    job_id = first[0].id
                    func = first[0].func
                    args = tuple(first[0].args)

                    # APScheduler removes a one-shot DateTrigger job once it
                    # has run; simulate that removal before invoking the
                    # callback through its scheduled func/args.
                    scheduler.remove_job(job_id)
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        await func(*args)
                    mock_run.assert_awaited_once_with(self.PROVIDER, self.CONFIG)
                    self.assertEqual(
                        scheduler_mod._reschedule_catchup_armed,
                        set(),
                        "the guard must be cleared by its armed job",
                    )

                    await svc.update_schedule(
                        self.PROVIDER, self.CRON, True, self.CONFIG
                    )
                    second = self._catchup_jobs(scheduler)
                    self.assertEqual(
                        len(second),
                        1,
                        "after the armed job ran, a later PUT must be able "
                        "to arm the next missed slot",
                    )
                    self.assertIsNone(scheduler.get_job(job_id))
                finally:
                    scheduler_mod._scheduler_service = old_svc
                    await svc.shutdown()

        _run_async(_scenario())

    def test_pending_startup_catchup_blocks_reschedule_arm(self) -> None:
        """PROBE B, startup half: ``_schedule_catchups`` already armed a
        one-shot for the same fire and its 60 s stagger window is still open.
        A PUT must not arm a second callback — the pending one owns the slot.
        """

        async def _scenario() -> None:
            from apscheduler.triggers.date import DateTrigger

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(os.path.join(tmpdir, "h.db"))
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                try:
                    pending_id = (
                        f"catchup-{self.PROVIDER}-{int(time.time() * 1000)}"
                    )
                    scheduler.add_job(
                        _noop_job,
                        trigger=DateTrigger(
                            run_date=datetime.now(scheduler.timezone)
                            + timedelta(seconds=60)
                        ),
                        id=pending_id,
                    )

                    with self.assertLogs("web.scheduler", level="INFO") as logs:
                        await svc.update_schedule(
                            self.PROVIDER, self.CRON, True, self.CONFIG
                        )

                    self.assertEqual(
                        [str(j.id) for j in self._catchup_jobs(scheduler)],
                        [pending_id],
                        "the already-armed startup catch-up must stay the "
                        "only one for this slot",
                    )
                    messages = [r.getMessage() for r in logs.records]
                    self.assertTrue(
                        any(
                            "pending one-shot owns the missed slot" in message
                            for message in messages
                        ),
                        messages,
                    )
                finally:
                    await svc.shutdown()

        _run_async(_scenario())
    # -- 2026-10-22 residual: EXECUTION-TIME deferral guard -----------------

    def test_execution_time_deferral_guard_suppresses_catchup(self) -> None:
        """Case (ix): the 429 callback RE-QUEUES the ladder after the catch-up
        was armed. APScheduler removes the due ``defer-`` one-shot before its
        callback runs, so a PUT inside that window sees no pending ladder and
        arms; at execution time the re-queued ladder owns the slot.

        The armed job must skip with one INFO line, leaving exactly ONE scan
        for the missed fire (the ladder's) — never two.
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        (
                            self.PROVIDER,
                            self.PROVIDER,
                            "completed",
                            "datetime('now','-2 day')",
                        )
                    ],
                )
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    job = await self._armed_catchup(svc)
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        # step 4: the deferral callback 429s + re-queues the
                        # ladder AFTER the arm (scheduled under the patch so
                        # the ladder's retry is observable below).
                        self.assertTrue(
                            svc.schedule_deferred(self.PROVIDER, self.CONFIG, 0)
                        )
                        with self.assertLogs(
                            "web.scheduler", level="INFO"
                        ) as logs:
                            await job.func(*job.args)
                        calls_after_catchup = mock_run.await_count

                        # step 5: the ladder retry fires later (one scan).
                        defer_job = [
                            j
                            for j in scheduler.get_jobs()
                            if str(j.id).startswith(
                                f"defer-{self.PROVIDER}-"
                            )
                        ][0]
                        scheduler.remove_job(defer_job.id)
                        await defer_job.func(*defer_job.args)
                    calls_total = mock_run.await_count

                    # Assert the TOTAL first: without the guard this reports
                    # two runner calls for one missed fire.
                    self.assertEqual(
                        calls_total,
                        1,
                        "one missed fire must produce exactly one scan; the "
                        "catch-up must not add a second beside the ladder",
                    )
                    self.assertEqual(
                        calls_after_catchup,
                        0,
                        "the armed catch-up must not start a scan while a "
                        "deferral ladder owns the slot",
                    )
                    messages = [r.getMessage() for r in logs.records]
                    self.assertEqual(
                        [
                            m
                            for m in messages
                            if "Reschedule catch-up skipped" in m
                            and "deferral ladder" in m
                        ],
                        [
                            f"Reschedule catch-up skipped for {self.PROVIDER}: "
                            f"a deferral ladder is queued and owns the missed "
                            f"slot"
                        ],
                        messages,
                    )
                    self.assertEqual(
                        scheduler_mod._reschedule_catchup_armed, set()
                    )
                finally:
                    scheduler_mod._scheduler_service = old_svc
                    await svc.shutdown()

        _run_async(_scenario())

    def test_execution_time_guard_allows_catchup_without_a_ladder(self) -> None:
        """Case (x): no deferral ladder is queued at execution time → the new
        guard must not over-suppress; the armed catch-up runs exactly ONE scan
        through ``_run_provider_job``.
        """

        async def _scenario() -> None:
            import web.scheduler as scheduler_mod

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    run_inserts=[
                        (
                            self.PROVIDER,
                            self.PROVIDER,
                            "completed",
                            "datetime('now','-2 day')",
                        )
                    ],
                )
                svc = self._start_service(db_path)
                scheduler = svc._scheduler
                old_svc = scheduler_mod._scheduler_service
                scheduler_mod._scheduler_service = svc
                try:
                    job = await self._armed_catchup(svc)
                    self.assertFalse(
                        any(
                            str(j.id).startswith(
                                f"defer-{self.PROVIDER}-"
                            )
                            for j in scheduler.get_jobs()
                        ),
                        "fixture: no ladder may be queued for case (x)",
                    )
                    with patch.object(
                        scheduler_mod, "_run_provider_job", new=AsyncMock()
                    ) as mock_run:
                        await job.func(*job.args)
                    mock_run.assert_awaited_once_with(
                        self.PROVIDER, self.CONFIG
                    )
                    self.assertEqual(
                        scheduler_mod._reschedule_catchup_armed, set()
                    )
                finally:
                    scheduler_mod._scheduler_service = old_svc
                    await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_SCHEDULE_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS schedule_config ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "provider_name TEXT NOT NULL UNIQUE, "
    "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
    "enabled INTEGER NOT NULL DEFAULT 1, "
    "config_file TEXT NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")"
)


class _FakeRunner:
    """Stand-in for web.runner.PipelineRunner.

    ``run_scan`` resolves with a fixed run_id as soon as it is called — like
    the real runner, which returns once the scan *thread* starts. The raised
    error (if any) mirrors run_scan's failure modes: HTTPException(409) from
    the runner's own guard, ValueError for a missing config template.
    ``get_run`` reports the mutable ``status`` so tests can keep the watcher
    polling ('running') and then flip it terminal to observe guard release.
    ``started_at`` (optional, UTC string like ``datetime('now')`` writes)
    feeds the watch-deadline computation. ``_update_run_sync`` records calls
    so tests can pin the watcher's best-effort ``running→failed`` flip
    (status + ``only_if_running`` kwargs) without touching a real DB.
    ``_count_valid_keys_for_failed_run`` mirrors the runner helper the
    deadline flip consults so the flip can carry ``valid_keys_found``;
    ``failed_run_valid_keys`` is the scripted return value and
    ``count_valid_keys_calls`` records the ``(provider, temp_yaml_path)``
    args it was called with. ``_dispatch_provider_pushes`` records the
    watch-deadline salvage dispatch (2026-10-18: the flip used to strand
    validated keys on disk until a restart); ``dispatch_error`` scripts a
    raise to prove the watcher swallows it.
    """

    def __init__(self, run_id: str = "run-fake-1") -> None:
        self.run_id = run_id
        self.status = "running"
        self.started_at: str | None = None
        self.run_scan_calls: list[tuple[str, str | None]] = []
        self.run_scan_error: Exception | None = None
        self.update_run_calls: list[tuple[tuple, dict]] = []
        self.failed_run_valid_keys: int | None = 0
        self.count_valid_keys_calls: list[tuple[str, object]] = []
        self.dispatch_calls: list[tuple[str, str]] = []
        self.dispatch_error: Exception | None = None

    def _dispatch_provider_pushes(self, provider_name: str, run_id: str) -> None:
        self.dispatch_calls.append((provider_name, run_id))
        if self.dispatch_error is not None:
            raise self.dispatch_error

    async def run_scan(
        self, provider_name: str, config_file: str | None = None
    ) -> str:
        self.run_scan_calls.append((provider_name, config_file))
        if self.run_scan_error is not None:
            raise self.run_scan_error
        return self.run_id

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        record: dict[str, object] = {"status": self.status}
        if self.started_at is not None:
            record["started_at"] = self.started_at
        return record

    def _count_valid_keys_for_failed_run(
        self, provider_name: str, temp_yaml_path: object = None
    ) -> int | None:
        self.count_valid_keys_calls.append((provider_name, temp_yaml_path))
        return self.failed_run_valid_keys

    def _update_run_sync(self, *args: object, **kwargs: object) -> None:
        self.update_run_calls.append((args, kwargs))


class TestWatchDeadlineSalvagePush(unittest.TestCase):
    """The 2026-10-18 stranded-keys gap: a watch-deadline flip must ALSO
    dispatch the provider pushes when validated keys sit on disk — the
    startup-only recovery (``recover_unpushed_runs``) never fires without a
    restart, and each provider's next clean-start run resets
    ``valid-keys.txt``. Measured on prod: brave/exa/nvidia were flipped at
    the 36 h deadline holding 155 validated keys with no push.
    """

    def _run_watch(self, fake: "_FakeRunner") -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ), patch(
                    "web.scheduler._RUN_WATCH_MAX_HOURS", 1e-5, create=True
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))
                    released = await _released_within(svc, "deepseek", attempts=300)
                    self.assertTrue(released, "watch must still release at deadline")
                await svc.shutdown()

        _run_async(_scenario())

    def test_flip_with_validated_keys_dispatches_salvage_push(self) -> None:
        fake = _FakeRunner()
        fake.failed_run_valid_keys = 17

        self._run_watch(fake)

        self.assertEqual(len(fake.update_run_calls), 1)
        self.assertEqual(
            fake.dispatch_calls,
            [("deepseek", fake.run_id)],
            "a deadline flip holding validated keys must dispatch the pushes "
            "through the same idempotent path the completion hook uses",
        )

    def test_flip_without_keys_skips_dispatch(self) -> None:
        fake = _FakeRunner()
        fake.failed_run_valid_keys = 0

        self._run_watch(fake)

        self.assertEqual(len(fake.update_run_calls), 1)
        self.assertEqual(
            fake.dispatch_calls,
            [],
            "zero validated keys must not fire a pointless dispatch",
        )

    def test_dispatch_error_is_swallowed_and_flip_still_lands(self) -> None:
        fake = _FakeRunner()
        fake.failed_run_valid_keys = 5
        fake.dispatch_error = RuntimeError("push layer down")

        self._run_watch(fake)

        self.assertEqual(len(fake.update_run_calls), 1)
        args, kwargs = fake.update_run_calls[0]
        self.assertEqual(args[:2], (fake.run_id, "failed"))
        self.assertEqual(
            fake.dispatch_calls,
            [("deepseek", fake.run_id)],
            "the dispatch was attempted before it raised",
        )


def _make_service_with_row(tmpdir: str, provider: str, config_file: str):
    """Build a SchedulerService over a fresh DB holding one schedule row."""
    from web.scheduler import SchedulerService

    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEDULE_TABLE_DDL)
    conn.execute(
        "INSERT INTO schedule_config "
        "(provider_name, cron_expression, enabled, config_file) "
        "VALUES (?, ?, ?, ?)",
        (provider, "0 3 * * *", 1, config_file),
    )
    conn.commit()
    conn.close()
    return SchedulerService(scheduler=MagicMock(), db_path=db_path)


def _make_settings(db_path: str):
    """Create a minimal settings-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class _Settings:
        db_path: str = ""
        host: str = "127.0.0.1"
        port: int = 8000
        cors_origins: list = field(default_factory=lambda: ["*"])
        web_auth_key: str = "test-key"
        gpt_load_base_url: str = ""
        gpt_load_auth_key: str = ""
        encryption_key: str | None = None

    return _Settings(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
