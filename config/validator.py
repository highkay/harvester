#!/usr/bin/env python3

"""
Configuration Validator

This module provides comprehensive validation for configuration objects.
It ensures configuration completeness, correctness, and consistency.

Key Features:
- Type validation
- Business rule validation
- Dependency checking
- Error reporting
"""

from typing import List

from .schemas import Config, LoadBalanceStrategy, TaskConfig


class ConfigValidator:
    """Configuration validator with comprehensive checks"""

    def __init__(self):
        """Initialize configuration validator"""
        self.errors: List[str] = []

    def validate(self, config: Config) -> None:
        """Validate complete configuration

        Args:
            config: Configuration object to validate

        Raises:
            ValueError: If validation fails
        """
        self.errors.clear()

        # Validate global configuration
        self._validate_global_config(config)

        # Validate pipeline configuration
        self._validate_pipeline_config(config)

        # Validate monitoring configuration
        self._validate_monitoring_config(config)

        # Validate tasks configuration
        self._validate_tasks_config(config)

        # Validate worker manager configuration
        self._validate_worker_manager_config(config)

        # Validate rate limits
        self._validate_rate_limits(config)

        # Validate display configuration
        self._validate_display_config(config)

        # Check for validation errors
        if self.errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"- {error}" for error in self.errors)
            raise ValueError(error_msg)

    def _validate_global_config(self, config: Config) -> None:
        """Validate global configuration section

        Args:
            config: Configuration object
        """
        global_config = config.global_config

        # Validate workspace
        if not global_config.workspace:
            self.errors.append("Global workspace cannot be empty")

        # Validate GitHub credentials
        credentials = global_config.github_credentials
        if not credentials.sessions and not credentials.tokens:
            self.errors.append("At least one GitHub session or token must be provided")

        # Validate load balance strategy
        if credentials.strategy not in LoadBalanceStrategy:
            self.errors.append(f"Invalid load balance strategy: {credentials.strategy}")

        # Validate user agents
        if not global_config.user_agents:
            self.errors.append("At least one user agent must be provided")

        # Validate max retries
        if global_config.max_retries_requeued < 0:
            self.errors.append("Max retries requeued must be non-negative")

        # Validate GitHub transport enhancements
        transport = getattr(global_config, "github_transport", None)
        if transport is not None:
            edge = transport.edge_pool
            if edge.refresh_interval < 60:
                self.errors.append("github_transport.edge_pool.refresh_interval must be >= 60")
            if edge.max_edges < 1:
                self.errors.append("github_transport.edge_pool.max_edges must be >= 1")
            if edge.source not in {"auto", "http", "gx", "builtin", "disabled"}:
                self.errors.append(
                    "github_transport.edge_pool.source must be one of: auto, http, gx, builtin, disabled"
                )
            cache = transport.cache
            if cache.ttl_search < 0 or cache.ttl_core < 0:
                self.errors.append("github_transport.cache TTL values must be non-negative")
            if cache.max_entries < 1:
                self.errors.append("github_transport.cache.max_entries must be >= 1")
            doh = transport.doh
            if doh.enabled and not doh.endpoints:
                self.errors.append("github_transport.doh.endpoints cannot be empty when DoH is enabled")

    def _validate_pipeline_config(self, config: Config) -> None:
        """Validate pipeline configuration section

        Args:
            config: Configuration object
        """
        pipeline = config.pipeline

        required_stages = {"search", "gather", "check", "inspect"}
        for stage in required_stages:
            # Validate thread counts
            if stage not in pipeline.threads:
                self.errors.append(f"Missing thread count for stage: {stage}")
            elif pipeline.threads[stage] <= 0:
                self.errors.append(f"Thread count for {stage} must be positive")

            # Validate queue sizes
            if stage not in pipeline.queue_sizes:
                self.errors.append(f"Missing queue size for stage: {stage}")
            elif pipeline.queue_sizes[stage] <= 0:
                self.errors.append(f"Queue size for {stage} must be positive")

    def _validate_monitoring_config(self, config: Config) -> None:
        """Validate monitoring configuration section

        Args:
            config: Configuration object
        """
        monitoring = config.monitoring

        if monitoring.update_interval <= 0:
            self.errors.append("Monitoring update interval must be positive")

        if not (0 <= monitoring.error_threshold <= 1):
            self.errors.append("Error threshold must be between 0 and 1")

        if monitoring.queue_threshold < 0:
            self.errors.append("Queue threshold must be non-negative")

        if monitoring.memory_threshold <= 0:
            self.errors.append("Memory threshold must be positive")

        if monitoring.response_threshold <= 0:
            self.errors.append("Response threshold must be positive")

    def _validate_tasks_config(self, config: Config) -> None:
        """Validate tasks configuration section

        Args:
            config: Configuration object
        """
        if not config.tasks:
            self.errors.append("At least one task must be configured")

        enabled_tasks = [task for task in config.tasks if task.enabled]
        if not enabled_tasks:
            self.errors.append("At least one task must be enabled")

        # Validate task names are unique
        task_names = [task.name for task in config.tasks if task.enabled]
        if len(task_names) != len(set(task_names)):
            self.errors.append("Task names must be unique")

        # Validate individual tasks
        for task in config.tasks:
            self._validate_task(task)

    def _validate_task(self, task: TaskConfig) -> None:
        """Validate individual task configuration

        Args:
            task: Task configuration object
        """
        if not task.enabled:
            return

        if not task.name:
            self.errors.append("Task name cannot be empty")

        if not task.provider_type:
            self.errors.append(f"Provider type cannot be empty for task: {task.name}")

        if task.storage.plan and not task.storage.directory:
            self.errors.append(f"Task {task.name} storage.plan requires storage.directory")

        if task.max_pages is not None and task.max_pages <= 0:
            self.errors.append(f"Task {task.name} max_pages must be positive")

        from config.schemas import ALLOWED_SEARCH_TYPES

        if not task.search_types:
            self.errors.append(f"Task {task.name} search_types cannot be empty")
        else:
            for search_type in task.search_types:
                if search_type not in ALLOWED_SEARCH_TYPES:
                    self.errors.append(
                        f"Task {task.name} has invalid search_type '{search_type}'; "
                        f"allowed: {sorted(ALLOWED_SEARCH_TYPES)}"
                    )
                if search_type != "code" and not task.use_api:
                    self.errors.append(
                        f"Task {task.name}: search_type '{search_type}' requires use_api=true "
                        "(web HTML parsing only supports code search)"
                    )

        # Validate stage dependencies
        try:
            task.stages.validate()
        except ValueError as e:
            self.errors.append(f"Task {task.name} stage validation failed: {e}")

        # Validate conditions
        if not task.conditions:
            self.errors.append(f"At least one condition required for task: {task.name}")
        else:
            # Check each condition has valid patterns
            for i, condition in enumerate(task.conditions):
                if not condition.patterns.key_pattern:
                    self.errors.append(f"Key pattern required for condition {i+1} in task: {task.name}")

                if not condition.query and not condition.patterns.key_pattern:
                    self.errors.append(f"Either query or key_pattern required for condition {i+1} in task: {task.name}")

    def _validate_worker_manager_config(self, config: Config) -> None:
        """Validate worker manager configuration

        Args:
            config: Configuration object
        """
        worker_manager = config.worker

        if worker_manager.min_workers < 1:
            self.errors.append("Worker manager min_workers must be at least 1")

        if worker_manager.max_workers < worker_manager.min_workers:
            self.errors.append("Worker manager max_workers must be >= min_workers")

        if worker_manager.target_queue_size < 0:
            self.errors.append("Worker manager target_queue_size must be non-negative")

        if worker_manager.adjustment_interval <= 0:
            self.errors.append("Worker manager adjustment_interval must be positive")

        if not (0 < worker_manager.scale_up_threshold < 1):
            self.errors.append("Worker manager scale_up_threshold must be between 0 and 1")

        if not (0 < worker_manager.scale_down_threshold < 1):
            self.errors.append("Worker manager scale_down_threshold must be between 0 and 1")

        if worker_manager.scale_down_threshold >= worker_manager.scale_up_threshold:
            self.errors.append("Worker manager scale_down_threshold must be < scale_up_threshold")

    def _validate_rate_limits(self, config: Config) -> None:
        """Validate rate limits configuration

        Args:
            config: Configuration object
        """
        for name, rate_limit in config.ratelimits.items():
            if rate_limit.base_rate <= 0:
                self.errors.append(f"Base rate must be positive for rate limit: {name}")

            if rate_limit.burst_limit <= 0:
                self.errors.append(f"Burst limit must be positive for rate limit: {name}")

            if not (0 < rate_limit.backoff_factor < 1):
                self.errors.append(f"Backoff factor must be between 0 and 1 for rate limit: {name}")

    def _validate_display_config(self, config: Config) -> None:
        """Validate display configuration

        Args:
            config: Configuration object to validate
        """
        if not config.display:
            self.errors.append("Display configuration is missing")
            return

        if not config.display.contexts:
            self.errors.append("Display contexts configuration is missing")
            return

        # Validate each context and mode
        for context_name, context_modes in config.display.contexts.items():
            if not context_modes:
                self.errors.append(f"No display modes configured for context: {context_name}")
                continue

            for mode_name, mode_config in context_modes.items():
                prefix = f"Display config [{context_name}.{mode_name}]"

                # Validate width
                if mode_config.width <= 0:
                    self.errors.append(f"{prefix}: width must be positive")
                elif mode_config.width < 40:
                    self.errors.append(f"{prefix}: width should be at least 40 characters")
                elif mode_config.width > 200:
                    self.errors.append(f"{prefix}: width should not exceed 200 characters")

                # Validate max_alerts_per_level
                if mode_config.max_alerts_per_level <= 0:
                    self.errors.append(f"{prefix}: max_alerts_per_level must be positive")
                elif mode_config.max_alerts_per_level > 20:
                    self.errors.append(f"{prefix}: max_alerts_per_level should not exceed 20")

                # Validate title
                if not mode_config.title.strip():
                    self.errors.append(f"{prefix}: title cannot be empty")
