#!/usr/bin/env python3

"""
Core Models - Fundamental Data Models

This module defines the core data models used throughout the application.
These models represent the fundamental entities and data structures that
form the foundation of the system.
"""

import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .enums import ErrorReason


@dataclass(frozen=True)
class ResultStorage:
    """Result persistence layout for a provider."""

    folder: str
    filenames: Dict[str, str] = field(default_factory=dict)


@dataclass
class ProviderTask(ABC):
    """Base class for all provider-specific tasks

    Abstract base class that defines the common interface and behavior
    for all tasks in the pipeline system. Each task carries provider
    identification for proper routing and result isolation.
    """

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider: str = ""  # Provider name for routing and isolation
    created_at: float = field(default_factory=time.time)
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize task to dictionary for persistence"""
        return {
            "type": self.__class__.__name__,
            "task_id": self.task_id,
            "provider": self.provider,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "data": self._serialize_data(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderTask":
        """Deserialize task from dictionary"""
        instance = cls.__new__(cls)
        instance.task_id = data["task_id"]
        instance.provider = data["provider"]
        instance.created_at = data["created_at"]
        instance.attempts = data["attempts"]
        instance._deserialize_data(data["data"])
        return instance

    @abstractmethod
    def _serialize_data(self) -> Dict[str, Any]:
        """Serialize task-specific data"""
        pass

    @abstractmethod
    def _deserialize_data(self, data: Dict[str, Any]) -> None:
        """Deserialize task-specific data"""
        pass

    def increment_attempts(self) -> None:
        """Increment attempt counter"""
        self.attempts += 1

    def get_age_seconds(self) -> float:
        """Get task age in seconds"""
        return time.time() - self.created_at

    def is_expired(self, max_age_seconds: float) -> bool:
        """Check if task has exceeded maximum age"""
        return self.get_age_seconds() > max_age_seconds


@dataclass
class SearchTask(ProviderTask):
    """Task for searching GitHub for potential API keys"""

    query: str = ""
    regex: str = ""
    page: int = 1
    use_api: bool = False
    max_pages: Optional[int] = None
    # GitHub search kind: code (default), issues, commits
    search_type: str = "code"
    address_pattern: str = ""
    endpoint_pattern: str = ""
    model_pattern: str = ""

    def _serialize_data(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "regex": self.regex,
            "page": self.page,
            "use_api": self.use_api,
            "max_pages": self.max_pages,
            "search_type": self.search_type,
            "address_pattern": self.address_pattern,
            "endpoint_pattern": self.endpoint_pattern,
            "model_pattern": self.model_pattern,
        }

    def _deserialize_data(self, data: Dict[str, Any]) -> None:
        self.query = data["query"]
        self.regex = data.get("regex", "")
        self.page = data["page"]
        self.use_api = data.get("use_api", False)
        self.max_pages = data.get("max_pages")
        self.search_type = data.get("search_type", "code") or "code"
        self.address_pattern = data.get("address_pattern", "")
        self.endpoint_pattern = data.get("endpoint_pattern", "")
        self.model_pattern = data.get("model_pattern", "")

    def get_search_term(self) -> str:
        """Get the primary search term"""
        return self.query or self.regex


@dataclass
class AcquisitionTask(ProviderTask):
    """Task for acquiring API keys from discovered URLs"""

    url: str = ""
    key_pattern: str = ""
    retries: int = 3
    address_pattern: str = ""
    endpoint_pattern: str = ""
    model_pattern: str = ""

    def _serialize_data(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "key_pattern": self.key_pattern,
            "retries": self.retries,
            "address_pattern": self.address_pattern,
            "endpoint_pattern": self.endpoint_pattern,
            "model_pattern": self.model_pattern,
        }

    def _deserialize_data(self, data: Dict[str, Any]) -> None:
        self.url = data["url"]
        self.key_pattern = data["key_pattern"]
        self.retries = data.get("retries", 3)
        self.address_pattern = data.get("address_pattern", "")
        self.endpoint_pattern = data.get("endpoint_pattern", "")
        self.model_pattern = data.get("model_pattern", "")


@dataclass
class CheckTask(ProviderTask):
    """Task for validating API keys"""

    service: "Service" = field(default_factory=lambda: Service())
    custom_url: str = ""
    retries: int = 3

    def _serialize_data(self) -> Dict[str, Any]:
        return {
            "service": self.service.to_dict(),
            "custom_url": self.custom_url,
            "retries": self.retries,
        }

    def _deserialize_data(self, data: Dict[str, Any]) -> None:
        self.service = Service.from_dict(data["service"])
        self.custom_url = data.get("custom_url", "")
        self.retries = data.get("retries", 3)


@dataclass
class InspectTask(ProviderTask):
    """Task for inspecting API capabilities"""

    service: "Service" = field(default_factory=lambda: Service())
    custom_url: str = ""
    retries: int = 3

    def _serialize_data(self) -> Dict[str, Any]:
        return {
            "service": self.service.to_dict(),
            "custom_url": self.custom_url,
            "retries": self.retries,
        }

    def _deserialize_data(self, data: Dict[str, Any]) -> None:
        self.service = Service.from_dict(data["service"])
        self.custom_url = data.get("custom_url", "")
        self.retries = data.get("retries", 3)


@dataclass
class RateLimitConfig:
    """Unified rate limiting configuration"""

    base_rate: float = 1.0
    burst_limit: int = 5
    adaptive: bool = True
    backoff_factor: float = 0.5
    recovery_factor: float = 1.1
    max_rate_multiplier: float = 2.0
    min_rate_multiplier: float = 0.1

    def __post_init__(self):
        """Validate rate limit configuration"""
        if self.base_rate <= 0:
            raise ValueError("base_rate must be positive")
        if self.burst_limit <= 0:
            raise ValueError("burst_limit must be positive")
        if not (0 < self.backoff_factor < 1):
            raise ValueError("backoff_factor must be between 0 and 1")
        if self.recovery_factor <= 1:
            raise ValueError("recovery_factor must be > 1")

    def calculate_adjusted_rate(self, success_ratio: float) -> float:
        """Calculate adjusted rate based on success ratio"""
        if not self.adaptive:
            return self.base_rate

        if success_ratio > 0.9:
            multiplier = min(self.max_rate_multiplier, self.recovery_factor)
        elif success_ratio < 0.5:
            multiplier = max(self.min_rate_multiplier, self.backoff_factor)
        else:
            multiplier = 1.0

        return self.base_rate * multiplier


# Task recovery and logging models
@dataclass
class LogFileInfo:
    """Information about a log file"""

    filename: str
    size: str
    modified: str
    path: str
    error: Optional[str] = None


@dataclass
class LoggingStats:
    """Logging system statistics"""

    active_loggers: int
    log_files: Dict[str, LogFileInfo] = field(default_factory=dict)
    logs_directory: Optional[str] = None


@dataclass
class RecoveredTasks:
    """Recovery data for a single provider"""

    check: List["Service"] = field(default_factory=list)
    acquisition: List[str] = field(default_factory=list)
    invalid: Set["Service"] = field(default_factory=set)
    # Previously validated keys (re-seeded after backup so valid-keys.txt is not lost)
    valid: List["Service"] = field(default_factory=list)

    def has_tasks(self) -> bool:
        """Check if any tasks need recovery"""
        return bool(self.check or self.acquisition or self.invalid or self.valid)

    def check_count(self) -> int:
        """Get number of check tasks"""
        return len(self.check)

    def acquisition_count(self) -> int:
        """Get number of acquisition tasks"""
        return len(self.acquisition)

    def invalid_count(self) -> int:
        """Get number of invalid keys"""
        return len(self.invalid)

    def valid_count(self) -> int:
        """Get number of previously valid keys"""
        return len(self.valid)

    def valid_check_tasks(self) -> List["Service"]:
        """Get check tasks filtered by invalid keys"""
        return [task for task in self.check if task not in self.invalid]

    def summary(self) -> str:
        """Get task summary string"""
        return (
            f"check: {self.check_count()}, acquisition: {self.acquisition_count()}, "
            f"invalid: {self.invalid_count()}, valid: {self.valid_count()}"
        )


@dataclass
class AllRecoveredTasks:
    """Recovery data for all providers"""

    providers: Dict[str, RecoveredTasks] = field(default_factory=dict)

    def add_provider(self, name: str, tasks: RecoveredTasks) -> None:
        """Add provider tasks if any exist"""
        if tasks.has_tasks():
            self.providers[name] = tasks

    def get_provider(self, name: str) -> RecoveredTasks:
        """Get provider tasks safely"""
        return self.providers.get(name, RecoveredTasks())

    def has_providers(self) -> bool:
        """Check if any providers have tasks"""
        return bool(self.providers)

    def provider_count(self) -> int:
        """Get number of providers with tasks"""
        return len(self.providers)

    def total_check_tasks(self) -> int:
        """Get total check tasks across all providers"""
        return sum(tasks.check_count() for tasks in self.providers.values())

    def total_acquisition_tasks(self) -> int:
        """Get total acquisition tasks across all providers"""
        return sum(tasks.acquisition_count() for tasks in self.providers.values())

    def total_invalid_keys(self) -> int:
        """Get total invalid keys across all providers"""
        return sum(tasks.invalid_count() for tasks in self.providers.values())

    def total_valid_keys(self) -> int:
        """Get total previously valid keys across all providers"""
        return sum(tasks.valid_count() for tasks in self.providers.values())

    def summary(self) -> str:
        """Get summary of all recovered tasks"""
        if not self.has_providers():
            return "No tasks recovered"

        return (
            f"Providers: {self.provider_count()}, "
            f"Check: {self.total_check_tasks()}, "
            f"Acquisition: {self.total_acquisition_tasks()}, "
            f"Invalid: {self.total_invalid_keys()}"
        )


@dataclass
class TaskRecoveryInfo:
    """Information for task recovery"""

    queue_tasks: Dict[str, List[ProviderTask]] = field(default_factory=dict)
    result_tasks: Optional[AllRecoveredTasks] = None
    total_queue_tasks: int = 0
    total_result_tasks: int = 0


@dataclass
class Service:
    """Core service data model representing an API service endpoint

    This is the fundamental data structure representing a discovered
    API service with its authentication and endpoint information.
    """

    # Server address
    address: str = ""

    # Application name or endpoint identifier
    endpoint: str = ""

    # API token/key for authentication
    key: str = ""

    # Model name for AI services
    model: str = ""

    def __hash__(self) -> int:
        """Hash based on all fields for use in sets and dicts"""
        return hash((self.address, self.endpoint, self.key, self.model))

    def __eq__(self, other: object) -> bool:
        """Equality comparison based on all fields"""
        if not isinstance(other, Service):
            return False

        return (
            self.address == other.address
            and self.endpoint == other.endpoint
            and self.key == other.key
            and self.model == other.model
        )

    def is_valid(self) -> bool:
        """Check if service has minimum required information"""
        return bool(self.key and (self.address or self.endpoint))

    def get_identifier(self) -> str:
        """Get unique identifier for this service"""
        return f"{self.address}:{self.endpoint}:{self.key[:8]}..."

    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary for serialization"""
        return {
            "address": self.address,
            "endpoint": self.endpoint,
            "key": self.key,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "Service":
        """Create Service from dictionary"""
        return cls(
            address=data.get("address", ""),
            endpoint=data.get("endpoint", ""),
            key=data.get("key", ""),
            model=data.get("model", ""),
        )

    def serialize(self) -> str:
        if not self.address and not self.endpoint and not self.model:
            return self.key

        data = {}
        if self.address:
            data["address"] = self.address
        if self.endpoint:
            data["endpoint"] = self.endpoint
        if self.key:
            data["key"] = self.key
        if self.model:
            data["model"] = self.model

        return "" if not data else json.dumps(data)

    @classmethod
    def deserialize(cls, text: str) -> Optional["Service"]:
        if not text:
            return None

        try:
            item = json.loads(text)
            return cls(
                address=item.get("address", ""),
                endpoint=item.get("endpoint", ""),
                key=item.get("key", ""),
                model=item.get("model", ""),
            )
        except Exception:
            return cls(key=text)


@dataclass
class CheckResult:
    """Result of API token validation check

    Represents the outcome of validating an API token against a service,
    including success status, error information, and additional metadata.
    """

    available: bool = False
    error_reason: ErrorReason = ErrorReason.UNKNOWN
    message: str = ""
    response_time: float = 0.0
    status_code: Optional[int] = None

    @property
    def ok(self) -> bool:
        """Alias for available for backward compatibility"""
        return self.available

    @property
    def reason(self) -> ErrorReason:
        """Alias for error_reason for backward compatibility"""
        return self.error_reason

    @classmethod
    def success(cls, message: str = "Token is valid", response_time: float = 0.0) -> "CheckResult":
        """Create a successful check result"""
        return cls(
            available=True,
            error_reason=ErrorReason.UNKNOWN,
            message=message,
            response_time=response_time,
        )

    @classmethod
    def fail(
        cls, reason: ErrorReason, message: str = "", response_time: float = 0.0, status_code: Optional[int] = None
    ) -> "CheckResult":
        """Create a failed check result"""
        return cls(
            available=False,
            error_reason=reason,
            message=message or reason.value,
            response_time=response_time,
            status_code=status_code,
        )

    def is_retryable(self) -> bool:
        """Check if the error is retryable"""
        return self.error_reason.is_retryable()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "available": self.available,
            "error_reason": self.error_reason.value,
            "message": self.message,
            "response_time": self.response_time,
            "status_code": self.status_code,
        }


@dataclass
class Patterns:
    """Extraction patterns for keys and metadata"""

    key_pattern: str = ""
    address_pattern: str = ""
    endpoint_pattern: str = ""
    model_pattern: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary"""
        return {
            "key_pattern": self.key_pattern,
            "address_pattern": self.address_pattern,
            "endpoint_pattern": self.endpoint_pattern,
            "model_pattern": self.model_pattern,
        }


@dataclass
class Condition:
    """Search condition with complete pattern configuration

    Defines search parameters and extraction patterns used to discover
    API keys and services for a specific provider.
    """

    query: Optional[str] = None
    patterns: Patterns = field(default_factory=Patterns)
    description: str = ""
    enabled: bool = True

    def __post_init__(self):
        """Validate condition after initialization"""
        if not self.query and not self.patterns.key_pattern:
            raise ValueError("Condition must have either query or key_pattern")

    def get_search_term(self) -> str:
        """Get the primary search term"""
        return self.query or self.patterns.key_pattern or ""

    def is_valid(self) -> bool:
        """Check if condition is valid and enabled"""
        return self.enabled and bool(self.get_search_term())

    def __hash__(self) -> int:
        """Hash based on query and patterns for use in sets and dicts"""
        return hash((self.query, self.patterns.key_pattern))

    def __eq__(self, other: object) -> bool:
        """Equality comparison based on query and patterns"""
        if not isinstance(other, Condition):
            return False
        return self.query == other.query and self.patterns == other.patterns

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "query": self.query,
            "patterns": self.patterns.to_dict(),
            "description": self.description,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Condition":
        """Create Condition from dictionary"""
        params = data.get("patterns", {})
        patterns = Patterns(**params) if params else Patterns()

        return cls(
            query=data.get("query"),
            patterns=patterns,
            description=data.get("description", ""),
            enabled=data.get("enabled", True),
        )


@dataclass
class TokenBucket:
    """Token bucket for rate limiting with burst support

    Implements the token bucket algorithm for rate limiting with
    adaptive rate adjustment based on success/failure feedback.
    """

    def __init__(self, rate: float, burst: int, adaptive: bool = True):
        """Initialize token bucket

        Args:
            rate: Tokens per second
            burst: Maximum tokens (bucket capacity)
            adaptive: Enable adaptive rate adjustment
        """
        self.rate = rate
        self.burst = burst
        self.adaptive = adaptive
        self.tokens = float(burst)
        self.last_update = time.time()
        self.lock = threading.Lock()

        # Adaptive rate adjustment
        self.original_rate = rate
        self.consecutive_success = 0
        self.consecutive_failures = 0
        self.last_adjustment = time.time()

    def acquire(self, tokens: int = 1) -> bool:
        """Acquire tokens from bucket

        Args:
            tokens: Number of tokens to acquire

        Returns:
            bool: True if tokens were acquired, False if rate limited
        """
        with self.lock:
            now = time.time()

            # Add tokens based on elapsed time
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now

            # Check if enough tokens available
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True

            return False

    def wait_time(self, tokens: int = 1) -> float:
        """Calculate wait time needed to acquire tokens

        Args:
            tokens: Number of tokens needed

        Returns:
            float: Wait time in seconds
        """
        with self.lock:
            if self.tokens >= tokens:
                return 0.0

            needed = tokens - self.tokens
            return needed / self.rate

    def adjust_rate(self, success: bool) -> None:
        """Adjust rate based on success/failure feedback

        Args:
            success: Whether the operation was successful
        """
        if not self.adaptive:
            return

        with self.lock:
            if success:
                self.consecutive_success += 1
                self.consecutive_failures = 0

                # Gradually increase rate after sustained success
                if self.consecutive_success >= 10 and self.rate < self.original_rate * 2:
                    self.rate *= 1.1
                    self.consecutive_success = 0
            else:
                self.consecutive_failures += 1
                self.consecutive_success = 0

                # Quickly decrease rate after failures
                if self.consecutive_failures >= 3:
                    self.rate = max(self.original_rate * 0.1, self.rate * 0.5)
                    self.consecutive_failures = 0

    def reset(self) -> None:
        """Reset bucket to initial state"""
        with self.lock:
            self.tokens = float(self.burst)
            self.rate = self.original_rate
            self.consecutive_success = 0
            self.consecutive_failures = 0
            self.last_update = time.time()
            self.last_adjustment = time.time()

    def get_stats(self) -> Dict[str, float]:
        """Get current bucket statistics

        Returns:
            Dict[str, float]: Statistics including rate, tokens, burst, utilization
        """
        with self.lock:
            # Update tokens to current time
            now = time.time()
            elapsed = now - self.last_update
            current_tokens = min(self.burst, self.tokens + elapsed * self.rate)

            return {
                "rate": self.rate,
                "burst": float(self.burst),
                "tokens": current_tokens,
                "utilization": (self.burst - current_tokens) / self.burst if self.burst > 0 else 0.0,
                "consecutive_success": float(self.consecutive_success),
                "consecutive_failures": float(self.consecutive_failures),
                "adaptive": 1.0 if self.adaptive else 0.0,
                "original_rate": self.original_rate,
            }


@dataclass
class ResourceUsage:
    """Type-safe resource usage metrics"""

    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    disk_mb: float = 0.0
    network_kb: float = 0.0
    active_connections: int = 0

    def __post_init__(self):
        """Validate resource usage after initialization"""
        self.validate()

    def validate(self) -> None:
        """Validate resource usage metrics"""
        if self.cpu_percent < 0:
            raise ValueError("cpu_percent cannot be negative")
        if self.memory_mb < 0:
            raise ValueError("memory_mb cannot be negative")
        if self.disk_mb < 0:
            raise ValueError("disk_mb cannot be negative")
        if self.network_kb < 0:
            raise ValueError("network_kb cannot be negative")
        if self.active_connections < 0:
            raise ValueError("active_connections cannot be negative")


@dataclass
class HealthStatus:
    """Type-safe health status"""

    healthy: bool
    component: str
    message: str = ""
    last_check: float = 0.0
    check_count: int = 0
    error_count: int = 0

    @property
    def error_rate(self) -> float:
        """Calculate error rate"""
        if self.check_count == 0:
            return 0.0
        return self.error_count / self.check_count

    def is_degraded(self, max_error_rate: float = 0.1) -> bool:
        """Check if component is in degraded state"""
        return self.error_rate > max_error_rate


def inherit_patterns(parent: Patterns, condition: Condition) -> None:
    """Inherit global patterns to condition if fields are empty"""
    if not parent or not isinstance(parent, Patterns) or not condition or not isinstance(condition, Condition):
        return

    if not condition.patterns.key_pattern:
        condition.patterns.key_pattern = parent.key_pattern
    if not condition.patterns.address_pattern:
        condition.patterns.address_pattern = parent.address_pattern
    if not condition.patterns.endpoint_pattern:
        condition.patterns.endpoint_pattern = parent.endpoint_pattern
    if not condition.patterns.model_pattern:
        condition.patterns.model_pattern = parent.model_pattern


# Task result types
@dataclass
class SearchTaskResult:
    """Result from search task execution"""

    links: List[str] = field(default_factory=list)
    total: Optional[int] = None

    def is_successful(self) -> bool:
        """Check if search was successful"""
        return len(self.links) > 0

    def get_summary(self) -> str:
        """Get result summary"""
        return f"Found {len(self.links)} links" + (f" out of {self.total}" if self.total else "")


@dataclass
class AcquisitionTaskResult:
    """Result from acquisition task execution"""

    services: List["Service"] = field(default_factory=list)

    def is_successful(self) -> bool:
        """Check if collection was successful"""
        return len(self.services) > 0

    def get_summary(self) -> str:
        """Get result summary"""
        return f"Collected {len(self.services)} services"


@dataclass
class CheckTaskResult:
    """Result from check task execution"""

    valid: List["Service"] = field(default_factory=list)
    invalid: List["Service"] = field(default_factory=list)
    no_quota: List["Service"] = field(default_factory=list)
    wait_check: List["Service"] = field(default_factory=list)

    def count(self) -> int:
        """Get total number of keys processed"""
        return len(self.valid) + len(self.invalid) + len(self.no_quota) + len(self.wait_check)

    def get_success_rate(self) -> float:
        """Get success rate of key validation"""
        total = self.count()
        return len(self.valid) / total if total > 0 else 0.0

    def get_summary(self) -> str:
        """Get result summary"""
        return f"Checked {self.count()} keys: {len(self.valid)} valid, {len(self.invalid)} invalid"


@dataclass
class InspectTaskResult:
    """Result from inspect task execution"""

    models: List[str] = field(default_factory=list)

    def is_successful(self) -> bool:
        """Check if models listing was successful"""
        return len(self.models) > 0

    def get_summary(self) -> str:
        """Get result summary"""
        return f"Found {len(self.models)} models"
