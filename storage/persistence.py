#!/usr/bin/env python3

"""
Result management system with real-time persistence.
Supports batch saving for keys, links, and other results with atomic file operations.
"""

import datetime
import json
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Union

from constant.runtime import RESULT_MAPPINGS
from core.enums import ResultType
from core.models import AllRecoveredTasks, RecoveredTasks, ResultStorage, Service
from core.types import IProvider
from state.models import PersistenceMetrics
from tools.logger import get_logger

from .atomic import AtomicFileWriter
from .strategies import ShardStrategy, SimpleFileStrategy, SnapshotManager

logger = get_logger("storage")


class ResultBuffer:
    """Optimized buffer for batching results before writing to files"""

    def __init__(self, result_type: str, batch_size: int = 100, flush_interval: float = 30.0):
        self.result_type = result_type
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self.buffer: deque = deque()
        self.last_flush = time.time()
        self.lock = threading.Lock()
        self._total_items = 0
        self._total_flushes = 0

    def add(self, item: Any) -> bool:
        """Add item to buffer. Returns True if buffer is full after adding."""
        with self.lock:
            self.buffer.append(item)
            self._total_items += 1
            return len(self.buffer) >= self.batch_size

    def flush(self) -> List[Any]:
        """Flush buffer and return items"""
        with self.lock:
            if not self.buffer:
                return []

            # Convert deque to list efficiently
            items = list(self.buffer)
            self.buffer.clear()
            self.last_flush = time.time()
            self._total_flushes += 1
            return items

    def get_stats(self) -> Dict[str, Union[str, int, float]]:
        """Get buffer statistics"""
        with self.lock:
            return {
                "result_type": self.result_type,
                "current_size": len(self.buffer),
                "batch_size": self.batch_size,
                "total_items": self._total_items,
                "total_flushes": self._total_flushes,
                "last_flush": self.last_flush,
            }

    def size(self) -> int:
        """Get current buffer size"""
        with self.lock:
            return len(self.buffer)

    def should_flush(self) -> bool:
        """Check if buffer should be flushed based on time interval"""
        with self.lock:
            return (time.time() - self.last_flush) >= self.flush_interval


class ResultManager:
    """Manages results for a single provider with real-time persistence"""

    def __init__(
        self,
        provider: IProvider,
        workspace: str,
        batch_size: int = 50,
        save_interval: float = 30.0,
        simple: bool = False,
        shutdown_timeout: float = 5.0,
    ):
        self.name = provider.name
        self.provider = provider
        self.workspace = workspace
        self.batch_size = batch_size
        self.save_interval = save_interval
        self.shutdown_timeout = float(max(1.0, shutdown_timeout))

        # Create provider directory
        self.directory = os.path.join(workspace, "providers", self.provider.result.folder)
        os.makedirs(self.directory, exist_ok=True)

        # Build file paths from provider instance using configuration mapping
        self.files = dict()
        for result_type, mapping in RESULT_MAPPINGS.items():
            filename = provider.result.filenames.get(mapping.filename, "")
            if not filename:
                continue
            self.files[result_type.value] = os.path.join(self.directory, filename)

        # Initialize persistence strategy based on mode
        if simple:
            self.strategy = SimpleFileStrategy(self.directory, self.files)
            self.snapshot_manager = None
        else:
            self.strategy = ShardStrategy(self.directory, self.files)
            # Create snapshot manager for non-summary result types
            result_types = [rt for rt in self.files.keys() if rt != "summary"]
            self.snapshot_manager = SnapshotManager(self.directory, result_types, self.name)

        # Result buffers
        self.buffers = {
            result_type: ResultBuffer(result_type, batch_size, save_interval)
            for result_type in self.files.keys()
            if result_type != "summary"
        }

        # Models data (not buffered, updated directly)
        self.models_data: Dict[str, List[str]] = {}

        # Statistics
        self.stats = PersistenceMetrics()

        # Thread safety
        self.lock = threading.Lock()

        # Start periodic flush thread
        self.running = True
        self.flush_thread = threading.Thread(target=self._periodic_flush, daemon=True)
        self.flush_thread.start()

        logger.info(f"Initialized result manager for provider: {self.name}, mode: {'simple' if simple else 'shard'}")

    def add_result(self, result_type: str, data: Any):
        """Add result to appropriate buffer

        Args:
            result_type: Result type string (enum value)
            data: Data to add (single item or list)
        """
        if result_type not in self.buffers:
            logger.error(f"[persist] unknown result type: {result_type}")
            return

        # Handle different data types
        items = []
        if isinstance(data, list):
            items = data
        else:
            items = [data]

        # Add to buffer and check if flush is needed
        buffer = self.buffers[result_type]
        needs_flush = False

        for item in items:
            if buffer.add(item):
                needs_flush = True

        # Update statistics using configuration mapping
        self._update_statistics(result_type, len(items))

        # Immediate flush if needed
        if needs_flush:
            self._flush_buffer(result_type)

        logger.debug(f"[persist] added {len(items)} {result_type} for {self.name}")

    def _update_statistics(self, result_type: str, count: int):
        """Update statistics for given result type using configuration mapping"""
        with self.lock:
            # Find matching result type configuration
            for rt, mapping in RESULT_MAPPINGS.items():
                if rt.value == result_type and mapping.stats:
                    # Update the corresponding statistics attribute
                    current = getattr(self.stats.resource, mapping.stats, 0)
                    setattr(self.stats.resource, mapping.stats, current + count)
                    break

    def add_links(self, links: List[str]):
        """Convenience method for adding links with validation"""
        if not links:
            return

        # Filter valid links
        valid_links = [link for link in links if link and isinstance(link, str) and link.startswith("http")]

        if valid_links:
            self.add_result(ResultType.LINKS.value, valid_links)
            logger.debug(f"[persist] added {len(valid_links)} links for {self.name}")

    def add_models(self, key: str, models: List[str]):
        """Add model list for a key (not buffered, saved immediately)"""
        with self.lock:
            self.models_data[key] = {"models": models, "timestamp": time.time()}
            self.stats.resource.models += 1

        # Save models data immediately
        self._save_models()
        logger.debug(f"[persist] added {len(models)} models for key in {self.name}")

    def flush_all(self):
        """Flush all buffers immediately"""
        for result_type in self.buffers.keys():
            self._flush_buffer(result_type)

        # Save models data
        self._save_models()

        logger.info(f"Flushed all buffers for {self.name}")

    def get_stats(self) -> PersistenceMetrics:
        """Get current statistics"""
        with self.lock:
            return self.stats

    def backup_existing_files(self) -> None:
        """Backup existing result files to timestamped folder"""

        # Check if any files exist
        existing_files = []
        for file_type, filepath in self.files.items():
            if os.path.exists(filepath):
                existing_files.append((file_type, filepath))

        if not existing_files:
            logger.debug(f"No existing files to backup for {self.name}")
            return

        # Create backup folder with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(self.directory, f"backup-{timestamp}")
        os.makedirs(backup_dir, exist_ok=True)

        # Move existing files to backup folder
        for file_type, filepath in existing_files:
            try:
                backup_path = os.path.join(backup_dir, os.path.basename(filepath))
                os.rename(filepath, backup_path)
                logger.debug(f"Backed up {file_type} file for {self.name}")
            except Exception as e:
                logger.error(f"Failed to backup {file_type} for {self.name}: {e}")

        logger.info(f"Backed up {len(existing_files)} files for {self.name} to {backup_dir}")

    def _process_links_data(self, obj: Dict[str, Any]) -> Optional[str]:
        """Process links data from NDJSON object.

        Args:
            obj: Parsed JSON object from shard file

        Returns:
            Valid URL string or None
        """
        # Accept either {"url": "..."} or {"value": "..."}
        url = obj.get("url") or obj.get("value")
        if isinstance(url, str) and url.startswith("http"):
            return url
        return None

    def _process_service_data(self, obj: Dict[str, Any]) -> Optional[Service]:
        """Process service data from NDJSON object.

        Args:
            obj: Parsed JSON object from shard file

        Returns:
            Valid Service object or None
        """
        try:
            # Try to deserialize as Service object
            if "value" in obj:
                # Handle {"value": "serialized_service_data"}
                return Service.deserialize(obj["value"])
            else:
                # Handle direct service object
                return Service.from_dict(obj)
        except Exception:
            return None

    def _process_shard_generic(
        self, filepath: str, processor_func, target_list: List, estimated_lines: Optional[int] = None
    ) -> int:
        """Generic shard file processor with custom data handler and deduplication.

        Args:
            filepath: Path to the shard file to process
            processor_func: Function to process each JSON object
            target_list: List to append processed items to
            estimated_lines: Optional estimated line count for debug logging

        Returns:
            Number of unique items processed from this file
        """
        seen_items = set()

        if estimated_lines:
            logger.debug(f"Processing shard {filepath} (estimated {estimated_lines} lines)")

        try:
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        processed_item = processor_func(obj)
                        if processed_item is not None and processed_item not in seen_items:
                            seen_items.add(processed_item)
                            target_list.append(processed_item)
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Failed to process shard file {filepath}: {e}")

        return len(seen_items)

    def _recover_result_type(self, result_type: ResultType, target_list: List, processor_func) -> int:
        """Unified recovery flow for different result types.

        Args:
            result_type: Type of result to recover
            target_list: List to append recovered items to
            processor_func: Function to process each JSON object

        Returns:
            Number of items recovered
        """
        total = 0

        # Try shard files first
        shards_dir = os.path.join(self.directory, "shards", result_type.value)
        if os.path.exists(shards_dir) and os.path.isdir(shards_dir):
            try:
                # Use index to optimize recovery: skip empty shards, estimate work
                indexed_shards = []
                unindexed_shards = []

                for filename in sorted(os.listdir(shards_dir)):
                    if not filename.endswith(".ndjson"):
                        continue
                    shard_path = os.path.join(shards_dir, filename)
                    index_path = os.path.splitext(shard_path)[0] + ".index.json"

                    try:
                        with open(index_path, encoding="utf-8") as f:
                            index_data = json.load(f)
                            lines = int(index_data.get("lines", 0))
                            if lines > 0:  # Skip empty shards
                                indexed_shards.append((shard_path, index_data))
                    except Exception:
                        unindexed_shards.append(shard_path)

                # Process indexed shards first (sorted by timestamp)
                indexed_shards.sort(key=lambda x: x[1].get("first_ts", ""))

                for shard_path, index_data in indexed_shards:
                    estimated_lines = int(index_data.get("lines", 0))
                    count = self._process_shard_generic(shard_path, processor_func, target_list, estimated_lines)
                    total += count

                # Process non-indexed shards
                for shard_path in unindexed_shards:
                    count = self._process_shard_generic(shard_path, processor_func, target_list)
                    total += count

                if total > 0:
                    logger.info(f"Recovered {total} unique {result_type.value} items from shards for {self.name}")
                    return total
            except Exception as e:
                logger.error(f"Failed to read {result_type.value} shards for {self.name}: {e}")

        # Fallback: recover from legacy text file
        file_path = self.files.get(result_type.value)
        if file_path and os.path.exists(file_path):
            try:
                fallback_count = self._recover_from_legacy_file(file_path, result_type, target_list)
                if fallback_count > 0:
                    logger.info(
                        f"Recovered {fallback_count} unique {result_type.value} items from legacy file for {self.name}"
                    )
                    total += fallback_count
            except Exception as e:
                logger.error(f"Failed to read {result_type.value} legacy file for {self.name}: {e}")

        return total

    def _recover_from_legacy_file(self, file_path: str, result_type: ResultType, target_list: List) -> int:
        """Recover data from legacy text files with deduplication.

        Args:
            file_path: Path to the legacy file
            result_type: Type of result being recovered
            target_list: List to append recovered items to

        Returns:
            Number of unique items recovered
        """
        seen_items = set()

        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    processed_item = None
                    if result_type == ResultType.LINKS:
                        # Links are stored as plain URLs
                        if line.startswith("http"):
                            processed_item = line
                    elif result_type in (
                        ResultType.MATERIAL,
                        ResultType.INVALID,
                        ResultType.VALID,
                        ResultType.NO_QUOTA,
                        ResultType.WAIT_CHECK,
                    ):
                        # Services are stored as serialized objects or plain keys
                        service = self._deserialize_service(line)
                        if service:
                            processed_item = service

                    if processed_item is not None and processed_item not in seen_items:
                        seen_items.add(processed_item)
                        target_list.append(processed_item)
        except Exception as e:
            logger.error(f"Failed to process legacy file {file_path}: {e}")

        return len(seen_items)

    def recover_tasks(self) -> RecoveredTasks:
        """Recover tasks from existing result files and NDJSON shards.

        Supports recovery of:
        - acquisition_tasks from LINKS data
        - check_tasks from MATERIAL data
        - invalid_keys from INVALID data
        - valid_keys from VALID data (re-seeded after backup)

        Returns:
            RecoveredTasks with all recovered data
        """
        recovered = RecoveredTasks()

        # Recover acquisition tasks from LINKS
        # Set HARVESTER_SKIP_LINK_RECOVERY=1 to avoid re-inflating gather from links.txt
        # when resuming a drain-only run that already has a large gather queue.
        skip_links = str(os.environ.get("HARVESTER_SKIP_LINK_RECOVERY", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if skip_links:
            links_count = 0
            logger.info(f"Skipping LINKS recovery for {self.name} (HARVESTER_SKIP_LINK_RECOVERY)")
        else:
            links_count = self._recover_result_type(ResultType.LINKS, recovered.acquisition, self._process_links_data)

        # Recover check tasks from MATERIAL
        material_count = self._recover_result_type(ResultType.MATERIAL, recovered.check, self._process_service_data)

        # Recover invalid keys from INVALID (using a temporary list then converting to set)
        invalid_list = []
        invalid_count = self._recover_result_type(ResultType.INVALID, invalid_list, self._process_service_data)

        # Convert list to set for invalid_keys
        if invalid_list:
            recovered.invalid.update(invalid_list)

        # Recover previously validated keys so backup does not wipe valid-keys.txt
        valid_list: List = []
        valid_count = self._recover_result_type(ResultType.VALID, valid_list, self._process_service_data)
        if valid_list:
            recovered.valid.extend(valid_list)

        # Log recovery summary
        if links_count > 0 or material_count > 0 or invalid_count > 0 or valid_count > 0:
            logger.info(
                f"Recovery completed for {self.name}: "
                f"{links_count} acquisition tasks, "
                f"{material_count} check tasks, "
                f"{invalid_count} invalid keys, "
                f"{valid_count} valid keys"
            )
        else:
            logger.debug(f"No tasks recovered for {self.name}")

        return recovered

    def build_snapshot(self, result_type: str) -> int:
        """Build snapshot for specific result type."""
        if not self.snapshot_manager:
            return 0
        return self.snapshot_manager.build_snapshot(result_type)

    def build_all_snapshots(self) -> Dict[str, int]:
        """Build snapshots for all result types."""
        if not self.snapshot_manager:
            return {}
        return self.snapshot_manager.build_all_snapshots()

    def _deserialize_service(self, line: str) -> Optional[Service]:
        """Deserialize service object from string"""
        try:
            return Service.deserialize(line)
        except Exception as e:
            logger.warning(f"Failed to deserialize service: {e}")
            return None

    def stop(self):
        """Stop the result manager and flush all data, then build snapshots."""
        self.running = False

        # Stop periodic snapshot thread first to avoid concurrent builds
        try:
            self.stop_periodic_snapshot()
        except Exception as e:
            logger.error(f"[persist] failed to stop periodic snapshot for {self.name}: {e}")

        # Wait for flush thread to complete
        if self.flush_thread.is_alive():
            self.flush_thread.join(timeout=self.shutdown_timeout)

        # Flush all remaining data
        self.flush_all()

        # Build final snapshots if supported
        try:
            self.build_all_snapshots()
        except Exception as e:
            logger.error(f"[persist] failed to build snapshots for {self.name} on stop: {e}")

        # Cleanup strategy resources
        try:
            self.strategy.cleanup()
        except Exception as e:
            logger.error(f"[persist] failed to cleanup strategy for {self.name}: {e}")

        logger.info(f"Stopped result manager for {self.name}")

    def start_periodic_snapshot(self, interval_sec: int = 300) -> None:
        """Start periodic snapshot building."""
        if not self.snapshot_manager:
            return
        self.snapshot_manager.start_periodic(interval_sec)

    def stop_periodic_snapshot(self) -> None:
        """Stop periodic snapshot building."""
        if not self.snapshot_manager:
            return
        self.snapshot_manager.stop()

    def _periodic_flush(self):
        """Periodic flush thread"""
        while self.running:
            try:
                time.sleep(self.save_interval)

                # Check each buffer for time-based flush
                for result_type, buffer in self.buffers.items():
                    if buffer.size() > 0 and time.time() - buffer.last_flush >= self.save_interval:
                        self._flush_buffer(result_type)

            except Exception as e:
                logger.error(f"[persist] error in periodic flush for {self.name}: {e}")

    def _flush_buffer(self, result_type: str):
        """Flush a specific buffer using persistence strategy"""
        buffer = self.buffers.get(result_type)
        if not buffer:
            return

        items = buffer.flush()
        if not items:
            return

        try:
            # Delegate to persistence strategy
            self.strategy.write_data(result_type, items, self.stats)

            with self.lock:
                self.stats.last_save = time.time()

        except Exception as e:
            logger.error(f"[persist] failed to save {result_type} for {self.name}: {e}")

    def _save_models(self):
        """Save models data to JSON file"""
        if not self.models_data:
            return

        try:
            filepath = self.files.get(ResultType.SUMMARY.value)
            if not filepath:
                logger.debug(f"[persist] summary file not configured for {self.name}, skip models save")
                return

            # Unique models
            unique_models = set()
            for data in self.models_data.values():
                unique_models.update(data.get("models", []))

            total_models = len(unique_models)
            del unique_models

            # Prepare summary data
            summary = {
                "provider": self.name,
                "updated_at": time.time(),
                "models": self.models_data,
                "stats": {
                    "total_keys": len(self.models_data),
                    "total_models": total_models,
                },
            }

            # Write atomically
            content = json.dumps(summary, indent=2, ensure_ascii=False)
            AtomicFileWriter.write_atomic(filepath, content)

            logger.debug(f"[persist] saved models summary for {self.name}")

        except Exception as e:
            logger.error(f"[persist] failed to save models for {self.name}: {e}")


class MultiResultManager:
    """Manages results for multiple providers"""

    def __init__(
        self,
        workspace: str,
        providers: Dict[str, IProvider] = None,
        batch_size: int = 50,
        save_interval: float = 30.0,
        simple: bool = False,
        shutdown_timeout: float = 5.0,
    ):
        self.workspace = workspace
        self.providers = providers or {}
        self.batch_size = batch_size
        self.save_interval = save_interval
        self.simple = simple
        self.shutdown_timeout = float(max(1.0, shutdown_timeout))
        self.managers: Dict[str, ResultManager] = {}
        self.lock = threading.Lock()

        # Create workspace directory
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(os.path.join(workspace, "providers"), exist_ok=True)

    def get_manager(self, name: str) -> ResultManager:
        """Get or create result manager for provider"""
        with self.lock:
            if name not in self.managers:
                provider = self.providers.get(name)
                if not provider:
                    raise ValueError(f"Provider instance not found: {name}")
                self.managers[name] = ResultManager(
                    provider,
                    self.workspace,
                    self.batch_size,
                    self.save_interval,
                    simple=self.simple,
                    shutdown_timeout=self.shutdown_timeout,
                )
            return self.managers[name]

    def add_result(self, provider: str, result_type: str, data: Any):
        """Add result for a specific provider"""
        manager = self.get_manager(provider)
        manager.add_result(result_type, data)

    def add_links(self, provider: str, links: List[str]):
        """Add links for a specific provider"""
        manager = self.get_manager(provider)
        manager.add_links(links)

    def add_models(self, provider: str, key: str, models: List[str]):
        """Add models for a specific provider"""
        manager = self.get_manager(provider)
        manager.add_models(key, models)

    def flush_all(self):
        """Flush all providers"""
        with self.lock:
            for manager in self.managers.values():
                manager.flush_all()

    def get_all_stats(self) -> Dict[str, PersistenceMetrics]:
        """Get statistics for all providers"""
        stats = {}
        with self.lock:
            for provider, manager in self.managers.items():
                stats[provider] = manager.get_stats()
        return stats

    def recover_all_tasks(self) -> AllRecoveredTasks:
        """Recover tasks from all providers' result files"""
        all_recovered = AllRecoveredTasks()

        for name in self.providers.keys():
            try:
                manager = self.get_manager(name)
                recovered = manager.recover_tasks()
                all_recovered.add_provider(name, recovered)
            except Exception as e:
                logger.error(f"Failed to recover tasks for {name}: {e}")

        if all_recovered.has_providers():
            logger.info(
                f"Recovered {all_recovered.total_check_tasks()} check tasks, "
                f"{all_recovered.total_acquisition_tasks()} acquisition tasks, "
                f"{all_recovered.total_invalid_keys()} invalid keys, and "
                f"{all_recovered.total_valid_keys()} valid keys from all providers"
            )

        return all_recovered

    def reseed_valid_keys(self, recovered: AllRecoveredTasks) -> int:
        """Write previously valid keys back after backup moves result files away."""
        total = 0
        for name, tasks in recovered.providers.items():
            if not tasks.valid:
                continue
            try:
                manager = self.get_manager(name)
                manager.add_result(ResultType.VALID.value, list(tasks.valid))
                total += len(tasks.valid)
                logger.info(f"Re-seeded {len(tasks.valid)} valid keys for {name}")
            except Exception as e:
                logger.error(f"Failed to re-seed valid keys for {name}: {e}")
        return total

    def backup_all_existing_files(self) -> None:
        """Backup existing files for all providers"""
        for name in self.providers.keys():
            try:
                manager = self.get_manager(name)
                manager.backup_existing_files()
            except Exception as e:
                logger.error(f"Failed to backup files for {name}: {e}")

    def start_periodic_snapshots(self, interval_sec: int = 300) -> None:
        """Start periodic snapshots for all providers."""
        started_count = 0
        with self.lock:
            for manager in self.managers.values():
                try:
                    # Only count if snapshot manager exists
                    if manager.snapshot_manager:
                        manager.start_periodic_snapshot(interval_sec)
                        started_count += 1
                except Exception as e:
                    logger.error(f"Failed to start periodic snapshot for {manager.name}: {e}")

        if started_count > 0:
            logger.info(f"Started periodic snapshots for {started_count} providers, interval: {interval_sec}s")
        else:
            logger.debug("No periodic snapshots started (simple mode or no providers)")

    def stop_periodic_snapshots(self) -> None:
        """Stop periodic snapshots for all providers."""
        with self.lock:
            for manager in self.managers.values():
                try:
                    manager.stop_periodic_snapshot()
                except Exception as e:
                    logger.error(f"Failed to stop periodic snapshot for {manager.name}: {e}")
        logger.info("Stopped periodic snapshots for all providers")

    def build_all_snapshots_all(self) -> Dict[str, Dict[str, int]]:
        """Build snapshots for all result types across all providers."""
        results: Dict[str, Dict[str, int]] = {}
        with self.lock:
            for provider_name, manager in self.managers.items():
                try:
                    results[provider_name] = manager.build_all_snapshots()
                except Exception as e:
                    logger.error(f"Failed to build snapshots for {provider_name}: {e}")
                    results[provider_name] = {}
        total_snapshots = sum(len(provider_results) for provider_results in results.values())
        logger.info(f"Built {total_snapshots} snapshots across {len(results)} providers")
        return results

    def stop_all(self):
        """Stop all result managers"""
        with self.lock:
            for manager in self.managers.values():
                manager.stop()

        logger.info("Stopped all result managers")


if __name__ == "__main__":
    # Test result manager
    # Create temporary workspace
    workspace = tempfile.mkdtemp()
    logger.info(f"Testing in workspace: {workspace}")

    # Create mock provider for testing
    class MockProvider:
        def __init__(self):
            self.name = "test_provider"
            self.result = ResultStorage(
                folder="test_provider",
                filenames={
                    "valid": "valid-keys.txt",
                    "no_quota": "no-quota-keys.txt",
                    "wait_check": "wait-check-keys.txt",
                    "invalid": "invalid-keys.txt",
                    "material": "material.txt",
                    "summary": "summary.json",
                    "links": "links.txt",
                },
            )

    try:
        # Test single provider
        mock_provider = MockProvider()
        manager = ResultManager(mock_provider, workspace, batch_size=3, save_interval=1)

        # Add some results
        manager.add_result(ResultType.VALID.value, ["key1", "key2"])
        manager.add_links(["http://example.com/1", "http://example.com/2"])
        manager.add_result(ResultType.VALID.value, "key3")  # Should trigger flush

        # Wait for periodic flush
        time.sleep(2)

        # Check files
        links_file = os.path.join(workspace, "providers", "test_provider", "links.txt")
        if os.path.exists(links_file):
            with open(links_file) as f:
                content = f.read()
                logger.info(f"Links file content:\n{content}")

        # Get stats
        stats = manager.get_stats()
        logger.info(f"Stats: valid_keys={stats.valid}, links={stats.resource.links}")

        # Stop manager
        manager.stop()

        logger.info("Result manager test completed!")

    finally:
        # Cleanup
        shutil.rmtree(workspace)
