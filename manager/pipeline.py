#!/usr/bin/env python3

"""
Dynamic pipeline system for asynchronous multi-provider task processing.
Implements producer-consumer pattern with configurable worker threads and dynamic stage management.
"""

import time
from typing import Dict, List, Optional

from config.schemas import Config
from core.auth import configure_auth, get_auth_provider
from core.enums import PipelineStage, SystemState
from core.metrics import PipelineStatus
from core.models import ProviderTask
from core.types import IPipelineStats, IProvider
from search import client
from stage.base import BasePipelineStage, StageOutput, StageResources, StageUtils
from stage.registry import StageRegistryMixin
from stage.resolver import DependencyResolver
from storage.persistence import MultiResultManager
from tools.coordinator import get_session, get_token, get_user_agent
from tools.logger import get_logger
from tools.ratelimit import RateLimiter

from .base import LifecycleManager
from .queue import QueueManager

logger = get_logger("manager")


class Pipeline(IPipelineStats, StageRegistryMixin, LifecycleManager):
    """Dynamic pipeline coordinator with registry-based stage management

    Inherits from PipelineBase to provide type-safe statistics interface,
    from StageRegistryMixin for stage management capabilities,
    and from LifecycleManager for lifecycle management.
    """

    def __init__(self, config: Config, providers: Dict[str, IProvider]):
        # Initialize base classes
        LifecycleManager.__init__(self, "Pipeline")

        self.config = config
        self.providers: Dict[str, IProvider] = providers

        # Configure authentication service
        configure_auth(session_provider=get_session, token_provider=get_token, user_agent_provider=get_user_agent)

        # Create shared components
        self.result_manager = MultiResultManager(
            workspace=config.global_config.workspace,
            providers=providers,
            batch_size=config.persistence.batch_size,
            save_interval=config.persistence.save_interval,
            simple=config.persistence.simple,
            shutdown_timeout=float(config.persistence.shutdown_timeout),
        )

        self.rate_limiter = RateLimiter(config.ratelimits)

        # Configure the shared HTTP opener before any stage starts making network requests
        client.set_proxy(config.global_config.proxy)

        # Initialize GitHub client rate limiter
        client.init_github_client(config.ratelimits)

        self.queue_manager = QueueManager(
            workspace=config.global_config.workspace,
            save_interval=config.persistence.queue_interval,
            shutdown_timeout=float(config.persistence.shutdown_timeout),
        )

        # Start periodic snapshots for results
        if not config.persistence.simple:
            try:
                self.result_manager.start_periodic_snapshots(config.persistence.snapshot_interval)
            except Exception as e:
                logger.error(f"Failed to start periodic snapshots: {e}")
        else:
            logger.debug("Skipping periodic snapshots in simple mode")

        # Store task configs for stage checking (must be before _create_stages)
        self.task_configs = {task.name: task for task in config.tasks if task.enabled}

        # Dynamic stage management
        self.stages: Dict[str, BasePipelineStage] = {}
        self.resolver = DependencyResolver(self.registry)

        # Create pipeline stages dynamically
        self._create_stages()

        # Cache dependency resolution results
        self._order_cache = None
        self._init_order_cache()

        # Statistics and completion tracking
        self.start_time = time.time()
        self.initial_tasks_count = 0

        logger.info(f"Initialized dynamic pipeline with {len(self.stages)} stages: {list(self.stages.keys())}")

    def _aggregate_stages(self) -> List[str]:
        """Aggregate stage requirements from all enabled tasks"""
        requested = set()

        # Collect all stages requested by enabled tasks
        for task in self.config.tasks:
            if task.enabled:
                enabled = StageUtils.get_enabled(task)
                requested.update(enabled)

                logger.debug(f"  {task.name}: [{', '.join(enabled)}]")

        result = list(requested)
        logger.info(f"Aggregated stages to create: {result}")
        return result

    def _create_stages(self) -> None:
        """Create pipeline stages dynamically with hybrid architecture"""

        # Get requested stages from configuration
        requested_stages = self._aggregate_stages()

        if not requested_stages:
            logger.warning("No stages requested, pipeline will be empty")
            return

        # Resolve stage creation order
        try:
            ordered_stages = self.resolver.resolve_order(requested_stages)
        except Exception as e:
            logger.error(f"Failed to resolve stage dependencies: {e}")
            raise

        # Create shared resources for dependency injection
        resources = StageResources(
            limiter=self.rate_limiter,
            providers=self.providers,
            config=self.config,
            task_configs=self.task_configs,
            auth=get_auth_provider(),
        )

        # Create stages in dependency order
        thread_config = self.config.pipeline.threads
        queue_config = self.config.pipeline.queue_sizes

        for name in ordered_stages:
            definition = self.get_stage_def(name)
            if not definition:
                logger.error(f"Stage definition not found: {name}")
                continue

            try:
                # Create stage instance with hybrid architecture
                stage = definition.stage_class(
                    resources=resources,
                    handler=self._handle_stage_output,
                    thread_count=max(thread_config.get(name, 1), 1),
                    queue_size=max(queue_config.get(name, 1000), 1),
                    max_retries=self.config.global_config.max_retries_requeued,
                )

                self.stages[name] = stage

            except Exception as e:
                logger.error(f"Failed to create stage {name}: {e}")
                raise

    def _on_start(self) -> None:
        """Start all pipeline stages"""
        if not self.stages:
            logger.warning("No stages to start")
            return

        # Start stages in dependency order
        ordered_stages = self.get_order()

        for stage_name in ordered_stages:
            stage = self.stages.get(stage_name)
            if stage:
                stage.start()

        logger.info(f"Started {len(self.stages)} pipeline stages")

    def _on_stop(self) -> None:
        """Stop all pipeline stages"""
        if not self.stages:
            return

        # Stop stages in reverse dependency order
        ordered_stages = self.get_order()
        stage_timeout = 30.0 / len(self.stages) if self.stages else 30.0

        for stage_name in reversed(ordered_stages):
            stage = self.stages.get(stage_name)
            if stage:
                stage.stop(stage_timeout)

        # Stop managers
        self.queue_manager.save_all_queues(self.stages)
        self.queue_manager.stop()
        self.result_manager.stop_all()

        logger.info("Stopped all pipeline stages")

    def is_finished(self) -> bool:
        """Check if pipeline is finished and manage stage states"""
        if not self.stages:
            return True

        ordered_stages = self.get_order()
        all_finished = True

        # Check each stage and manage accepting state
        for stage_name in ordered_stages:
            stage = self.stages.get(stage_name)
            if not stage:
                continue

            # Stop accepting if stage can finish
            if stage.accepting and self._can_stage_stop_accepting(stage_name):
                stage.stop_accepting()
                logger.info(f"[{stage_name}] stopped accepting new tasks")

            # Check if stage is finished
            if not stage.is_finished():
                all_finished = False

        return all_finished

    def get_all_stats(self) -> PipelineStatus:
        """Get statistics for all stages"""
        return self._get_pipeline_status()

    def get_dynamic_stats(self) -> PipelineStatus:
        """Get dynamic statistics for all stages"""
        return self._get_pipeline_status()

    def _get_pipeline_status(self) -> PipelineStatus:
        """Get pipeline status as PipelineStatus object"""
        stage_status = {}

        # Collect stats from all active stages
        for stage_name, stage in self.stages.items():
            stage_status[stage_name] = stage.get_stats()

        pipeline_status = PipelineStatus(
            state=SystemState.RUNNING if self.stages else SystemState.STOPPED,
            active=len([s for s in self.stages.values() if s.running]),
            total=len(self.stages),
            stages=stage_status,
            runtime=time.time() - self.start_time,
        )

        return pipeline_status

    def add_initial_tasks(self, initial_tasks: List[ProviderTask]) -> None:
        """Add initial search tasks to pipeline"""
        self.initial_tasks_count = len(initial_tasks)

        search_stage = self.stages.get(PipelineStage.SEARCH.value)
        if search_stage:
            for task in initial_tasks:
                search_stage.put_task(task)
        else:
            logger.warning("Search stage not created, cannot add initial tasks")

        logger.info(f"Added {len(initial_tasks)} initial tasks to pipeline")

    def get_stage(self, name: str) -> Optional[BasePipelineStage]:
        """Get stage by name"""
        return self.stages.get(name)

    def _init_order_cache(self) -> None:
        """Initialize and cache stage order"""
        if self.stages:
            self._order_cache = self.resolver.resolve_order(list(self.stages.keys()))
            logger.debug(f"Cached stage order: {self._order_cache}")

    def get_order(self) -> List[str]:
        """Get cached stage order"""
        if self._order_cache is None:
            self._init_order_cache()
        return self._order_cache.copy() if self._order_cache else []

    def _handle_stage_output(self, output: StageOutput) -> None:
        """Handle pure functional stage output - core orchestration logic"""

        # Save results
        for provider, result_type, data in output.results:
            self.result_manager.add_result(provider, result_type, data)

        # Save links
        for provider, links in output.links:
            self.result_manager.add_links(provider, links)

        # Save models
        for provider, key, models in output.models:
            self.result_manager.add_models(provider, key, models)

        # Route new tasks
        for task, target_stage in output.new_tasks:
            config = self.task_configs.get(task.provider)
            if config and StageUtils.check(config, target_stage):
                stage = self.stages.get(target_stage)
                if stage:
                    stage.put_task(task)
                else:
                    logger.warning(f"Target stage {target_stage} not found for task {task.provider}")
            else:
                logger.debug(f"Stage {target_stage} disabled for {task.provider}, skipping task")

    def _can_stage_stop_accepting(self, stage_name: str) -> bool:
        """Check if a stage can stop accepting new tasks based on precise conditions"""
        stage = self.stages.get(stage_name)
        if not stage:
            return False

        # Stage can stop accepting tasks ONLY when:
        # 1. Own task queue is empty
        # 2. No workers are actively processing tasks
        # 3. All upstream producers are finished (or no upstream producers exist)

        # Check condition 1: Own task queue is empty
        if not stage.queue.empty():
            return False

        # Check condition 2: No workers are actively processing tasks
        if stage.active_workers > 0:
            return False

        # Check condition 3: All upstream producers are finished
        # Get all stages that can potentially send tasks to this stage
        upstream_stages = []
        for other_stage_name in self.stages.keys():
            definition = self.get_stage_def(other_stage_name)
            if definition and stage_name in definition.produces_for:
                upstream_stages.append(other_stage_name)

        # If no upstream stages, consider upstream as finished
        if not upstream_stages:
            return True

        # Check if all upstream stages are finished
        for upstream_name in upstream_stages:
            upstream_stage = self.stages.get(upstream_name)
            if upstream_stage:
                # Upstream stage must be finished (queue empty + no active workers)
                if not upstream_stage.queue.empty():
                    return False
                if upstream_stage.active_workers > 0:
                    return False
                # Also check if upstream is still accepting (could generate more tasks)
                if upstream_stage.accepting:
                    return False

        return True
