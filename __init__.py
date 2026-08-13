#!/usr/bin/env python3

"""
Harvester - Universal Data Acquisition Framework

A comprehensive, adaptive data acquisition framework designed for multi-source
information gathering from GitHub, network mapping platforms, and arbitrary
web endpoints. Built with a focus on AI service provider key discovery while
maintaining extensibility for diverse data acquisition scenarios.

Key Features:
- Multi-source data acquisition (GitHub API/Web, FOFA, Shodan)
- Asynchronous pipeline processing with configurable stages
- Intelligent rate limiting and adaptive throttling
- Real-time monitoring and metrics collection
- Graceful shutdown and recovery mechanisms
- Type-safe configuration management
- Extensible plugin architecture

Quick Start:
    from harvester import HarvesterApp, load_config

    # Load configuration
    config = load_config("config.yaml")

    # Create and run application
    app = HarvesterApp(config)
    app.run()

Architecture:
    - Core: Business logic, domain models, and task definitions
    - Manager: Pipeline, task, queue, and worker management
    - Storage: Data persistence, recovery, and atomic operations
    - Search: Provider implementations and search clients
    - Stage: Pipeline stage definitions and processing logic
    - State: System state management and monitoring
    - Tools: Utilities, logging, rate limiting, and coordination
    - Config: Unified configuration management system

For detailed documentation, see README.md or visit:
https://github.com/wzdnzd/harvester
"""

__version__ = "1.0.0"
__author__ = "Harvester Development Team"
__license__ = "MIT"
__description__ = "Universal Data Acquisition Framework"

# Configuration management
from config import Config, get_config, load_config, reload_config

# Core domain models and types
from core import (  # Task types; Enums; Interfaces; Exceptions
    AcquisitionTask,
    BusinessLogicError,
    CheckTask,
    ConfigurationError,
    CoreException,
    ErrorReason,
    IAuthProvider,
    InspectTask,
    IPipelineStats,
    IProvider,
    NetworkError,
    ProcessingError,
    ProviderTask,
    ResultType,
    SearchTask,
    SystemState,
    ValidationError,
)

# Management components
from manager import (
    Pipeline,
    QueueManager,
    TaskManager,
    WorkerManager,
    create_task_manager,
    create_worker_manager,
)

# State management and monitoring
from state import StatusBuilder, StatusCollector, SystemStatus

# Storage and persistence
from storage import (
    AtomicFileWriter,
    ResultManager,
    SnapshotManager,
)

# Utilities and tools
from tools import Balancer, ExponentialBackoff, RateLimiter, RetryPolicy, get_logger

# Core application components
from .main import HarvesterApp

# Export main application interface
__all__ = [
    # Version and metadata
    "__version__",
    "__author__",
    "__license__",
    "__description__",
    # Main application
    "HarvesterApp",
    # Configuration
    "Config",
    "load_config",
    "get_config",
    "reload_config",
    # Core domain models
    "ProviderTask",
    "SearchTask",
    "AcquisitionTask",
    "CheckTask",
    "InspectTask",
    # Enums
    "SystemState",
    "ResultType",
    "ErrorReason",
    # Interfaces
    "IProvider",
    "IAuthProvider",
    "IPipelineStats",
    # Exceptions
    "CoreException",
    "BusinessLogicError",
    "ConfigurationError",
    "NetworkError",
    "ProcessingError",
    "ValidationError",
    # Management
    "Pipeline",
    "TaskManager",
    "QueueManager",
    "WorkerManager",
    "create_task_manager",
    "create_worker_manager",
    # Storage
    "ResultManager",
    "AtomicFileWriter",
    "SnapshotManager",
    # State management
    "SystemStatus",
    "StatusBuilder",
    "StatusCollector",
    # Utilities
    "get_logger",
    "RateLimiter",
    "RetryPolicy",
    "ExponentialBackoff",
    "Balancer",
]
