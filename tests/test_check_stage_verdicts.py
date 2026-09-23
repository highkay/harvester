#!/usr/bin/env python3

"""CheckStage verdict routing: retryable failures are not key verdicts.

Measured 2026-09-22: ollama.com through the scan's socks exits answers TLS
EOF/timeouts often enough (8/25 first probes, container-direct) that the old
catch-all routing burned live keys into ``invalid-keys.txt`` forever. Retryable
reasons (network / timeout / 5xx / rate limit) now land in the recoverable
``wait-check-keys.txt`` bucket.
"""

from __future__ import annotations

import unittest
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.enums import ErrorReason, PipelineStage, ResultType
from core.models import CheckResult, CheckTask, Service
from provider.ollama import OllamaProvider
from stage.base import StageResources
from stage.definition import CheckStage


def _run(result: CheckResult):
    provider = OllamaProvider(conditions=[])
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={"ollama": provider},
        config=mock.MagicMock(),
        task_configs={"ollama": TaskConfig(name="ollama", provider_type="ollama", stages=StageConfig())},
        auth=mock.MagicMock(),
    )
    stage = CheckStage(resources, handler=lambda _output: None)
    with mock.patch.object(provider, "check", return_value=result):
        output = stage._check_worker(CheckTask(provider="ollama", service=Service(key="candidate-key")))

    assert output is not None
    return output


class TestCheckStageVerdictRouting(unittest.TestCase):
    def test_retryable_failures_go_to_wait_check(self):
        reasons = (
            ErrorReason.NETWORK_ERROR,
            ErrorReason.TIMEOUT,
            ErrorReason.SERVER_ERROR,
            ErrorReason.RATE_LIMITED,
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                output = _run(CheckResult.fail(reason))

                kinds = [kind for _provider, kind, _data in output.results]
                self.assertEqual(kinds, [ResultType.WAIT_CHECK.value])

    def test_bad_request_goes_to_wait_check(self):
        """BAD_REQUEST means the probe/request was rejected, not the credential
        (provider/base.py HTTP 400 mapping, qwen non-Arrearage 400, deepseek
        400, opencode 401-ModelError) — it must land in the recoverable
        wait-check bucket, never in the permanent invalid discard."""
        output = _run(CheckResult.fail(ErrorReason.BAD_REQUEST))

        kinds = [kind for _provider, kind, _data in output.results]
        self.assertEqual(kinds, [ResultType.WAIT_CHECK.value])

    def test_no_model_and_no_access_go_to_wait_check(self):
        for reason in (ErrorReason.NO_MODEL, ErrorReason.NO_ACCESS):
            with self.subTest(reason=reason):
                output = _run(CheckResult.fail(reason))

                kinds = [kind for _provider, kind, _data in output.results]
                self.assertEqual(kinds, [ResultType.WAIT_CHECK.value])

    def test_no_quota_goes_to_no_quota(self):
        output = _run(CheckResult.fail(ErrorReason.NO_QUOTA))

        kinds = [kind for _provider, kind, _data in output.results]
        self.assertEqual(kinds, [ResultType.NO_QUOTA.value])

    def test_unknown_goes_to_invalid(self):
        """UNKNOWN stays a permanent discard: the response was parsed but the
        verdict is genuinely unknowable — it is not a retryable transport state."""
        output = _run(CheckResult.fail(ErrorReason.UNKNOWN))

        kinds = [kind for _provider, kind, _data in output.results]
        self.assertEqual(kinds, [ResultType.INVALID.value])

    def test_invalid_key_goes_to_invalid(self):
        output = _run(CheckResult.fail(ErrorReason.INVALID_KEY))

        kinds = [kind for _provider, kind, _data in output.results]
        self.assertEqual(kinds, [ResultType.INVALID.value])

    def test_valid_key_goes_to_inspect(self):
        output = _run(CheckResult.success())

        self.assertEqual([name for _task, name in output.new_tasks], [PipelineStage.INSPECT.value])
        self.assertEqual([kind for _provider, kind, _data in output.results], [ResultType.VALID.value])


class TestCheckStageLimiterFeedback(unittest.TestCase):
    """CheckStage must report the REAL outcome to the adaptive limiter.

    A retryable verdict (RATE_LIMITED / TIMEOUT / NETWORK_ERROR / 5xx) is a
    transport signal, not a key verdict: it has to reach ``adjust_rate``'s
    failure path. Reporting True unconditionally (before 2026-09-23) made that
    path unreachable, so the bucket could only accelerate (x1.1 per 10
    successes, capped at 2x base) and held the provider at the 1-2 req/s that
    trips tavily's per-IP bulk-validation block (a 1276-key sweep failed
    1249/1251 at 6 s pacing; the validated safe pace is >=5-6 s/key).
    """

    BASE_RATE = 5.0

    def _bucket_after(self, result: CheckResult, calls: int = 3):
        from core.models import RateLimitConfig
        from tools.ratelimit import RateLimiter
        from tools.utils import get_service_name

        service = get_service_name("ollama")
        limiter = RateLimiter({service: RateLimitConfig(base_rate=self.BASE_RATE, burst_limit=10, adaptive=True)})
        provider = OllamaProvider(conditions=[])
        resources = StageResources(
            limiter=limiter,
            providers={"ollama": provider},
            config=mock.MagicMock(),
            task_configs={"ollama": TaskConfig(name="ollama", provider_type="ollama", stages=StageConfig())},
            auth=mock.MagicMock(),
        )
        stage = CheckStage(resources, handler=lambda _output: None)
        with mock.patch.object(provider, "check", return_value=result):
            for _ in range(calls):
                stage._check_worker(CheckTask(provider="ollama", service=Service(key="candidate-key")))

        return limiter.buckets[service]

    def test_retryable_verdicts_engage_the_backoff(self):
        bucket = self._bucket_after(CheckResult.fail(ErrorReason.RATE_LIMITED))

        self.assertAlmostEqual(bucket.rate, self.BASE_RATE / 2)

    def test_key_verdicts_do_not_back_off(self):
        for result in (CheckResult.success(), CheckResult.fail(ErrorReason.INVALID_KEY)):
            with self.subTest(reason=result.error_reason):
                bucket = self._bucket_after(result)

                self.assertAlmostEqual(bucket.rate, self.BASE_RATE)


if __name__ == "__main__":
    unittest.main()