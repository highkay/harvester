#!/usr/bin/env python3
"""CheckStage integration with the terminal-verdict cache (plan todo 6).

Given a cache-eligible target task (mimo-cn/mimo-sg/qwen-cn/qwen-intl/doubao),
When the workspace verdict cache already holds a fresh explicit INVALID_KEY or
NO_QUOTA for the same fingerprint,
Then CheckStage emits the identical result bucket without calling the provider,
without probing capability tiers, and without touching the rate limiter.
Every other verdict (valid, UNKNOWN, NO_MODEL, NO_ACCESS, BAD_REQUEST,
retryable, or anything derived from a capability-tier probe) must stay live and
must never be written to the cache.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from config.schemas import Config, StageConfig, TaskConfig
from constant.capability import TIER_NO_MODEL
from core.enums import ErrorReason, ResultType
from core.models import CheckResult, CheckTask, Service
from core.types import IAuthProvider
from provider.mimo import MiMoProvider
from provider.ollama import OllamaProvider
from stage.base import StageOutput, StageResources
from stage.definition import CheckStage
from state.models import CheckCacheStats
from storage.verdict_cache import INVALID_KEY_TTL_SECONDS, VerdictCache, VerdictEntry
from tools.utils import get_service_name

TASK = "mimo-cn"
KEY = "sk-cache-test-0123456789abcdef0123456789abcdef"
ADDRESS = "https://token-plan-cn.xiaomimimo.com/v1"
ENDPOINT = "/chat/completions"
MODEL = "mimo-v2.6-pro"


class RecordingLimiter:
    """Minimal limiter double: records the calls a live probe makes."""

    def __init__(self) -> None:
        self.acquire_calls: list[str] = []
        self.report_calls: list[tuple[str, bool]] = []
        self.wait_calls: list[str] = []

    def acquire(self, service_type: str) -> bool:
        self.acquire_calls.append(service_type)
        return True

    def wait_time(self, service_type: str) -> float:
        self.wait_calls.append(service_type)
        return 0.0

    def report_result(self, service_type: str, success: bool) -> None:
        self.report_calls.append((service_type, success))


class ExplodingLimiter:
    """Any limiter interaction on a cache hit is a hard test failure."""

    def acquire(self, service_type: str) -> bool:
        raise AssertionError(f"limiter.acquire called on cache hit: {service_type}")

    def wait_time(self, service_type: str) -> float:
        raise AssertionError(f"limiter.wait_time called on cache hit: {service_type}")

    def report_result(self, service_type: str, success: bool) -> None:
        raise AssertionError(f"limiter.report_result called on cache hit: {service_type}")


def _make_provider(name: str = TASK, tiers: list[dict[str, str]] | None = None) -> MiMoProvider:
    kwargs: dict[str, object] = {}
    if tiers is not None:
        kwargs["capability_tiers"] = tiers
    return MiMoProvider(conditions=[], name=name, **kwargs)


def _make_resources(
    workspace: str,
    provider: MiMoProvider | OllamaProvider,
    cache: VerdictCache | None,
    limiter: Any = None,
) -> StageResources:
    name = provider.name
    effective_limiter: Any = RecordingLimiter() if limiter is None else limiter
    return StageResources(
        limiter=effective_limiter,
        providers={name: provider},
        config=cast(Config, SimpleNamespace(global_config=SimpleNamespace(workspace=workspace))),
        task_configs={name: TaskConfig(name=name, provider_type="mimo", stages=StageConfig())},
        auth=cast(IAuthProvider, SimpleNamespace()),
        verdict_cache=cache,
    )


def _make_stage(
    workspace: str,
    provider: MiMoProvider | OllamaProvider,
    cache: VerdictCache | None,
    limiter: Any = None,
) -> CheckStage:
    return CheckStage(_make_resources(workspace, provider, cache, limiter), handler=lambda _o: None)


def _make_task(
    name: str = TASK,
    key: str = KEY,
    address: str = ADDRESS,
    endpoint: str = "",
    model: str = "",
    custom_url: str = "",
) -> CheckTask:
    return CheckTask(
        provider=name,
        service=Service(key=key, address=address, endpoint=endpoint, model=model),
        custom_url=custom_url,
    )


def _kinds(output: StageOutput | None) -> list[str]:
    if output is None:
        return []
    return [kind for _provider, kind, _data in output.results]


def _cache_entry(cache: VerdictCache, task: CheckTask, **identity: str) -> VerdictEntry | None:
    return cache.lookup(
        task=task.provider,
        key=task.service.key,
        base_url=identity.get("base_url", task.custom_url or task.service.address),
        endpoint=identity.get("endpoint", task.service.endpoint or "chat/completions"),
        probe_model=identity.get("probe_model", task.service.model or MODEL),
        tier=identity.get("tier", ""),
    )


class TestCacheHitShortCircuit(unittest.TestCase):
    """A repeat terminal verdict is served from the cache, not the network."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name
        self.cache = VerdictCache(self.workspace)

    def _stage_with(self, result: CheckResult) -> tuple[CheckStage, MiMoProvider, RecordingLimiter]:
        provider = _make_provider()
        limiter = RecordingLimiter()
        stage = _make_stage(self.workspace, provider, self.cache, limiter)
        self.check = mock.Mock(return_value=result)
        self._patch = mock.patch.object(provider, "check", self.check)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        return stage, provider, limiter

    def test_invalid_key_hit_has_identical_bucket_and_no_live_call(self) -> None:
        stage, _provider, limiter = self._stage_with(CheckResult.fail(ErrorReason.INVALID_KEY))
        task = _make_task()

        first = stage._check_worker(task)
        second = stage._check_worker(task)

        assert first is not None
        assert second is not None
        self.assertEqual(_kinds(first), [ResultType.INVALID.value])
        self.assertEqual(_kinds(second), _kinds(first))
        self.assertEqual(first.results, second.results)
        self.assertEqual(self.check.call_count, 1, "second pass must not probe the provider")
        self.assertEqual(limiter.acquire_calls, [get_service_name(TASK)])
        self.assertEqual(limiter.report_calls, [(get_service_name(TASK), True)])

        metrics = stage.cache_metrics()
        self.assertEqual(metrics["live_probes"], 1)
        self.assertEqual(metrics["cache_hits"], 1)
        self.assertEqual(metrics["cache_hits_invalid"], 1)
        self.assertEqual(metrics["cache_hits_no_quota"], 0)

    def test_no_quota_hit_has_identical_bucket_and_no_live_call(self) -> None:
        stage, _provider, limiter = self._stage_with(CheckResult.fail(ErrorReason.NO_QUOTA))
        task = _make_task()

        first = stage._check_worker(task)
        second = stage._check_worker(task)

        assert first is not None
        assert second is not None
        self.assertEqual(_kinds(first), [ResultType.NO_QUOTA.value])
        self.assertEqual(_kinds(second), _kinds(first))
        self.assertEqual(first.results, second.results)
        self.assertEqual(self.check.call_count, 1)
        self.assertEqual(len(limiter.acquire_calls), 1)
        self.assertEqual(len(limiter.report_calls), 1)
        self.assertEqual(stage.cache_metrics()["cache_hits_no_quota"], 1)

    def test_hit_does_not_touch_the_rate_limiter_at_all(self) -> None:
        stage, _provider, _limiter = self._stage_with(CheckResult.fail(ErrorReason.INVALID_KEY))
        task = _make_task()
        self.assertIsNotNone(stage._check_worker(task))

        stage.resources.limiter = ExplodingLimiter()  # type: ignore[assignment]
        second = stage._check_worker(task)

        self.assertEqual(_kinds(second), [ResultType.INVALID.value])
        self.assertEqual(self.check.call_count, 1)

    def test_hit_skips_tier_probes(self) -> None:
        provider = _make_provider(
            tiers=[{"name": "seed", "probe_model": "mimo-v2.6-pro"}]
        )
        tier_probe = mock.Mock(side_effect=AssertionError("tier probe must not run on a hit"))
        provider._apply_capability_tiers = tier_probe  # type: ignore[method-assign]
        limiter = RecordingLimiter()
        stage = _make_stage(self.workspace, provider, self.cache, limiter)
        self.check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
        self._patch = mock.patch.object(provider, "check", self.check)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        task = _make_task()
        stage._check_worker(task)
        stage._check_worker(task)

        self.assertEqual(self.check.call_count, 1)
        tier_probe.assert_not_called()

    def test_a_new_stage_instance_hits_the_persisted_verdict(self) -> None:
        """A restart (new stage/resources on the same workspace) still hits."""
        provider_one = _make_provider()
        first_stage = _make_stage(self.workspace, provider_one, VerdictCache(self.workspace))
        with mock.patch.object(
            provider_one, "check", return_value=CheckResult.fail(ErrorReason.INVALID_KEY)
        ):
            first_stage._check_worker(_make_task())

        provider_two = _make_provider()
        check = mock.Mock(side_effect=AssertionError("restart must not re-probe"))
        second_stage = _make_stage(self.workspace, provider_two, VerdictCache(self.workspace))
        with mock.patch.object(provider_two, "check", check):
            output = second_stage._check_worker(_make_task())

        self.assertEqual(_kinds(output), [ResultType.INVALID.value])
        check.assert_not_called()
        self.assertEqual(second_stage.cache_metrics()["cache_hits"], 1)

    def test_expired_entry_takes_the_live_path(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 1_000_000.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        cache = VerdictCache(self.workspace, clock=clock)
        cache.store(
            task=TASK,
            key=KEY,
            base_url=ADDRESS,
            endpoint="chat/completions",
            probe_model=MODEL,
            tier="",
            verdict=ErrorReason.INVALID_KEY,
        )
        clock.now += INVALID_KEY_TTL_SECONDS
        provider = _make_provider()
        stage = _make_stage(self.workspace, provider, cache)
        check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
        with mock.patch.object(provider, "check", check):
            output = stage._check_worker(_make_task())

        self.assertEqual(_kinds(output), [ResultType.INVALID.value])
        self.assertEqual(check.call_count, 1)
        self.assertEqual(stage.cache_metrics()["cache_hits"], 0)


class TestMissBehaviour(unittest.TestCase):
    """Misses keep the existing limiter/report semantics and store only terminals."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name
        self.cache = VerdictCache(self.workspace)

    def _run_twice(self, result: CheckResult, name: str = TASK, provider: Any = None) -> tuple:
        provider = provider or _make_provider()
        limiter = RecordingLimiter()
        stage = _make_stage(self.workspace, provider, self.cache, limiter)
        check = mock.Mock(return_value=result)
        with mock.patch.object(provider, "check", check):
            task = _make_task(name=name)
            first = stage._check_worker(task)
            second = stage._check_worker(task)
        return stage, limiter, check, first, second, task

    def test_reports_real_outcome_to_limiter_on_every_miss(self) -> None:
        stage, limiter, check, _first, _second, _task = self._run_twice(
            CheckResult.fail(ErrorReason.RATE_LIMITED)
        )
        service = get_service_name(TASK)

        self.assertEqual(check.call_count, 2, "retryable verdicts must never be cached")
        self.assertEqual(limiter.acquire_calls, [service, service])
        self.assertEqual(limiter.report_calls, [(service, False), (service, False)])
        self.assertEqual(stage.cache_metrics()["live_probes"], 2)
        self.assertEqual(stage.cache_metrics()["cache_hits"], 0)

    def test_only_invalid_and_no_quota_are_stored(self) -> None:
        for result in (
            CheckResult.fail(ErrorReason.INVALID_KEY),
            CheckResult.fail(ErrorReason.NO_QUOTA),
        ):
            with self.subTest(reason=result.reason):
                provider = _make_provider()
                check = mock.Mock(return_value=result)
                with tempfile.TemporaryDirectory() as workspace, mock.patch.object(provider, "check", check):
                    cache = VerdictCache(workspace)
                    limiter = RecordingLimiter()
                    stage = _make_stage(workspace, provider, cache, limiter)
                    task = _make_task()
                    stage._check_worker(task)
                    stage._check_worker(task)

                    self.assertEqual(check.call_count, 1)
                    self.assertIsNotNone(_cache_entry(cache, task))

    def test_forbidden_verdicts_always_stay_live(self) -> None:
        forbidden = (
            CheckResult.fail(ErrorReason.UNKNOWN),
            CheckResult.fail(ErrorReason.NO_MODEL),
            CheckResult.fail(ErrorReason.NO_ACCESS),
            CheckResult.fail(ErrorReason.BAD_REQUEST),
            CheckResult.fail(ErrorReason.RATE_LIMITED),
            CheckResult.fail(ErrorReason.NETWORK_ERROR),
            CheckResult.success(),
        )
        for result in forbidden:
            with self.subTest(reason=result.reason, available=result.available):
                provider = _make_provider()
                check = mock.Mock(return_value=result)
                with tempfile.TemporaryDirectory() as workspace, mock.patch.object(provider, "check", check):
                    cache = VerdictCache(workspace)
                    stage = _make_stage(workspace, provider, cache, RecordingLimiter())
                    task = _make_task()
                    stage._check_worker(task)
                    stage._check_worker(task)

                    self.assertEqual(check.call_count, 2, "every non-terminal verdict must re-probe")
                    self.assertIsNone(_cache_entry(cache, task))

    def test_tier_derived_invalid_is_never_stored(self) -> None:
        result = CheckResult.fail(ErrorReason.INVALID_KEY)
        result.tiers = {"seed": TIER_NO_MODEL}
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=result)
            with mock.patch.object(provider, "check", check):
                task = _make_task()
                stage._check_worker(task)
                stage._check_worker(task)

            self.assertEqual(check.call_count, 2)
            self.assertIsNone(_cache_entry(cache, task))


class TestFingerprintInvalidation(unittest.TestCase):
    """A changed region/address/model/tier declaration must force a live probe."""

    def _run_twice_with_change(self, changed: dict[str, str], tiers: list[dict[str, str]] | None = None) -> int:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider(tiers=tiers)
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(provider, "check", check):
                first_task = _make_task(endpoint=ENDPOINT)
                stage._check_worker(first_task)
                second_task = _make_task(**changed)
                stage._check_worker(second_task)
            return check.call_count

    def test_custom_address_change_is_a_miss(self) -> None:
        calls = self._run_twice_with_change({"custom_url": "https://token-plan-sgp.xiaomimimo.com/v1"})
        self.assertEqual(calls, 2)

    def test_model_change_is_a_miss(self) -> None:
        calls = self._run_twice_with_change({"model": "mimo-v2.6-lite"})
        self.assertEqual(calls, 2)

    def test_endpoint_change_is_a_miss(self) -> None:
        calls = self._run_twice_with_change({"endpoint": "/v1/models"})
        self.assertEqual(calls, 2)

    def test_tier_declaration_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider(tiers=[{"name": "seed", "probe_model": "mimo-v2.6-pro"}])
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(provider, "check", check):
                task = _make_task()
                stage._check_worker(task)
                provider._capability_tiers = [
                    {"name": "seed", "probe_model": "mimo-v2.6-lite"}
                ]
                stage._check_worker(task)
            self.assertEqual(check.call_count, 2)

    def test_provider_default_address_is_part_of_the_identity(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            cn_provider = MiMoProvider(conditions=[], name=TASK)
            cn_stage = _make_stage(workspace, cn_provider, cache, RecordingLimiter())
            with mock.patch.object(
                cn_provider, "check", return_value=CheckResult.fail(ErrorReason.INVALID_KEY)
            ):
                cn_stage._check_worker(_make_task(address=""))

            sg_provider = MiMoProvider(
                conditions=[],
                name=TASK,
                base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            )
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            sg_stage = _make_stage(workspace, sg_provider, cache, RecordingLimiter())
            with mock.patch.object(sg_provider, "check", check):
                output = sg_stage._check_worker(_make_task(address=""))

            self.assertEqual(_kinds(output), [ResultType.INVALID.value])
            self.assertEqual(check.call_count, 1, "a provider-default region change must re-probe")
            self.assertEqual(sg_stage.cache_metrics()["cache_hits"], 0)


class TestTargetOnlyAndFailOpen(unittest.TestCase):
    """Only the named providers consult the cache; every cache error is a miss."""

    def test_non_target_provider_bypasses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = OllamaProvider(conditions=[])
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(provider, "check", check):
                task = _make_task(name="ollama")
                first = stage._check_worker(task)
                second = stage._check_worker(task)

            self.assertEqual(_kinds(first), [ResultType.INVALID.value])
            self.assertEqual(_kinds(second), [ResultType.INVALID.value])
            self.assertEqual(check.call_count, 2, "other providers keep the pre-cache behaviour")
            self.assertIsNone(_cache_entry(cache, task))
            self.assertEqual(stage.cache_metrics()["cache_hits"], 0)

    def test_corrupt_cache_file_fails_open_to_live(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            path = cache.task_path(TASK)
            os.makedirs(cache.directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(provider, "check", check):
                task = _make_task()
                output = stage._check_worker(task)

            self.assertEqual(_kinds(output), [ResultType.INVALID.value])
            self.assertEqual(check.call_count, 1)
            self.assertIsNotNone(_cache_entry(cache, task), "the live store heals the file")

    def test_store_false_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(cache, "store", return_value=False), mock.patch.object(
                provider, "check", check
            ):
                task = _make_task()
                first = stage._check_worker(task)
                second = stage._check_worker(task)

            self.assertEqual(_kinds(first), [ResultType.INVALID.value])
            self.assertEqual(_kinds(second), [ResultType.INVALID.value])
            self.assertEqual(check.call_count, 2)
            self.assertEqual(stage.cache_metrics()["store_failures"], 2)

    def test_cache_exceptions_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(cache, "lookup", side_effect=OSError("locked")), mock.patch.object(
                cache, "store", side_effect=OSError("locked")
            ), mock.patch.object(provider, "check", check):
                output = stage._check_worker(_make_task())

            self.assertEqual(_kinds(output), [ResultType.INVALID.value])
            self.assertEqual(check.call_count, 1)


class TestSharedInstanceWiring(unittest.TestCase):
    """One VerdictCache per StageResources, shared by every CheckStage worker."""

    def test_resources_construct_exactly_one_cache(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            real_cache = VerdictCache(workspace)
            with mock.patch("stage.base.VerdictCache", return_value=real_cache) as ctor:
                resources = _make_resources(workspace, _make_provider(), None)
            ctor.assert_called_once_with(workspace)
            self.assertIs(resources.verdict_cache, real_cache)

            first = CheckStage(resources, handler=lambda _o: None)
            second = CheckStage(resources, handler=lambda _o: None)
            self.assertIs(first.resources.verdict_cache, second.resources.verdict_cache)
            self.assertIs(first.resources.verdict_cache, real_cache)
            self.assertIsInstance(vars(first.get_stats()).get("cache"), CheckCacheStats)

    def test_all_workers_use_the_same_cache_object(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            seen: list[int] = []
            seen_lock = threading.Lock()
            original_lookup = cache.lookup

            def recording_lookup(**kwargs):
                with seen_lock:
                    seen.append(id(cache))
                return original_lookup(**kwargs)

            with mock.patch.object(cache, "lookup", side_effect=recording_lookup), mock.patch.object(
                provider, "check", check
            ):
                tasks = [_make_task(key=f"{KEY}-{index}") for index in range(8)]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    outputs = list(pool.map(stage._check_worker, tasks))

            self.assertTrue(all(output is not None for output in outputs))
            self.assertEqual(len(seen), len(tasks))
            self.assertEqual(set(seen), {id(cache)})
            self.assertEqual(check.call_count, len(tasks))

    def test_concurrent_misses_leave_a_usable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            task = _make_task()
            with mock.patch.object(provider, "check", check), ThreadPoolExecutor(max_workers=4) as pool:
                outputs = list(pool.map(stage._check_worker, [task for _ in range(8)]))

            self.assertTrue(all(output is not None for output in outputs))
            metrics = stage.cache_metrics()
            self.assertEqual(metrics["live_probes"] + metrics["cache_hits"], 8)
            second_provider = _make_provider()
            second_stage = _make_stage(workspace, second_provider, VerdictCache(workspace))
            fail_closed = mock.Mock(side_effect=AssertionError("cache must be usable"))
            with mock.patch.object(second_provider, "check", fail_closed):
                hit = second_stage._check_worker(task)
            self.assertEqual(_kinds(hit), [ResultType.INVALID.value])
            fail_closed.assert_not_called()

    def test_info_summary_is_single_and_contains_no_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            cache = VerdictCache(workspace)
            provider = _make_provider()
            stage = _make_stage(workspace, provider, cache, RecordingLimiter())
            check = mock.Mock(return_value=CheckResult.fail(ErrorReason.INVALID_KEY))
            with mock.patch.object(provider, "check", check):
                task = _make_task()
                stage._check_worker(task)
                stage._check_worker(task)

            with mock.patch("stage.definition.logger.info") as info:
                stage.stop(timeout=0)
                stage.stop(timeout=0)

            summary_calls = [call for call in info.call_args_list if "verdict cache" in str(call)]
            self.assertEqual(len(summary_calls), 1)
            message = str(summary_calls[0])
            self.assertNotIn(KEY, message)
            self.assertIn("cache_hits=1", message)


if __name__ == "__main__":
    unittest.main()