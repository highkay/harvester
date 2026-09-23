#!/usr/bin/env python3

"""
Main application class for the async pipeline system.
Integrates all components: task management, monitoring, load balancing, and graceful shutdown.
"""

import argparse
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, List, Optional

import yaml

from config import load_config
from config.defaults import get_default_config
from config.schemas import Config, WorkerManagerConfig
from constant.system import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_STATS_INTERVAL,
    FORCE_EXIT_GRACE_PERIOD,
)
from core.enums import PipelineStage, SystemState
from manager.base import LifecycleManager
from manager.shutdown import ShutdownCoordinator
from manager.status import StatusManager
from manager.task import TaskManager
from manager.worker import WorkerManager, create_worker_manager
from state.collector import StatusCollector
from state.enums import DisplayMode, StatusContext
from state.models import ApplicationStatus, WorkerMetrics
from state.monitor import ProviderMonitoring, create_monitoring
from tools.coordinator import init_managers
from tools.logger import flush_logs, get_logger, init_logging
from tools.utils import handle_exceptions

logger = get_logger("main")


class HarvesterApp:
    """Main application class for the async pipeline system"""

    def __init__(self, config_path: str = DEFAULT_CONFIG_FILE) -> None:
        self.config_path = config_path
        self.config: Optional[Config] = None
        self.task_manager: Optional[TaskManager] = None
        self.monitoring: Optional[ProviderMonitoring] = None
        self.worker_manager: Optional[WorkerManager] = None
        self.status_collector: Optional[StatusCollector] = None
        self.status_manager: Optional[StatusManager] = None

        # Application state
        self.running = False
        self.start_time = 0.0
        self._initialized = False

        # Enhanced shutdown control
        self.shutdown_event = threading.Event()
        self.force_shutdown = False
        self.signal_count = 0
        self.shutdown_lock = threading.Lock()

        # New coordinated components
        self.shutdown_coordinator: Optional[ShutdownCoordinator] = None

        # Statistics
        self.stats_interval = float(DEFAULT_STATS_INTERVAL)

        # Status display style (default to classic per user preference)
        self.display_style = "classic"

        logger.info(f"Initialized application with config: {config_path}")

    def initialize(self) -> bool:
        """Initialize all application components (idempotent).

        web/runner.py calls initialize() and then run(), which calls
        initialize() again. Re-initializing rebuilt the whole world: the
        config was parsed twice, init_managers()/set_proxy/
        configure_github_transport/init_github_client ran twice (rebuilding
        the shared GitHub client and resetting adaptive backoff), a second
        pair of sqlite connections was opened (LinkIndex/ResponseCache have
        no production close() callers), and the TaskManager that callers had
        registered completion listeners on was silently DISCARDED. Once
        initialization has succeeded, later calls are truthful no-ops; a
        failed initialization does not latch, so a retry still runs.
        """
        if self._initialized:
            logger.info("Application already initialized, skipping re-initialization")
            return True

        try:
            # Load configuration
            self.config = load_config(self.config_path)
            logger.info("Configuration loaded successfully")

            # Initialize global resource managers
            init_managers()
            logger.info("Global resource managers initialized")

            # Create task manager
            self.task_manager = TaskManager(self.config)
            logger.info(f"Task manager created with {len(self.task_manager.providers)} providers")

            # Create monitoring system using monitoring config (without status manager yet)
            monitoring_config = self.config.monitoring
            self.monitoring = create_monitoring(monitoring_config)
            logger.info("Monitoring system created")

            # Create worker manager only if enabled
            if self.config.worker.enabled:
                worker_manager_config = WorkerManagerConfig(
                    enabled=self.config.worker.enabled,
                    min_workers=self.config.worker.min_workers,
                    max_workers=self.config.worker.max_workers,
                    target_queue_size=self.config.worker.target_queue_size,
                    adjustment_interval=self.config.worker.adjustment_interval,
                    scale_up_threshold=self.config.worker.scale_up_threshold,
                    scale_down_threshold=self.config.worker.scale_down_threshold,
                    log_recommendations=self.config.worker.log_recommendations,
                )
                self.worker_manager = create_worker_manager(
                    worker_manager_config, shutdown_timeout=float(self.config.persistence.shutdown_timeout)
                )
                logger.info("Worker manager created")
            else:
                self.worker_manager = None
                logger.info("Worker manager disabled in configuration")

            # Register pipeline stages with worker manager (if enabled)
            if self.worker_manager and self.task_manager.pipeline:
                pipeline = self.task_manager.pipeline
                for stage in PipelineStage:
                    stage_instance = pipeline.get_stage(stage.value)
                    if stage_instance:
                        self.worker_manager.register_stage(stage.value, stage_instance)

            # Create status collector with monitoring dependency only
            self.status_collector = StatusCollector(monitoring=self.monitoring)
            logger.info("Status collector created")

            # Initialize unified status manager as scheduling entry point
            self.status_manager = StatusManager(
                collector=self.status_collector,
                task_provider=self.task_manager,
                display_interval=self.stats_interval,
            )
            logger.info("Status manager initialized as scheduling entry point")

            # Initialize shutdown coordinator
            components: List[LifecycleManager] = [self.task_manager, self.status_manager]
            if self.worker_manager:
                components.append(self.worker_manager)
            self.shutdown_coordinator = ShutdownCoordinator(
                components=components,
                shutdown_timeout=float(self.config.persistence.shutdown_timeout),
                monitor_interval=self.config.monitoring.update_interval,
            )
            logger.info("Shutdown coordinator initialized")

            # Register completion event listeners
            if self.worker_manager:
                self.task_manager.add_completion_listener(self.worker_manager._on_task_completion)

            self.task_manager.add_completion_listener(self.status_manager._on_task_completion)
            logger.info("Completion event listeners registered")

            logger.info("Application initialization completed")
            self._initialized = True
            return True

        except Exception as e:
            logger.error(f"Application initialization failed: {e}")
            return False

    def run(self) -> bool:
        """Main run method with improved shutdown handling"""
        try:
            # Setup signal handlers first
            self._setup_signal_handlers()

            if not self.initialize():
                return False

            self.running = True
            self.start_time = time.time()

            # Start components with error handling
            try:
                logger.info("Starting application components...")

                if self.worker_manager:
                    self.worker_manager.start()
                    logger.debug("Worker manager started")

                self.task_manager.start()
                logger.debug("Task manager started")

                # Start status manager as the new scheduling entry point
                self.status_manager.start()
                logger.debug("Status manager started as scheduling entry point")

            except Exception as e:
                logger.error(f"Failed to start components: {e}")
                return False

            logger.info("Application started successfully. You can press Ctrl+C twice to stop gracefully")

            # Start coordinated components
            self.shutdown_coordinator.start_completion_monitor()

            # Simplified main loop using shutdown coordinator
            while self.running:
                # Check for shutdown conditions
                if self.shutdown_event.is_set() or self.force_shutdown:
                    break

                # Check if shutdown coordinator signaled completion
                if self.shutdown_coordinator.is_shutdown_requested():
                    logger.info("Shutdown coordinator signaled completion")
                    break

                # Interruptible wait
                if self.shutdown_event.wait(timeout=0.5):
                    break

            logger.info("Main loop exited")
            return True

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received in main thread")
            self.shutdown_event.set()
            return False
        except Exception as e:
            logger.error(f"Application error: {e}")
            traceback.print_exc()
            return False
        finally:
            self._graceful_shutdown()

    def _graceful_shutdown(self) -> None:
        """Enhanced graceful shutdown using shutdown coordinator"""
        with self.shutdown_lock:
            if not self.running:
                return

            self.running = False
            self.shutdown_event.set()

            # Use shutdown coordinator for graceful component shutdown
            if self.shutdown_coordinator:
                success = self.shutdown_coordinator.graceful_shutdown()
                if success:
                    logger.info("Graceful shutdown completed successfully")
                else:
                    logger.warning("Graceful shutdown completed with some errors")
            else:
                logger.warning("No shutdown coordinator available, using fallback")
                self._fallback_shutdown()

    def _fallback_shutdown(self) -> None:
        """Fallback shutdown method when coordinator is not available"""
        components: List[LifecycleManager] = [
            self.task_manager,
            self.worker_manager,
            self.status_manager,
        ]

        for component in components:
            if component and component.is_running:
                try:
                    logger.info(f"Stopping {component.name}")
                    component.stop()
                    logger.info(f"{component.name} stopped")
                except Exception as e:
                    logger.error(f"Error stopping {component.name}: {e}")

    def _force_exit(self) -> None:
        """Force exit the application"""
        logger.warning("Force exiting application due to shutdown timeout")
        os._exit(1)

    def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """Wait for application to complete processing using shutdown coordinator"""
        if not self.running or not self.shutdown_coordinator:
            return False

        try:
            return self.shutdown_coordinator.wait_for_completion(timeout)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received in wait_for_completion")
            self.shutdown_event.set()
            return False

    def get_status(self) -> ApplicationStatus:
        """Get comprehensive application status"""
        try:
            status = ApplicationStatus(
                shutdown_requested=self.shutdown_event.is_set(),
            )

            # Set inherited SystemStatus fields
            status.timestamp = time.time()
            status.runtime = time.time() - self.start_time if self.start_time > 0 else 0
            status.state = SystemState.RUNNING if self.running else SystemState.STOPPED

            # Get task manager status
            if self.task_manager:
                try:
                    status.task_manager_status = self.task_manager.stats()
                except Exception as e:
                    status.task_manager_status = {"error": str(e)}

            # Get monitoring status
            if self.monitoring:
                try:
                    status.monitoring_status = self.monitoring.summary()
                except Exception as e:
                    status.monitoring_status = {"error": str(e)}

            # Get worker manager status
            if self.worker_manager:
                try:
                    status.worker_manager_status = self.worker_manager.get_worker_stats()
                except Exception as e:
                    status.worker_manager_status = {"error": str(e)}

            return status
        except Exception:
            raise

    def _update_worker_manager_metrics(self) -> None:
        """Update worker manager with current metrics"""
        if not self.worker_manager or not self.task_manager:
            return

        try:
            tm_stats = self.task_manager.stats()

            if tm_stats.pipeline_stats:
                pipeline_stats = tm_stats.pipeline_stats

                # Update metrics for each stage that exists
                for stage in PipelineStage:
                    stage_stats = pipeline_stats.get_stage_stats(stage.value)
                    if stage_stats:

                        metrics_update = WorkerMetrics(
                            stage=stage.value,
                            queue_size=stage_stats.queue_size,
                            current_workers=stage_stats.workers,
                            processing_rate=stage_stats.processing_rate,
                            avg_processing_time=0.0,
                        )
                        self.worker_manager.update_metrics(stage.value, metrics_update)

        except Exception as e:
            logger.debug(f"Error updating worker manager metrics: {e}")

    def _setup_signal_handlers(self) -> None:
        """Set up enhanced signal handlers for graceful and force shutdown"""

        def signal_handler(signum: int, frame: Optional[Any]) -> None:
            _ = frame  # Suppress unused parameter warning

            self.signal_count += 1

            if self.signal_count == 1:
                logger.info(f"Received signal {signum}, initiating graceful shutdown...")
                logger.info(f"Press Ctrl+C again within {FORCE_EXIT_GRACE_PERIOD} seconds to force exit")
                self.shutdown_event.set()

                # Set up force exit timer for second signal
                def force_exit_on_second_signal():
                    time.sleep(FORCE_EXIT_GRACE_PERIOD)
                    if self.signal_count >= 2:
                        logger.warning("Force exit due to repeated signals")
                        self._force_exit()

                force_timer = threading.Thread(target=force_exit_on_second_signal, daemon=True)
                force_timer.start()

            elif self.signal_count >= 2:
                logger.warning(f"Received signal {signum} again, forcing immediate exit...")
                self.force_shutdown = True
                self._force_exit()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)


@handle_exceptions(default_result=False, log_level="error")
def validate_config(config_path: str) -> bool:
    """Validate configuration file exists and is readable"""
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: Configuration file '{config_path}' not found.")
        return False

    if not config_file.is_file():
        print(f"Error: '{config_path}' is not a file.")
        return False

    # Try to load the configuration
    try:
        load_config(config_path)
        print(f"Configuration file '{config_path}' is valid.")
        return True
    except Exception as e:
        print(f"Error: Invalid configuration file '{config_path}': {e}")
        return False


@handle_exceptions(default_result=False, log_level="error")
def create_sample_config(output_path: str = DEFAULT_CONFIG_FILE) -> bool:
    """Create a sample configuration file using default configuration"""
    # Get default configuration
    config = get_default_config()

    # Create YAML content with comments
    content = yaml.dump(
        config,
        default_flow_style=False,
        sort_keys=False,
        indent=2,
        allow_unicode=True,
    )

    output_file = Path(output_path)
    output_file.write_text(content, encoding="utf-8")
    print(f"Sample configuration created at: {output_path}")
    return True


if __name__ == "__main__":
    # Parse command line arguments first to handle special commands
    parser = argparse.ArgumentParser(
        description="Async Pipeline System for Multi-Provider API Key Discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Run with default config.yaml
  %(prog)s -c custom.yaml           # Run with custom config
  %(prog)s --validate               # Validate configuration
  %(prog)s --create-config          # Create sample configuration
  %(prog)s --log-level DEBUG        # Enable debug logging
  %(prog)s --timeout 300            # Set maximum runtime
        """,
    )

    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_FILE, help=f"Configuration file path (default: {DEFAULT_CONFIG_FILE})"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )

    parser.add_argument("--timeout", type=float, help="Maximum runtime in seconds")

    parser.add_argument(
        "--stats-interval",
        type=float,
        default=float(DEFAULT_STATS_INTERVAL),
        help="Statistics display interval in seconds",
    )

    parser.add_argument("--validate", action="store_true", help="Validate configuration and exit")

    parser.add_argument("--create-config", action="store_true", help="Create sample configuration file and exit")

    args = parser.parse_args()

    # Handle special commands first
    if args.create_config:
        success = create_sample_config(args.config)
        sys.exit(0 if success else 1)

    if args.validate:
        success = validate_config(args.config)
        sys.exit(0 if success else 1)

    # Check if config file exists
    if not Path(args.config).exists():
        print(f"Error: Configuration file not found: {args.config}")
        print("Use --create-config to create a sample configuration")
        sys.exit(1)

    # Initialize logging with specified level
    init_logging(args.log_level)

    # Create and configure application
    app = HarvesterApp(args.config)

    # Set stats interval from command line
    app.stats_interval = args.stats_interval

    try:
        logger.info("Starting Async Pipeline Application")
        logger.info("=" * 60)

        # Run application with timeout handling
        if args.timeout:
            # Start timeout thread
            def timeout_handler():
                time.sleep(args.timeout)
                logger.warning(f"Processing timed out after {args.timeout} seconds")
                app.shutdown_event.set()

            timeout_thread = threading.Thread(target=timeout_handler, daemon=True)
            timeout_thread.start()

        # Run the application
        success = app.run()

        if success:
            if app.task_manager and app.task_manager.pipeline and app.task_manager.pipeline.is_finished():
                logger.info("Processing completed successfully!")
            elif args.timeout and app.shutdown_event.is_set():
                logger.info(f"Processing timed out after {args.timeout} seconds")
            else:
                logger.info("🛑 Processing stopped by user")
        else:
            logger.error("Application failed to run")

        # Print final status
        logger.info("Final Status:")
        try:
            if app.status_manager:
                app.status_manager.show_status(StatusContext.APPLICATION, DisplayMode.DETAILED)
        except Exception as e:
            logger.debug(f"Error showing final status: {e}")

    except KeyboardInterrupt:
        logger.info("🛑 Shutdown requested by user")

    except Exception as e:
        logger.error(f"Application error: {e}")
        traceback.print_exc()

    finally:
        logger.info("Shutting down...")
        try:
            # Get final status
            status = app.get_status()
            logger.info(f"Summary: Runtime {status.runtime:.1f}s")

            if status.monitoring_status:
                monitoring_stats = status.monitoring_status
                if isinstance(monitoring_stats, dict):
                    logger.info(
                        f"Results: {monitoring_stats.get('total_valid_keys', 0)} valid keys, "
                        f"{monitoring_stats.get('total_links', 0)} links processed"
                    )
        except Exception as e:
            logger.error(f"Could not retrieve final status: {e}")

        # Flush logs before exit
        try:
            flush_logs()
            logger.info("Logs flushed to disk")
        except Exception as e:
            logger.error(f"Error flushing logs: {e}")

        # Clean exit
        sys.exit(0)
