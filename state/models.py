#!/usr/bin/env python3

"""
Core Status Models for Monitor Package

This module contains all unified data models that replace the scattered status models
throughout the system. It provides strong typing and eliminates dictionary-based data passing.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Dict, List, Optional

from core.enums import AlertKeyType, QueueStateStatus, SystemState
from core.metrics import BaseMetrics, PipelineStatus, TaskMetrics

from .enums import AlertLevel, ProviderState


@dataclass
class BaseKeyStats(BaseMetrics):
    """Base class for key-related statistics"""

    valid: int = 0
    invalid: int = 0
    no_quota: int = 0
    wait_check: int = 0
    material: int = 0

    @property
    def total(self) -> int:
        """Total key count calculation"""
        return self.valid + self.invalid + self.no_quota + self.wait_check

    @property
    def success_rate(self) -> float:
        """Key validation success rate"""
        return self.valid / self.total if self.total > 0 else 0.0

    @property
    def empty(self) -> bool:
        """Check if statistics are empty"""
        return self.total == 0


@dataclass
class StatsSource(BaseKeyStats):
    """Data structure for objects that provide statistics fields"""

    # Total number of links obtained
    links: int = 0

    # Number of models
    models: int = 0


@dataclass
class CheckCacheStats(BaseMetrics):
    """Per-run terminal-verdict cache accounting for CheckStage.

    Every routed check verdict is exactly one live probe or one cache hit, so
    ``live_probes + cache_hits`` reconciles with the verdict buckets
    (``total_keys_checked``). The per-verdict counters split cache hits by the
    stored terminal verdict. Observability only - never a verdict source.
    """

    live_probes: int = 0
    cache_hits: int = 0
    cache_hits_invalid: int = 0
    cache_hits_no_quota: int = 0
    store_failures: int = 0
    lookups: int = 0


@dataclass
class ProviderStatus:
    """Provider status information"""

    name: str
    state: ProviderState = ProviderState.UNKNOWN
    enabled: bool = True

    # Stage configuration
    searchable: bool = False
    gatherable: bool = False
    checkable: bool = False
    inspectable: bool = False

    # Source
    resource: StatsSource = field(default_factory=StatsSource)

    # API metrics
    calls: int = 0
    errors: int = 0
    limits: int = 0

    def abbreviations(self) -> str:
        """Get standardized stage abbreviations"""
        abbrev = []
        if self.searchable:
            abbrev.append("S")
        if self.gatherable:
            abbrev.append("G")
        if self.checkable:
            abbrev.append("V")
        if self.inspectable:
            abbrev.append("I")

        return "/".join(abbrev)

    @property
    def success_rate(self) -> float:
        """API call success rate"""
        if self.calls > 0:
            return (self.calls - self.errors) / self.calls

        return 0.0


@dataclass
class PersistenceMetrics(BaseMetrics):
    """Persistence metrics"""

    resource: StatsSource = field(default_factory=StatsSource)

    # Persistence-specific fields
    start: float = field(default_factory=time.time)
    last_save: float = field(default_factory=time.time)
    last_snapshot: float = field(default_factory=time.time)

    # Enhanced metrics
    snapshot_count: int = 0
    bad_line_count: int = 0
    repair_count: int = 0
    total_append_time: float = 0.0
    total_snapshot_time: float = 0.0
    append_operations: int = 0
    snapshot_operations: int = 0

    @property
    def success_rate(self) -> float:
        """Key validation success rate"""
        return self.keys.success_rate

    @property
    def avg_append_time(self) -> float:
        """Average append operation time in seconds"""
        return self.total_append_time / max(1, self.append_operations)

    @property
    def avg_snapshot_time(self) -> float:
        """Average snapshot operation time in seconds"""
        return self.total_snapshot_time / max(1, self.snapshot_operations)


@dataclass
class WorkerMetrics(BaseMetrics):
    """Worker thread metrics for load balancer and performance monitoring

    This class tracks performance metrics for individual worker threads
    in pipeline stages, enabling dynamic load balancing decisions and
    system status monitoring.
    """

    stage: str = ""
    current_workers: int = 0
    target_workers: int = 0
    queue_size: int = 0
    processing_rate: float = 0.0
    avg_processing_time: float = 0.0
    success_rate: float = 1.0
    error_count: int = 0
    utilization: float = 0.0
    last_adjustment: float = 0.0
    last_updated: float = field(default_factory=time.monotonic)  # Use monotonic for intervals

    # Additional fields for system status
    active: int = 0
    total: int = 0
    idle: int = 0
    busy: int = 0

    def get_utilization(self) -> float:
        """Worker utilization rate"""
        return self.busy / self.total if self.total > 0 else self.utilization

    def update_metrics(
        self,
        queue_size: int,
        processing_rate: float,
        avg_time: float,
        success_rate: float = 1.0,
        error_count: int = 0,
    ) -> None:
        """Update worker metrics with new values"""
        self.queue_size = queue_size
        self.processing_rate = processing_rate
        self.avg_processing_time = avg_time
        self.success_rate = success_rate
        self.error_count = error_count
        self.last_updated = time.monotonic()

    def calculate_load_score(self) -> float:
        """Calculate load score for load balancing decisions"""
        if self.current_workers == 0:
            return float("inf")

        queue_factor = self.queue_size / max(self.current_workers, 1)
        time_factor = self.avg_processing_time
        error_factor = 1.0 - self.success_rate

        return queue_factor * (1.0 + time_factor + error_factor)


@dataclass
class BasePerformanceStats(BaseMetrics):
    """Base class for performance-related statistics"""

    throughput: float = 0.0
    success_rate: float = 0.0
    error_rate: float = 0.0

    def calculate_rates(self, completed: int, failed: int, runtime: float) -> None:
        """Calculate performance rates from task counts"""
        if runtime > 0:
            self.throughput = completed / runtime

        total_processed = completed + failed
        if total_processed > 0:
            self.success_rate = completed / total_processed
            self.error_rate = failed / total_processed


@dataclass
class PerformanceMetrics(BasePerformanceStats):
    """Performance metrics extending base performance statistics"""

    tasks_per_second: float = 0.0
    avg_response_time: float = 0.0

    def calculate_derived_metrics(self, tasks: TaskMetrics, runtime: float) -> None:
        """Calculate derived performance metrics"""
        self.calculate_rates(tasks.completed, tasks.failed, runtime)
        self.tasks_per_second = self.throughput


@dataclass
class QueueStateMetrics(BaseMetrics):
    """Queue state metrics for individual stage monitoring and persistence"""

    stage: str = ""
    tasks: int = 0
    saved_at: datetime = field(default_factory=datetime.now)
    age_hours: float = 0.0
    file_size: int = 0
    status: QueueStateStatus = QueueStateStatus.ACTIVE
    last_operation: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def is_healthy(self) -> bool:
        """Check if queue is in healthy state"""
        return self.status in (QueueStateStatus.ACTIVE, QueueStateStatus.EMPTY)

    @property
    def is_stale(self) -> bool:
        """Check if queue state is stale (older than 24 hours)"""
        return self.age_hours > 24.0

    def calculate_age(self) -> None:
        """Calculate age in hours from saved_at timestamp"""
        now = datetime.now()
        delta = now - self.saved_at
        self.age_hours = delta.total_seconds() / 3600.0


@dataclass
class BaseTaskStats(BaseMetrics):
    """Base class for task-related statistics"""

    tasks: int = 0
    completed: int = 0
    failed: int = 0

    @property
    def total_processed(self) -> int:
        """Total processed tasks"""
        return self.completed + self.failed

    @property
    def success_rate(self) -> float:
        """Task success rate"""
        return self.completed / self.total_processed if self.total_processed > 0 else 0.0

    @property
    def error_rate(self) -> float:
        """Task error rate"""
        return self.failed / self.total_processed if self.total_processed > 0 else 0.0


@dataclass
class MonitoringSummary(BaseTaskStats):
    """Monitoring summary data extending base task statistics"""

    # Performance fields
    throughput: float = 0.0
    runtime: float = 0.0

    # Resource fields
    links: int = 0
    keys: int = 0

    def update_performance_metrics(self) -> None:
        """Update performance metrics based on task statistics"""
        if self.runtime > 0:
            self.throughput = self.completed / self.runtime


@dataclass
class MonitoringSnapshot(BaseMetrics):
    """Monitoring snapshot"""

    runtime: float = 0.0
    pipeline: Optional[PipelineStatus] = None
    providers: Dict[str, ProviderStatus] = field(default_factory=dict)
    summary: PerformanceMetrics = field(default_factory=PerformanceMetrics)

    @classmethod
    def create_from_monitoring(cls, monitoring: "IMonitorProvider") -> "MonitoringSnapshot":
        """Create snapshot from monitoring system using abstract interface methods"""
        summary = PerformanceMetrics()
        runtime = monitoring.runtime()

        # Get provider and pipeline stats through abstract interface
        provider_stats = monitoring.get_provider_status()
        pipeline_stats = monitoring.get_pipeline_status()

        # Calculate summary metrics from provider stats
        total_tasks = sum(p.calls for p in provider_stats.values())
        total_errors = sum(p.errors for p in provider_stats.values())

        summary.tasks_per_second = total_tasks / max(runtime, 1)
        summary.error_rate = total_errors / max(total_tasks, 1)
        summary.success_rate = 1.0 - summary.error_rate

        # Use MappingProxyType for zero-copy read-only access to provider stats
        providers_readonly = MappingProxyType(provider_stats)

        return cls(
            runtime=runtime,
            pipeline=pipeline_stats,
            providers=providers_readonly,
            summary=summary,
        )


@dataclass
class Alert:
    """Strong-typed alert"""

    type: AlertKeyType
    level: AlertLevel
    message: str
    timestamp: float
    source: str = ""
    context: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def create_system_alert(cls, level: AlertLevel, message: str, source: str = "system") -> "Alert":
        """Create a system alert"""
        return cls(type=AlertKeyType.SYSTEM, level=level, message=message, timestamp=time.time(), source=source)

    @classmethod
    def create_performance_alert(cls, level: AlertLevel, message: str, metric_name: str, value: float) -> "Alert":
        """Create a performance alert"""
        return cls(
            type=AlertKeyType.PERFORMANCE,
            level=level,
            message=message,
            timestamp=time.time(),
            source="performance",
            context={"metric": metric_name, "value": str(value)},
        )

    def is_critical(self) -> bool:
        """Check if alert is critical"""
        return self.level == AlertLevel.CRITICAL

    def age_seconds(self) -> float:
        """Get alert age in seconds"""
        return time.time() - self.timestamp


@dataclass
class SystemStatus(BaseMetrics):
    """
    Unified system status data model
    """

    # Basic information
    runtime: float = 0.0
    state: SystemState = SystemState.UNKNOWN

    # Core metrics
    tasks: TaskMetrics = field(default_factory=TaskMetrics)
    resource: StatsSource = field(default_factory=StatsSource)
    performance: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    worker: WorkerMetrics = field(default_factory=WorkerMetrics)

    # Component status
    providers: Dict[str, ProviderStatus] = field(default_factory=dict)
    pipeline: PipelineStatus = field(default_factory=PipelineStatus)

    # Alerts and events
    alerts: List[Alert] = field(default_factory=list)

    # Application state
    monitored: bool = False
    balanced: bool = False

    def abbreviations(self, name: str) -> str:
        """Get stage abbreviations for a provider"""
        if name not in self.providers:
            return ""

        return self.providers[name].abbreviations()

    def add_provider(self, status: ProviderStatus) -> None:
        """Add a provider to the system status"""
        self.providers[status.name] = status

    def active_providers(self) -> List[ProviderStatus]:
        """Get list of active providers"""
        return [p for p in self.providers.values() if p.state == ProviderState.ACTIVE]

    def critical_alerts(self) -> List[Alert]:
        """Get critical alerts"""
        return [alert for alert in self.alerts if alert.is_critical()]

    def calculate_overall_metrics(self) -> None:
        """Calculate overall system metrics from component metrics"""
        # Aggregate provider metrics
        total = StatsSource()

        for provider in self.providers.values():
            total.valid += provider.resource.valid
            total.invalid += provider.resource.invalid
            total.no_quota += provider.resource.no_quota
            total.wait_check += provider.resource.wait_check
            total.material += provider.resource.material

            total.links += provider.resource.links
            total.models += provider.resource.models

        self.resource = total

        # Calculate performance metrics
        self.performance.calculate_derived_metrics(self.tasks, self.runtime)

    def has_pipeline_data(self) -> bool:
        """Check if pipeline data is available"""
        return bool(self.pipeline.stages)

    def has_provider_data(self) -> bool:
        """Check if provider data is available"""
        return bool(self.providers)

    def has_alerts(self) -> bool:
        """Check if there are any alerts"""
        return bool(self.alerts)

    def healthy(self) -> bool:
        """Check if system is in healthy state"""
        return (
            self.state in (SystemState.RUNNING, SystemState.STOPPED)
            and not self.critical_alerts()
            and self.performance.error_rate < 0.1
        )


@dataclass
class StageWorkerStatus:
    """Worker statistics for a single stage"""

    current_workers: int
    target_workers: int
    queue_size: int
    utilization: float
    processing_rate: float
    last_adjustment: float


@dataclass
class WorkerStatus:
    """Overall worker management statistics"""

    timestamp: float
    stages: Dict[str, StageWorkerStatus]
    total_workers: int
    total_target_workers: int
    total_queue_size: int
    status: str = "ok"
    error: Optional[str] = None


@dataclass
class ApplicationStatus(SystemStatus):
    """Application-specific status extending SystemStatus"""

    shutdown_requested: bool = False
    task_manager_status: Optional[SystemStatus] = None
    monitoring_status: Optional[MonitoringSummary] = None
    worker_manager_status: Optional[WorkerStatus] = None


@dataclass
class CacheStats:
    """Cache statistics for monitoring and debugging"""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    size: int = 0

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class IMonitorProvider(ABC):
    """Abstract base class for monitoring data providers"""

    @abstractmethod
    def summary(self) -> MonitoringSummary:
        """Get monitoring summary metrics

        Returns:
            MonitoringSummary: Aggregated metrics including task counts,
                              completion rates, throughput, and performance
        """
        pass

    @abstractmethod
    def snapshot(self) -> MonitoringSnapshot:
        """Get current monitoring snapshot

        Returns:
            MonitoringSnapshot: Real-time system snapshot with pipeline
                               status, provider states, and performance data
        """
        pass

    @abstractmethod
    def ingest(self, system_stats: SystemStatus) -> None:
        """Ingest task statistics for monitoring aggregation

        Args:
            system_stats: System status from task manager to process and cache
        """
        pass

    @abstractmethod
    def runtime(self) -> float:
        """Get runtime in seconds

        Returns:
            float: Current runtime in seconds
        """
        pass

    @abstractmethod
    def get_provider_status(self) -> Dict[str, ProviderStatus]:
        """Get provider statistics

        Returns:
            Dict[str, ProviderStatus]: Dictionary mapping provider names to their status
        """
        pass

    @abstractmethod
    def get_pipeline_status(self) -> Optional[PipelineStatus]:
        """Get pipeline statistics

        Returns:
            Optional[PipelineStatus]: Current pipeline status or None if not available
        """
        pass
