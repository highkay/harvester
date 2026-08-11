#!/usr/bin/env python3

"""
Task Recovery Strategy - Enhanced Task Recovery Management

This module provides enhanced task recovery functionality using type-safe
stage management and configuration-driven approach.
"""

from typing import Dict, List, Optional, Set

from core.enums import PipelineStage
from core.models import AllRecoveredTasks, ProviderTask, RecoveredTasks, Service
from core.types import IProvider
from stage.base import StageUtils
from stage.factory import TaskFactory
from tools.logger import get_logger

from .pipeline import Pipeline

logger = get_logger("manager")


class TaskRecoveryManager:
    """Enhanced task recovery strategy using type-safe stage management"""

    def __init__(self, pipeline: "Pipeline", providers: Dict[str, IProvider]):
        """Initialize recovery strategy

        Args:
            pipeline: Pipeline instance
            providers: Dictionary of provider instances
        """
        self.pipeline = pipeline
        self.providers = providers

    def recover_queue_tasks(self, queue_tasks: Dict[str, List[ProviderTask]]) -> None:
        """Recover queue tasks using stage registry

        Args:
            queue_tasks: Dictionary mapping stage names to task lists
        """
        if not queue_tasks:
            return

        logger.info("Starting queue task recovery")

        for stage_name, tasks in queue_tasks.items():
            if not tasks:
                continue

            # Validate stage name using enum
            stage_enum = self._get_stage_enum(stage_name)
            if not stage_enum:
                logger.warning(f"Unknown stage name: {stage_name}")
                continue

            # Get stage instance from pipeline
            stage = self.pipeline.get_stage(stage_enum.value)
            if not stage:
                logger.warning(f"Stage not found: {stage_name}")
                continue

            # Recover tasks to stage
            for task in tasks:
                try:
                    stage.put_task(task)
                    logger.debug(f"Recovered task to {stage_name}: {task}")
                except Exception as e:
                    logger.error(f"Failed to recover task to {stage_name}: {e}")

        logger.info(f"Queue task recovery completed for {len(queue_tasks)} stages")

    def recover_result_tasks(self, result_tasks: AllRecoveredTasks) -> None:
        """Recover result tasks with stage enum validation

        Args:
            result_tasks: All recovered tasks data
        """
        if not result_tasks or not result_tasks.providers:
            return

        logger.info("Starting result task recovery")

        for provider_name, provider_tasks in result_tasks.providers.items():
            if provider_name not in self.providers:
                logger.warning(f"Provider not found: {provider_name}")
                continue

            self._recover_provider_tasks(provider_name, provider_tasks)

        logger.info(f"Result task recovery completed for {len(result_tasks.providers)} providers")

    def _recover_provider_tasks(self, name: str, tasks: RecoveredTasks) -> None:
        """Recover tasks for specific provider

        Args:
            name: Name of the provider
            tasks: Recovered tasks for the provider
        """
        config = self.pipeline.task_configs.get(name)
        if not config:
            logger.warning(f"No config found for provider: {name}")
            return

        # Recover check tasks (Service objects can be used directly).
        # Previously-valid keys are re-checked instead of being trusted:
        # stale results (e.g. keys validated before 402/no-quota filtering
        # existed) must not be re-seeded as valid without re-validation.
        if StageUtils.check(config, PipelineStage.CHECK):
            self._recover_check_tasks(name, tasks.check + tasks.valid, tasks.invalid)

        # Recover acquisition tasks (URLs need to be converted to AcquisitionTask objects)
        if StageUtils.check(config, PipelineStage.GATHER):
            self._recover_acquisition_tasks(name, tasks.acquisition)

    def _recover_check_tasks(self, name: str, check_tasks: List[Service], invalid_keys: Set[Service]) -> None:
        """Recover check tasks for provider

        Args:
            name: Name of the provider
            check_tasks: List of services to check
            invalid_keys: Set of invalid services
        """
        stage_instance = self.pipeline.get_stage(PipelineStage.CHECK.value)
        if not stage_instance:
            logger.warning(f"Check stage not found for {name}")
            return

        provider = self.providers.get(name)
        if not provider:
            logger.warning(f"Provider not found: {name}")
            return

        recovered_count = 0

        # Recover valid check tasks
        for service in check_tasks:
            if not service or (invalid_keys and service in invalid_keys):
                continue

            try:
                check_task = TaskFactory.create_check_task(name, service)
                stage_instance.put_task(check_task)
                recovered_count += 1

            except Exception as e:
                logger.error(f"Failed to create check task for {name}: {e}")

        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} check tasks for {name}")

    def _recover_acquisition_tasks(self, name: str, acquisition_tasks: List[str]) -> None:
        """Recover acquisition tasks for provider

        Args:
            name: Name of the provider
            acquisition_tasks: List of URLs to acquire
        """
        stage_instance = self.pipeline.get_stage(PipelineStage.GATHER.value)
        if not stage_instance:
            logger.warning(f"Acquisition stage not found for {name}")
            return

        provider = self.providers.get(name)
        if not provider:
            logger.warning(f"Provider not found: {name}")
            return

        patterns = provider.get_patterns()
        recovered_count = 0

        for url in acquisition_tasks:
            try:
                # Create acquisition task with patterns
                acquisition_task = TaskFactory.create_acquisition_task(name, url, patterns)
                stage_instance.put_task(acquisition_task)
                recovered_count += 1

            except Exception as e:
                logger.error(f"Failed to create acquisition task for {name}: {e}")

        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} acquisition tasks for {name}")

    def _get_stage_enum(self, stage_name: str) -> Optional[PipelineStage]:
        """Get stage enum from stage name

        Args:
            stage_name: Name of the stage

        Returns:
            PipelineStage enum or None if not found
        """
        try:
            return PipelineStage(stage_name)
        except ValueError:
            return None
