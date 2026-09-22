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

    def test_invalid_key_goes_to_invalid(self):
        output = _run(CheckResult.fail(ErrorReason.INVALID_KEY))

        kinds = [kind for _provider, kind, _data in output.results]
        self.assertEqual(kinds, [ResultType.INVALID.value])

    def test_valid_key_goes_to_inspect(self):
        output = _run(CheckResult.success())

        self.assertEqual([name for _task, name in output.new_tasks], [PipelineStage.INSPECT.value])
        self.assertEqual([kind for _provider, kind, _data in output.results], [ResultType.VALID.value])


if __name__ == "__main__":
    unittest.main()