#!/usr/bin/env python3

"""
Task manager for coordinating multi-provider pipeline processing.
Creates provider instances from configuration and manages task distribution.
"""

import copy
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Set

import constant
from config import load_config
from config.schemas import Config, TaskConfig
from core.models import Condition, Patterns, ProviderTask, SearchTask, TaskRecoveryInfo
from core.types import IProvider
from provider.base import AIBaseProvider
from provider.registry import ProviderRegistry
from search import client
from stage.base import StageUtils
from stage.factory import TaskFactory
from state.builder import StatusBuilder
from state.models import ProviderStatus, SystemState, SystemStatus
from state.types import TaskDataProvider
from tools.coordinator import get_session, get_token
from tools.logger import get_logger
from tools.utils import get_service_name, handle_exceptions, trim

from .base import LifecycleManager
from .pipeline import Pipeline
from .recovery import TaskRecoveryManager

logger = get_logger("manager")


class CompletionEventManager:
    """Simple completion event manager for task completion notifications"""

    def __init__(self):
        self._listeners: Set[Callable[[], None]] = set()
        self._lock = threading.Lock()
        self._completion_notified = False

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Add completion event listener"""
        with self._lock:
            self._listeners.add(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Remove completion event listener"""
        with self._lock:
            self._listeners.discard(callback)

    @handle_exceptions(default_result=None, log_level="error")
    def notify_completion(self) -> None:
        """Notify all listeners of completion"""
        with self._lock:
            if self._completion_notified:
                return

            success = True
            for callback in self._listeners:
                try:
                    callback()
                except Exception as e:
                    success = False
                    logger.error(f"Error in completion callback: {e}")

            self._completion_notified = success

    @property
    def is_notified(self) -> bool:
        """Check if completion has been notified"""
        with self._lock:
            return self._completion_notified


class ProviderFactory:
    """Factory for creating provider instances from configuration"""

    @staticmethod
    def create_provider(task_config: TaskConfig, conditions: List[Condition]) -> AIBaseProvider:
        """Create provider instance using global registry"""
        provider_type = task_config.provider_type
        name = task_config.name
        api_config = task_config.api
        extras = task_config.extras or {}

        # Copy extras to avoid modifying original object
        kwargs = extras.copy()

        storage = task_config.storage
        if storage and (trim(storage.directory) or trim(storage.plan)):
            kwargs["storage"] = {
                "directory": storage.directory,
                "plan": storage.plan,
            }

        # Add API configuration parameters
        params = {
            "name": name,
            "base_url": api_config.base_url,
            "completion_path": api_config.completion_path,
            "model_path": api_config.model_path,
            "default_model": api_config.default_model,
        }

        # Only add non-empty parameters
        for key, value in params.items():
            if trim(value):
                kwargs[key] = value

        if trim(api_config.auth_key):
            kwargs["auth_key"] = api_config.auth_key

        if isinstance(api_config.extra_headers, dict) and api_config.extra_headers:
            extra_headers = kwargs.get("extra_headers", {})
            if not isinstance(extra_headers, dict):
                extra_headers = {}

            kwargs["extra_headers"] = {**extra_headers, **api_config.extra_headers}

        if trim(api_config.api_version):
            kwargs["api_version"] = api_config.api_version

        if isinstance(api_config.timeout, (int, float)) and api_config.timeout > 0:
            kwargs["timeout"] = api_config.timeout
        if type(api_config.retries) is int and api_config.retries > 0:
            kwargs["retries"] = api_config.retries

        kwargs["use_proxy"] = bool(api_config.use_proxy)

        return ProviderRegistry.create(provider_type, conditions=conditions, **kwargs)


class TaskManager(LifecycleManager, TaskDataProvider):
    """Main task manager for multi-provider coordination and data provision"""

    def __init__(self, config: Config) -> None:
        # Initialize base class
        super().__init__("TaskManager")

        self.config = config
        self.providers: Dict[str, IProvider] = dict()
        self.pipeline: Optional[Pipeline] = None
        self.start_time = time.time()

        # Cache for provider stages to avoid duplicate construction
        self._cached_provider_status = None
        self._config_hash = None

        # Completion event manager
        self.completion_events = CompletionEventManager()

        # Initialize providers
        self._initialize_providers()

        # Create pipeline
        self._create_pipeline()

        logger.info(f"Initialized task manager with {len(self.providers)} providers")

    def _get_provider_statuses(self) -> List[ProviderStatus]:
        """Get provider status information with caching to avoid duplicate construction"""
        # Create a simple hash of the configuration to detect changes
        key = str(
            [
                (
                    task.name,
                    task.enabled,
                    task.stages.search,
                    task.stages.gather,
                    task.stages.check,
                    task.stages.inspect,
                )
                for task in self.config.tasks
            ]
        )
        current_hash = hash(key)

        # Return cached result if configuration hasn't changed
        if self._cached_provider_status is not None and self._config_hash == current_hash:
            return self._cached_provider_status

        # Rebuild cache
        provider_statuses: List[ProviderStatus] = []
        for task in self.config.tasks:
            if task.enabled and task.name in self.providers:
                provider_status = ProviderStatus(
                    name=task.name,
                    enabled=task.enabled,
                    searchable=task.stages.search,
                    gatherable=task.stages.gather,
                    checkable=task.stages.check,
                    inspectable=task.stages.inspect,
                )
                provider_statuses.append(provider_status)

        # Update cache
        self._cached_provider_status = provider_statuses
        self._config_hash = current_hash

        return provider_statuses

    def _initialize_providers(self) -> None:
        """Initialize all enabled providers from configuration"""
        for task_config in self.config.tasks:
            if not task_config.enabled:
                logger.debug(f"Skipping disabled provider: {task_config.name}")
                continue

            try:
                # Use conditions directly from config (already parsed and validated)
                conditions = [c for c in task_config.conditions if c.is_valid()]

                if not conditions:
                    logger.warning(f"No valid conditions for provider {task_config.name}, skipping")
                    continue

                # Create provider instance
                provider = ProviderFactory.create_provider(task_config, conditions)
                self.providers[task_config.name] = provider

                # Log provider creation with stage information
                enabled_stages = StageUtils.get_enabled(task_config)

                logger.info(
                    f"Created provider: {task_config.name} ({task_config.provider_type}) "
                    f"with {len(conditions)} conditions, stages: [{', '.join(enabled_stages)}]"
                )

            except Exception as e:
                logger.error(f"Failed to create provider {task_config.name}: {e}")
                continue

        if not self.providers:
            raise ValueError("No valid providers configured")

    def _create_pipeline(self) -> None:
        """Create pipeline with all components"""
        # Add provider-specific rate limits
        rate_limits = self.config.ratelimits.copy()

        for task_config in self.config.tasks:
            if task_config.enabled:
                service_name = get_service_name(task_config.name)
                rate_limits[service_name] = task_config.rate_limit

        # Create runtime config with provider rate limits (avoid mutating original config)
        runtime_config = copy.deepcopy(self.config)
        runtime_config.ratelimits = rate_limits

        self.pipeline = Pipeline(runtime_config, self.providers)

        logger.info("Created pipeline with all providers")

    def _on_start(self) -> None:
        """Start the task manager and pipeline"""
        # 1. Start pipeline (creates ResultManager without backup)
        self.pipeline.start()

        # 2. Recover queue tasks
        recoverd_tasks = self.pipeline.queue_manager.load_all_queues()

        # 3. Filter recovered tasks by stage configuration
        undo_tasks = self._filter_recovery(recoverd_tasks)

        # 4. Recover result file tasks (material.txt, links.txt) and invalid keys
        old_tasks = self.pipeline.result_manager.recover_all_tasks()

        # 5. Add recovered tasks to appropriate queues
        recovery_info = TaskRecoveryInfo(
            queue_tasks=undo_tasks,
            result_tasks=old_tasks,
            total_queue_tasks=sum(len(tasks) for tasks in undo_tasks.values()),
            total_result_tasks=old_tasks.total_check_tasks() + old_tasks.total_acquisition_tasks(),
        )
        self._add_recovered_tasks(recovery_info)

        # 6. Backup existing files (after recovery is complete)
        self.pipeline.result_manager.backup_all_existing_files()

        # 6b. Previously-valid keys were re-queued for check during recovery
        # (see TaskRecoveryManager._recover_provider_tasks), so they are
        # re-validated with the current provider logic instead of being
        # re-seeded blindly. This prevents stale valid entries (e.g. keys
        # accepted before 402/no-quota filtering existed) from surviving.
        # NOTE: no reseed_valid_keys call here.

        # 7. Start queue manager periodic save, after recovery to avoid file conflicts
        self.pipeline.queue_manager.start_periodic_save(self.pipeline.stages)

        # 8. Add initial search tasks
        initial_tasks = self._create_initial_tasks()
        if initial_tasks:
            self.pipeline.add_initial_tasks(initial_tasks)

        # Log recovery and startup info
        logger.info(
            f"Started task manager: {recovery_info.total_queue_tasks} queue tasks, {recovery_info.total_result_tasks} result tasks, {len(initial_tasks)} initial tasks"
        )

    def _on_stop(self) -> None:
        """Stop the task manager gracefully"""
        if self.pipeline:
            self.pipeline.stop()

        logger.info("Stopped task manager")

    def add_completion_listener(self, callback: Callable[[], None]) -> None:
        """Add completion event listener"""
        self.completion_events.add_listener(callback)

    def remove_completion_listener(self, callback: Callable[[], None]) -> None:
        """Remove completion event listener"""
        self.completion_events.remove_listener(callback)

    def is_finished(self) -> bool:
        """Check if task manager is finished processing all tasks"""
        # Check base class conditions first
        if super().is_finished():
            return True

        if not self.pipeline:
            return True

        finished = self.pipeline.is_finished()

        # Send completion event once when finished
        if finished and not self.completion_events.is_notified:
            self.completion_events.notify_completion()
            logger.info("TaskManager finished, notified other components")

        return finished

    def stats(self) -> SystemStatus:
        """Get current task manager statistics using enhanced StatusBuilder

        Implements TaskDataProvider.stats() interface method.
        """

        # Use StatusBuilder for clean, maintainable status construction
        builder = StatusBuilder()

        # Set basic system information
        runtime = time.time() - self.start_time if self.start_time > 0 else 0
        state = SystemState.RUNNING if self.running else SystemState.STOPPED
        builder.with_basic_info(runtime, state)

        # Set providers information
        builder.with_providers_info(self.providers)

        # Set pipeline statistics if available
        if self.pipeline:
            builder.with_pipeline_stats(self.pipeline)

            # Set result statistics using enhanced aggregator
            if self.pipeline.result_manager:
                result_stats = self.pipeline.result_manager.get_all_stats()
                builder.with_result_stats(result_stats)

        # Set provider stage configurations
        provider_status = self._get_provider_statuses()
        builder.with_provider_status(provider_status)

        # Set additional compatibility data
        builder.with_additional_data(github_stats=client.get_github_stats())
        return builder.build()

    def _create_initial_tasks(self) -> List[SearchTask]:
        """Create initial search tasks for all providers"""
        tasks = []

        for task_config in self.config.tasks:
            if not task_config.enabled:
                continue

            if not task_config.stages.search:
                logger.info(
                    f"Skipping initial search tasks for provider {task_config.name} due to search stage disabled"
                )
                continue

            # Check if we have GitHub credentials
            try:
                # Try to get either token or session to verify availability
                has_token = get_token() is not None
                has_session = get_session() is not None
                if (
                    not has_token
                    and not has_session
                    or (task_config.use_api and not has_token)
                    or (not task_config.use_api and not has_session)
                ):
                    logger.warning(
                        f"Skipping search for provider {task_config.name} as no github token or session is provided"
                    )
                    continue
            except Exception:
                logger.warning(
                    f"Skipping search for provider {task_config.name} as no github token or session is provided"
                )
                continue

            provider = self.providers.get(task_config.name)
            if not provider:
                continue

            search_types = getattr(task_config, "search_types", None) or ["code"]
            for condition in provider.conditions:
                # Fan out one search task per configured search type (default: code only)
                for search_type in search_types:
                    # Web HTML path only supports code search
                    if search_type != "code" and not task_config.use_api:
                        logger.warning(
                            f"Skipping search_type={search_type} for provider {task_config.name}: "
                            "requires use_api=true"
                        )
                        continue
                    task = TaskFactory.create_search_task(
                        provider=task_config.name,
                        query=condition.query or condition.patterns.key_pattern,
                        regex=condition.patterns.key_pattern,
                        page=1,
                        use_api=task_config.use_api,
                        max_pages=task_config.max_pages,
                        search_type=search_type,
                        address_pattern=condition.patterns.address_pattern,
                        endpoint_pattern=condition.patterns.endpoint_pattern,
                        model_pattern=condition.patterns.model_pattern,
                    )
                    tasks.append(task)

        # Log summary of initial task creation
        if tasks:
            providers_with_tasks = set(task.provider for task in tasks)
            logger.info(
                f"Created {len(tasks)} initial search tasks for {len(providers_with_tasks)} providers: {', '.join(providers_with_tasks)}"
            )
        else:
            logger.info(
                "No initial search tasks created - all providers have search stage disabled or missing credentials"
            )

        return tasks

    def _add_recovered_tasks(self, recovery_info: TaskRecoveryInfo) -> None:
        """Add recovered tasks using enhanced TaskRecoveryStrategy"""

        # Use TaskRecoveryStrategy for type-safe, maintainable task recovery
        recovery_strategy = TaskRecoveryManager(self.pipeline, self.providers)

        # Recover queue tasks using enhanced strategy
        recovery_strategy.recover_queue_tasks(recovery_info.queue_tasks)

        # Recover result tasks using enhanced strategy
        recovery_strategy.recover_result_tasks(recovery_info.result_tasks)

    def _get_provider_patterns(self, provider: AIBaseProvider) -> Patterns:
        """Extract patterns from provider conditions"""
        # Use first condition's patterns if available
        if provider.conditions:
            return provider.conditions[0].patterns

        return Patterns()

    def _filter_recovery(self, recovered: Dict[str, List[ProviderTask]]) -> Dict[str, List[ProviderTask]]:
        """Filter recovered tasks based on stage configuration"""
        filtered = {}

        for stage, tasks in recovered.items():
            valid_tasks = []
            for task in tasks:
                if not task or task.provider not in self.providers:
                    continue

                config = self._get_config(task.provider)
                if config and StageUtils.check(config, stage):
                    valid_tasks.append(task)
                else:
                    logger.debug(f"Skipping recovery of {stage} task for provider {task.provider} - stage disabled")

            if valid_tasks:
                filtered[stage] = valid_tasks

        return filtered

    def _get_config(self, provider: str) -> Optional[TaskConfig]:
        """Get task config for provider"""
        return next((t for t in self.config.tasks if t.name == provider), None)


def create_task_manager(config_file: str = constant.DEFAULT_CONFIG_FILE) -> TaskManager:
    """Factory function to create task manager from configuration"""
    config = load_config(config_file)
    if not config:
        return None

    return TaskManager(config)


if __name__ == "__main__":
    # Test task manager creation
    try:
        # Create task manager
        manager = create_task_manager()

        logger.info(f"Created task manager with providers: {list(manager.providers.keys())}")

        # Test provider creation
        for name, provider in manager.providers.items():
            logger.info(f"  {name}: {provider.__class__.__name__} with {len(provider.conditions)} conditions")

        # Test stats
        stats = manager.get_stats()
        logger.info(f"Manager stats: {stats.providers}")

        logger.info("Task manager test completed!")

    except Exception as e:
        logger.error(f"Task manager test failed: {e}")
        traceback.print_exc()
