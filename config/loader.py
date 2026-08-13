#!/usr/bin/env python3

"""
Configuration Loader

This module provides unified YAML configuration loading functionality.
It replaces the scattered configuration loading logic throughout the application.

Key Features:
- Single YAML file loading
- Default configuration generation
- Type-safe configuration parsing
- Validation integration
"""

import os
from typing import Any, Dict

import yaml

from core.models import Condition, Patterns, RateLimitConfig, inherit_patterns

from .defaults import get_default_config
from .schemas import (
    ApiConfig,
    Config,
    CredentialsConfig,
    DisplayConfig,
    DisplayContextConfig,
    GlobalConfig,
    LoadBalanceStrategy,
    MonitoringConfig,
    PersistenceConfig,
    PipelineConfig,
    StageConfig,
    StorageConfig,
    TaskConfig,
    WorkerManagerConfig,
)
from .validator import ConfigValidator


class ConfigLoader:
    """Unified configuration loader for YAML files"""

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize configuration loader

        Args:
            config_file: Path to configuration file
        """
        self.config_file = config_file
        self.validator = ConfigValidator()

    def load(self) -> Config:
        """Load configuration from YAML file

        Returns:
            Config: Loaded and validated configuration object
        """
        if not os.path.exists(self.config_file):
            self._create_default_config()

        try:
            with open(self.config_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            config = self._parse_config(data)
            self.validator.validate(config)
            return config

        except Exception as e:
            raise RuntimeError(f"Failed to load config file {self.config_file}: {e}")

    def _parse_config(self, data: Dict[str, Any]) -> Config:
        """Parse YAML data into Config object

        Args:
            data: Raw YAML data

        Returns:
            Config: Parsed configuration object
        """
        config = Config()

        # Parse global configuration
        if "global" in data:
            config.global_config = self._parse_global_config(data["global"])

        # Parse pipeline configuration
        if "pipeline" in data:
            config.pipeline = self._parse_pipeline_config(data["pipeline"])

        # Parse monitoring configuration
        if "monitoring" in data:
            config.monitoring = self._parse_monitoring_config(data["monitoring"])

        # Parse display configuration
        if "display" in data:
            config.display = self._parse_display_config(data["display"])

        # Parse persistence configuration
        if "persistence" in data:
            config.persistence = self._parse_persistence_config(data["persistence"])

        # Parse worker configuration
        if "worker" in data:
            config.worker = self._parse_worker_manager_config(data["worker"])

        # Parse rate limits
        if "ratelimits" in data:
            config.ratelimits = self._parse_rate_limits(data["ratelimits"])

        # Parse tasks
        if "tasks" in data:
            config.tasks = [self._parse_task_config(task_data) for task_data in data["tasks"]]

        return config

    def _parse_global_config(self, data: Dict[str, Any]) -> GlobalConfig:
        """Parse global configuration section

        Args:
            data: Global configuration data

        Returns:
            GlobalConfig: Parsed global configuration
        """
        credentials_data = data.get("github_credentials", {})

        # Get sessions and tokens from config
        sessions = credentials_data.get("sessions") or []
        tokens = credentials_data.get("tokens") or []

        # Filter out placeholder values from config
        valid_sessions = [s for s in sessions if s and not s.startswith("your_")]
        valid_tokens = [t for t in tokens if t and not t.startswith("your_")]

        # If both valid sessions and tokens are empty, try to read from environment variables
        if not valid_sessions and not valid_tokens:
            # Try to read GitHub sessions from GITHUB_SESSIONS environment variable
            github_sessions = os.getenv("GITHUB_SESSIONS")
            if github_sessions:
                valid_sessions = [s.strip() for s in github_sessions.split(",") if s.strip()]

            # Try to read GitHub tokens from GITHUB_TOKENS environment variable
            github_tokens = os.getenv("GITHUB_TOKENS")
            if github_tokens:
                valid_tokens = [t.strip() for t in github_tokens.split(",") if t.strip()]

        credentials = CredentialsConfig(
            sessions=valid_sessions,
            tokens=valid_tokens,
            strategy=LoadBalanceStrategy(credentials_data.get("strategy", "round_robin")),
        )

        # Explicit proxy: "" in YAML disables env proxy; omit key to inherit http(s)_proxy
        if "proxy" in data:
            proxy = data.get("proxy") or ""
        else:
            proxy = self._get_env_ignore_case("https_proxy", "http_proxy")

        return GlobalConfig(
            workspace=os.path.abspath(data.get("workspace", "./data")),
            max_retries_requeued=data.get("max_retries_requeued", 3),
            proxy=proxy,
            github_credentials=credentials,
            user_agents=data.get("user_agents", []),
            github_transport=self._parse_github_transport(data.get("github_transport", {})),
        )

    def _parse_github_transport(self, data: Dict[str, Any]) -> "GithubTransportConfig":
        """Parse GitHub transport enhancements (edge pool / DoH / cache)."""
        from config.schemas import (
            DohConfig,
            EdgePoolConfig,
            GithubCacheConfig,
            GithubIndexConfig,
            GithubTransportConfig,
        )

        if not isinstance(data, dict):
            data = {}

        edge_data = data.get("edge_pool", {}) or {}
        doh_data = data.get("doh", {}) or {}
        cache_data = data.get("cache", {}) or {}
        index_data = data.get("index", {}) or {}

        edge = EdgePoolConfig(
            enabled=bool(edge_data.get("enabled", True)),
            source=str(edge_data.get("source", "auto")),
            hosts_url=str(edge_data.get("hosts_url", "https://hosts.ohmygh.com/v1/hosts")),
            gx_bin=str(edge_data.get("gx_bin", "gx")),
            refresh_interval=int(edge_data.get("refresh_interval", 3600)),
            max_edges=int(edge_data.get("max_edges", 32)),
            verify=bool(edge_data.get("verify", True)),
            prefer_over_proxy=bool(edge_data.get("prefer_over_proxy", False)),
        )

        endpoints = doh_data.get("endpoints")
        if not isinstance(endpoints, list) or not endpoints:
            endpoints = None
        doh = DohConfig(
            enabled=bool(doh_data.get("enabled", True)),
            endpoints=endpoints if endpoints is not None else DohConfig().endpoints,
        )

        cache = GithubCacheConfig(
            enabled=bool(cache_data.get("enabled", True)),
            ttl_search=int(cache_data.get("ttl_search", 60)),
            ttl_core=int(cache_data.get("ttl_core", 300)),
            max_entries=int(cache_data.get("max_entries", 1000)),
            directory=str(cache_data.get("directory", "cache/github_api")),
        )

        index = GithubIndexConfig(
            enabled=bool(index_data.get("enabled", True)),
            directory=str(index_data.get("directory", "cache/search_index")),
            skip_known_links=bool(index_data.get("skip_known_links", False)),
        )

        return GithubTransportConfig(
            edge_pool=edge,
            doh=doh,
            cache=cache,
            index=index,
            quota_tracking=bool(data.get("quota_tracking", True)),
            text_match=bool(data.get("text_match", True)),
        )

    @staticmethod
    def _get_env_ignore_case(*names: str) -> str:
        """Read the first non-empty environment variable by name, ignoring case."""
        env = {key.lower(): value for key, value in os.environ.items()}
        for name in names:
            value = env.get(name.lower(), "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _parse_pipeline_config(self, data: Dict[str, Any]) -> PipelineConfig:
        """Parse pipeline configuration section

        Args:
            data: Pipeline configuration data

        Returns:
            PipelineConfig: Parsed pipeline configuration
        """
        return PipelineConfig(threads=data.get("threads", {}), queue_sizes=data.get("queue_sizes", {}))

    def _parse_monitoring_config(self, data: Dict[str, Any]) -> MonitoringConfig:
        """Parse system monitoring configuration section

        Args:
            data: Monitoring configuration data

        Returns:
            MonitoringConfig: Parsed monitoring configuration
        """
        return MonitoringConfig(
            update_interval=data.get("update_interval", 2.0),
            error_threshold=data.get("error_threshold", 0.1),
            queue_threshold=data.get("queue_threshold", 1000),
            memory_threshold=data.get("memory_threshold", 1073741824),
            response_threshold=data.get("response_threshold", 5.0),
        )

    def _parse_display_config(self, data: Dict[str, Any]) -> DisplayConfig:
        """Parse display configuration section

        Args:
            data: Display configuration data

        Returns:
            DisplayConfig: Parsed display configuration
        """
        contexts = {}
        contexts_data = data.get("contexts", {})

        for context_name, modes_data in contexts_data.items():
            contexts[context_name] = {}
            for mode_name, mode_data in modes_data.items():
                contexts[context_name][mode_name] = DisplayContextConfig(
                    title=mode_data.get("title", ""),
                    show_workers=mode_data.get("show_workers", True),
                    show_alerts=mode_data.get("show_alerts", True),
                    show_performance=mode_data.get("show_performance", False),
                    show_newline_prefix=mode_data.get("show_newline_prefix", False),
                    width=mode_data.get("width", 80),
                    max_alerts_per_level=mode_data.get("max_alerts_per_level", 3),
                )

        return DisplayConfig(contexts=contexts)

    def _parse_persistence_config(self, data: Dict[str, Any]) -> PersistenceConfig:
        """Parse persistence configuration section

        Args:
            data: Persistence configuration data

        Returns:
            PersistenceConfig: Parsed persistence configuration
        """
        return PersistenceConfig(
            batch_size=data.get("batch_size", 50),
            save_interval=data.get("save_interval", 30),
            queue_interval=data.get("queue_interval", 60),
            auto_restore=data.get("auto_restore", True),
            shutdown_timeout=data.get("shutdown_timeout", 30),
            simple=data.get("format", "txt").strip().lower() == "txt",
        )

    def _parse_worker_manager_config(self, data: Dict[str, Any]) -> WorkerManagerConfig:
        """Parse worker manager configuration section

        Args:
            data: Worker manager configuration data

        Returns:
            WorkerManagerConfig: Parsed worker manager configuration
        """
        return WorkerManagerConfig(
            enabled=data.get("enabled", False),
            min_workers=data.get("min_workers", 1),
            max_workers=data.get("max_workers", 10),
            target_queue_size=data.get("target_queue_size", 100),
            adjustment_interval=data.get("adjustment_interval", 5.0),
            scale_up_threshold=data.get("scale_up_threshold", 0.8),
            scale_down_threshold=data.get("scale_down_threshold", 0.2),
            log_recommendations=data.get("log_recommendations", True),
        )

    def _parse_rate_limits(self, data: Dict[str, Any]) -> Dict[str, RateLimitConfig]:
        """Parse rate limits configuration section

        Args:
            data: Rate limits configuration data

        Returns:
            Dict[str, RateLimitConfig]: Parsed rate limits
        """
        rate_limits = {}
        for name, limit_data in data.items():
            rate_limits[name] = RateLimitConfig(
                base_rate=limit_data.get("base_rate", 1.0),
                burst_limit=limit_data.get("burst_limit", 5),
                adaptive=limit_data.get("adaptive", True),
                backoff_factor=limit_data.get("backoff_factor", 0.5),
                recovery_factor=limit_data.get("recovery_factor", 1.1),
                max_rate_multiplier=limit_data.get("max_rate_multiplier", 2.0),
                min_rate_multiplier=limit_data.get("min_rate_multiplier", 0.1),
            )
        return rate_limits

    def _parse_task_config(self, data: Dict[str, Any]) -> TaskConfig:
        """Parse task configuration

        Args:
            data: Task configuration data

        Returns:
            TaskConfig: Parsed task configuration
        """
        # Parse stages
        stages_data = data.get("stages", {})
        stages = StageConfig(
            search=stages_data.get("search", True),
            gather=stages_data.get("gather", True),
            check=stages_data.get("check", True),
            inspect=stages_data.get("inspect", True),
        )

        # Parse API configuration
        api_data = data.get("api", {})
        api = ApiConfig(
            base_url=api_data.get("base_url", ""),
            completion_path=api_data.get("completion_path", "/v1/chat/completions"),
            model_path=api_data.get("model_path", "/v1/models"),
            default_model=api_data.get("default_model", ""),
            auth_key=api_data.get("auth_key", "Authorization"),
            extra_headers=api_data.get("extra_headers", {}),
            api_version=api_data.get("api_version", ""),
            timeout=api_data.get("timeout", 30),
            retries=api_data.get("retries", 3),
            use_proxy=api_data.get("use_proxy", True),
        )

        # Parse patterns
        patterns_data = data.get("patterns", {})
        patterns = Patterns(
            key_pattern=patterns_data.get("key_pattern", ""),
            address_pattern=patterns_data.get("address_pattern", ""),
            endpoint_pattern=patterns_data.get("endpoint_pattern", ""),
            model_pattern=patterns_data.get("model_pattern", ""),
        )

        # Parse conditions
        conditions_data = data.get("conditions", [])
        conditions = []
        for condition_data in conditions_data:
            condition = Condition.from_dict(condition_data)
            # Inherit global patterns if condition patterns are empty
            inherit_patterns(patterns, condition)
            conditions.append(condition)

        # Parse rate limit
        rate_limit_data = data.get("rate_limit", {})
        rate_limit = RateLimitConfig(
            base_rate=rate_limit_data.get("base_rate", 1.0),
            burst_limit=rate_limit_data.get("burst_limit", 5),
            adaptive=rate_limit_data.get("adaptive", True),
        )

        storage_data = data.get("storage", {})
        storage = StorageConfig(
            directory=storage_data.get("directory", ""),
            plan=storage_data.get("plan", ""),
        )

        search_types = data.get("search_types")
        if not isinstance(search_types, list) or not search_types:
            search_types = ["code"]

        return TaskConfig(
            name=data.get("name", ""),
            enabled=data.get("enabled", True),
            provider_type=data.get("provider_type", ""),
            use_api=data.get("use_api", False),
            max_pages=data.get("max_pages"),
            search_types=search_types,
            stages=stages,
            storage=storage,
            extras=data.get("extras", {}),
            api=api,
            patterns=patterns,
            conditions=conditions,
            rate_limit=rate_limit,
        )

    def _create_default_config(self) -> None:
        """Create default configuration file"""
        default_config = get_default_config()

        with open(self.config_file, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True, indent=2)

        print(f"Created default configuration file: {self.config_file}")
        print("Please edit the configuration file and set your GitHub credentials and other settings")
