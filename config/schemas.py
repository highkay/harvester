#!/usr/bin/env python3

"""
Configuration Data Schemas

This module defines all configuration data classes used throughout the application.
It consolidates and replaces duplicate configuration definitions from multiple files.

Key Features:
- Type-safe configuration structures
- Default value support
- Validation methods
- Unified configuration schema
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.enums import LoadBalanceStrategy, PipelineStage
from core.models import Condition, Patterns, RateLimitConfig


@dataclass
class CredentialsConfig:
    """GitHub credentials configuration with load balancing"""

    sessions: List[str] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list)
    strategy: LoadBalanceStrategy = LoadBalanceStrategy.ROUND_ROBIN

    def __post_init__(self):
        """Validate credentials configuration"""

        # Only require valid credentials if no placeholders are present
        if not isinstance(self.sessions, list):
            self.sessions = list()

        if not isinstance(self.tokens, list):
            self.tokens = list()

        # Convert string strategy to enum if needed
        if isinstance(self.strategy, str):
            self.strategy = LoadBalanceStrategy(self.strategy)


@dataclass
class EdgePoolConfig:
    """GitHub API edge IP pool configuration (inspired by ohmygh/gx)."""

    enabled: bool = True
    # auto: HTTP hosts feed → optional gx → builtin seeds
    source: str = "auto"
    hosts_url: str = "https://hosts.ohmygh.com/v1/hosts"
    gx_bin: str = "gx"
    refresh_interval: int = 3600
    max_edges: int = 32
    verify: bool = True
    # When global.proxy is set, edge routing is off unless this is true
    prefer_over_proxy: bool = False

    def __post_init__(self):
        self.source = (self.source or "auto").strip().lower()
        if self.source not in {"auto", "http", "gx", "builtin", "disabled"}:
            raise ValueError("edge_pool.source must be one of: auto, http, gx, builtin, disabled")
        self.hosts_url = (self.hosts_url or "").strip()
        self.gx_bin = (self.gx_bin or "gx").strip() or "gx"
        self.refresh_interval = max(60, int(self.refresh_interval or 3600))
        self.max_edges = max(1, int(self.max_edges or 32))


@dataclass
class DohConfig:
    """DNS-over-HTTPS fallback for resolving api.github.com."""

    enabled: bool = True
    endpoints: List[str] = field(
        default_factory=lambda: [
            "https://cloudflare-dns.com/dns-query",
            "https://doh.pub/dns-query",
        ]
    )


@dataclass
class GithubCacheConfig:
    """ETag / TTL response cache for GitHub API."""

    enabled: bool = True
    ttl_search: int = 60
    ttl_core: int = 300
    max_entries: int = 1000
    # Relative to workspace unless absolute
    directory: str = "cache/github_api"

    def __post_init__(self):
        self.ttl_search = max(0, int(self.ttl_search if self.ttl_search is not None else 60))
        self.ttl_core = max(0, int(self.ttl_core if self.ttl_core is not None else 300))
        self.max_entries = max(1, int(self.max_entries or 1000))
        self.directory = (self.directory or "cache/github_api").strip()


@dataclass
class GithubIndexConfig:
    """Local discovered-link index for dedup / offline lookup."""

    enabled: bool = True
    directory: str = "cache/search_index"
    # When true, skip creating gather tasks for URLs already in the index
    skip_known_links: bool = False

    def __post_init__(self):
        self.directory = (self.directory or "cache/search_index").strip()


@dataclass
class GithubTransportConfig:
    """GitHub transport enhancements inspired by ohmygh/gx."""

    edge_pool: EdgePoolConfig = field(default_factory=EdgePoolConfig)
    doh: DohConfig = field(default_factory=DohConfig)
    cache: GithubCacheConfig = field(default_factory=GithubCacheConfig)
    index: GithubIndexConfig = field(default_factory=GithubIndexConfig)
    # Track X-RateLimit-Resource (search vs core) per credential
    quota_tracking: bool = True
    # Request GitHub text-match fragments for richer in-result key extraction
    text_match: bool = True


@dataclass
class GlobalConfig:
    """Global application configuration"""

    workspace: str = "./data"
    max_retries_requeued: int = 3
    proxy: str = ""
    github_credentials: Optional[CredentialsConfig] = None
    user_agents: List[str] = field(default_factory=list)
    github_transport: GithubTransportConfig = field(default_factory=GithubTransportConfig)

    def __post_init__(self):
        """Set default values if none provided"""
        self.proxy = self._normalize_proxy(self.proxy)

        # Set default credentials with placeholder values
        if self.github_credentials is None:
            self.github_credentials = CredentialsConfig(
                sessions=[],
                tokens=[],
                strategy=LoadBalanceStrategy.ROUND_ROBIN,
            )

        if self.github_transport is None:
            self.github_transport = GithubTransportConfig()

        # Set default user agents if none provided
        if not self.user_agents:
            self.user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            ]

    @staticmethod
    def _normalize_proxy(proxy: Optional[str]) -> str:
        """Validate and normalize the global HTTP proxy URL."""
        if proxy is None:
            return ""

        if not isinstance(proxy, str):
            raise ValueError("proxy must be a string")

        proxy = proxy.strip()
        if not proxy:
            return ""

        parsed = urlparse(proxy)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5"}:
            raise ValueError("proxy scheme must be one of: http, https, socks5")

        if not parsed.hostname:
            raise ValueError("proxy must include a host")

        # Accessing port validates the port syntax and range
        try:
            port = parsed.port
        except ValueError as e:
            raise ValueError(f"invalid proxy port: {e}") from e

        if scheme == "socks5" and port is None:
            raise ValueError("socks5 proxy must include a port")

        return proxy


def _get_default_threads() -> Dict[str, int]:
    """Get default thread configuration using StandardPipelineStage enum"""
    return {
        PipelineStage.SEARCH.value: 1,
        PipelineStage.GATHER.value: 8,
        PipelineStage.CHECK.value: 4,
        PipelineStage.INSPECT.value: 2,
    }


def _get_default_queue_sizes() -> Dict[str, int]:
    """Get default queue sizes using StandardPipelineStage enum"""
    return {
        PipelineStage.SEARCH.value: 100000,
        PipelineStage.GATHER.value: 200000,
        PipelineStage.CHECK.value: 500000,
        PipelineStage.INSPECT.value: 1000000,
    }


@dataclass
class PipelineConfig:
    """Pipeline stage configuration"""

    threads: Dict[str, int] = field(default_factory=_get_default_threads)
    queue_sizes: Dict[str, int] = field(default_factory=_get_default_queue_sizes)

    def __post_init__(self):
        if not self.threads:
            self.threads = _get_default_threads()
        if not self.queue_sizes:
            self.queue_sizes = _get_default_queue_sizes()


@dataclass
class MonitoringConfig:
    """System monitoring and alerting configuration"""

    update_interval: float = 2.0
    error_threshold: float = 0.1
    queue_threshold: int = 1000
    memory_threshold: int = 1073741824  # 1GB in bytes
    response_threshold: float = 5.0

    def __post_init__(self):
        """Validate monitoring configuration"""
        if self.update_interval <= 0:
            raise ValueError("update_interval must be positive")
        if not (0 <= self.error_threshold <= 1):
            raise ValueError("error_threshold must be between 0 and 1")
        if self.queue_threshold < 0:
            raise ValueError("queue_threshold must be non-negative")
        if self.memory_threshold <= 0:
            raise ValueError("memory_threshold must be positive")
        if self.response_threshold <= 0:
            raise ValueError("response_threshold must be positive")

    def is_error_critical(self, error_rate: float) -> bool:
        """Check if error rate exceeds threshold"""
        return error_rate > self.error_threshold

    def is_queue_critical(self, queue_size: int) -> bool:
        """Check if queue size exceeds threshold"""
        return queue_size > self.queue_threshold

    def is_memory_critical(self, memory_usage_mb: int) -> bool:
        """Check if memory usage exceeds threshold"""
        return memory_usage_mb > self.memory_threshold

    def is_response_critical(self, response_time: float) -> bool:
        """Check if response time exceeds threshold"""
        return response_time > self.response_threshold


@dataclass
class DisplayContextConfig:
    """Display configuration for a specific context"""

    title: str = ""
    show_workers: bool = True
    show_alerts: bool = True
    show_performance: bool = False
    show_newline_prefix: bool = False

    # Formatting options
    width: int = 80
    max_alerts_per_level: int = 3


@dataclass
class DisplayConfig:
    """Display configuration for all contexts"""

    contexts: Dict[str, Dict[str, DisplayContextConfig]] = field(default_factory=dict)

    def __post_init__(self):
        """Set default display configurations if none provided"""
        if not self.contexts:
            self._set_default_contexts()

    def _set_default_contexts(self):
        """Set default display context configurations"""
        # System context
        self.contexts["system"] = {
            "standard": DisplayContextConfig(
                title="System Status", show_workers=True, show_alerts=True, show_performance=False
            ),
            "compact": DisplayContextConfig(
                title="System Status", show_workers=False, show_alerts=False, show_performance=False
            ),
            "detailed": DisplayContextConfig(
                title="Detailed System Status",
                show_workers=True,
                show_alerts=True,
                show_performance=True,
                show_newline_prefix=True,
            ),
        }

        # Monitoring context
        self.contexts["monitoring"] = {
            "standard": DisplayContextConfig(
                title="Pipeline Monitoring", show_workers=True, show_alerts=True, show_performance=True
            ),
            "detailed": DisplayContextConfig(
                title="Detailed Pipeline Monitoring",
                show_workers=True,
                show_alerts=True,
                show_performance=True,
                show_newline_prefix=True,
            ),
        }

        # Task manager context
        self.contexts["task"] = {
            "standard": DisplayContextConfig(
                title="Task Manager Status", show_workers=True, show_alerts=False, show_performance=False
            ),
            "compact": DisplayContextConfig(
                title="Task Manager Status", show_workers=False, show_alerts=False, show_performance=False
            ),
        }

        # Application context
        self.contexts["application"] = {
            "standard": DisplayContextConfig(
                title="Application Status", show_workers=False, show_alerts=True, show_performance=False
            ),
            "detailed": DisplayContextConfig(
                title="Detailed Application Status", show_workers=True, show_alerts=True, show_performance=True
            ),
        }

        # Main context
        self.contexts["main"] = {
            "standard": DisplayContextConfig(
                title="Pipeline Status", show_workers=True, show_alerts=False, show_performance=False
            ),
        }


@dataclass
class PersistenceConfig:
    """Persistence and recovery configuration"""

    batch_size: int = 50
    save_interval: int = 30
    queue_interval: int = 60
    snapshot_interval: int = 300  # seconds, periodic snapshot build interval
    auto_restore: bool = True
    shutdown_timeout: int = 30
    simple: bool = False  # Write simple text files alongside NDJSON


@dataclass
class ApiConfig:
    """API configuration for a provider"""

    base_url: str = ""
    completion_path: str = ""
    model_path: str = ""
    default_model: str = ""
    auth_key: str = "Authorization"
    extra_headers: Dict[str, str] = field(default_factory=dict)
    api_version: str = ""
    timeout: int = 30
    retries: int = 3
    use_proxy: bool = True


@dataclass
class StageConfig:
    """Pipeline stage configuration for individual tasks"""

    search: bool = True
    gather: bool = True
    check: bool = True
    inspect: bool = True

    def validate(self) -> None:
        """Validate stage dependencies"""
        if not self.check and self.inspect:
            raise ValueError("inspect stage requires check stage to be enabled")


@dataclass
class StorageConfig:
    """Result storage grouping for a provider task"""

    directory: str = ""
    plan: str = ""


# Supported GitHub search result types (code is the primary key-discovery path)
ALLOWED_SEARCH_TYPES = frozenset({"code", "issues", "commits"})


@dataclass
class TaskConfig:
    """Configuration for a single provider task"""

    name: str = ""
    enabled: bool = True
    provider_type: str = ""
    use_api: bool = False
    max_pages: Optional[int] = None
    # GitHub search kinds to fan out; default code-only for backward compatibility
    search_types: List[str] = field(default_factory=lambda: ["code"])
    stages: StageConfig = field(default_factory=StageConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    extras: Dict[str, Any] = field(default_factory=dict)
    api: ApiConfig = field(default_factory=ApiConfig)
    patterns: Patterns = field(default_factory=Patterns)
    conditions: List[Condition] = field(default_factory=list)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self):
        if not self.search_types:
            self.search_types = ["code"]
        normalized: List[str] = []
        for item in self.search_types:
            value = str(item or "").strip().lower()
            if not value:
                continue
            if value not in ALLOWED_SEARCH_TYPES:
                raise ValueError(
                    f"Invalid search_type '{value}' for task {self.name or '<unnamed>'}; "
                    f"allowed: {sorted(ALLOWED_SEARCH_TYPES)}"
                )
            if value not in normalized:
                normalized.append(value)
        self.search_types = normalized or ["code"]


@dataclass
class WorkerManagerConfig:
    """Worker manager configuration for dynamic thread management"""

    # Enable/disable worker manager (default: disabled)
    enabled: bool = False
    min_workers: int = 1
    max_workers: int = 10
    target_queue_size: int = 100
    adjustment_interval: float = 5.0
    scale_up_threshold: float = 0.8
    scale_down_threshold: float = 0.2

    # Enable/disable worker adjustment recommendation logging
    log_recommendations: bool = True

    def __post_init__(self):
        """Validate worker manager configuration"""
        if self.min_workers < 1:
            raise ValueError("min_workers must be at least 1")
        if self.max_workers < self.min_workers:
            raise ValueError("max_workers must be >= min_workers")
        if self.target_queue_size < 0:
            raise ValueError("target_queue_size must be non-negative")
        if self.adjustment_interval <= 0:
            raise ValueError("adjustment_interval must be positive")
        if not (0 < self.scale_up_threshold < 1):
            raise ValueError("scale_up_threshold must be between 0 and 1")
        if not (0 < self.scale_down_threshold < 1):
            raise ValueError("scale_down_threshold must be between 0 and 1")
        if self.scale_down_threshold >= self.scale_up_threshold:
            raise ValueError("scale_down_threshold must be < scale_up_threshold")

    def is_scale_up_needed(self, queue_ratio: float) -> bool:
        """Check if scale up is needed based on queue ratio"""
        return queue_ratio > self.scale_up_threshold

    def is_scale_down_needed(self, queue_ratio: float) -> bool:
        """Check if scale down is needed based on queue ratio"""
        return queue_ratio < self.scale_down_threshold

    def calculate_target_workers(self, current_queue_size: int, current_workers: int) -> int:
        """Calculate target number of workers based on current metrics"""
        if current_queue_size == 0:
            return max(self.min_workers, current_workers - 1)

        queue_ratio = current_queue_size / max(self.target_queue_size, 1)

        if queue_ratio > self.scale_up_threshold:
            target = min(self.max_workers, current_workers + 1)
        elif queue_ratio < self.scale_down_threshold:
            target = max(self.min_workers, current_workers - 1)
        else:
            target = current_workers

        return target


@dataclass
class Config:
    """Main configuration container"""

    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    worker: WorkerManagerConfig = field(default_factory=WorkerManagerConfig)
    ratelimits: Dict[str, RateLimitConfig] = field(default_factory=dict)
    tasks: List[TaskConfig] = field(default_factory=list)

    def __post_init__(self):
        """Set default rate limits if none provided"""
        if not self.ratelimits:
            self.ratelimits = {
                "github_api": RateLimitConfig(base_rate=0.15, burst_limit=3, adaptive=True),
                "github_web": RateLimitConfig(base_rate=0.5, burst_limit=2, adaptive=True),
            }

    def to_dict(self) -> Dict[str, Any]:
        """Convert Config object to dictionary

        Returns:
            Dict[str, Any]: Configuration as dictionary with proper structure
        """
        return {
            "global": self._dataclass_to_dict(self.global_config),
            "pipeline": self._dataclass_to_dict(self.pipeline),
            "monitoring": self._dataclass_to_dict(self.monitoring),
            "display": self._dataclass_to_dict(self.display),
            "persistence": self._dataclass_to_dict(self.persistence),
            "worker": self._dataclass_to_dict(self.worker),
            "ratelimits": {k: self._dataclass_to_dict(v) for k, v in self.ratelimits.items()},
            "tasks": [self._dataclass_to_dict(task) for task in self.tasks],
        }

    def _dataclass_to_dict(self, obj: Any) -> Any:
        """Convert dataclass object to dictionary recursively

        Args:
            obj: Object to convert (dataclass, dict, list, or primitive)

        Returns:
            Any: Converted object
        """
        if hasattr(obj, "__dataclass_fields__"):
            # Handle dataclass objects
            result = {}
            for field_name in obj.__dataclass_fields__.keys():
                value = getattr(obj, field_name)
                result[field_name] = self._dataclass_to_dict(value)
            return result
        elif isinstance(obj, dict):
            # Handle dictionaries
            return {k: self._dataclass_to_dict(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            # Handle lists and tuples
            return [self._dataclass_to_dict(item) for item in obj]
        elif hasattr(obj, "value"):
            # Handle enums
            return obj.value
        else:
            # Handle primitive types
            return obj
