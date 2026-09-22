"""
Snapshot manager for building pretty JSON from NDJSON shards.
"""

import json
import os
import threading
from typing import List

from tools.logger import get_logger, log_aggregated_error

from .atomic import repair_trailing_partial

logger = get_logger("storage")


class SnapshotManager:
    """Build pretty JSON snapshots from NDJSON shards with atomic replace."""

    def __init__(self, shard_root: str, snapshot_path: str):
        self.shard_root = shard_root
        self.snapshot_path = snapshot_path

    def build_snapshot(self) -> int:
        """Merge all shards under shard_root into a streaming JSON snapshot.

        Uses streaming JSON array output to avoid loading all records into memory.
        If sidecar indexes exist, use them to order and quickly estimate content.
        The snapshot is written to a per-builder temp file and then renamed over
        the target, so concurrent builders cannot corrupt each other's output.

        Returns: number of records written

        Raises:
            Exception: re-raised after cleaning up the temp file.
        """
        # First, try to use sidecar indexes to get a deterministic order of shards
        shards: List[str] = []
        indexed: List[tuple[str, dict]] = []
        for root, _, files in os.walk(self.shard_root):
            for fn in files:
                if not fn.endswith(".ndjson"):
                    continue
                fp = os.path.join(root, fn)
                idx_fp = os.path.splitext(fp)[0] + ".index.json"
                try:
                    with open(idx_fp, "r", encoding="utf-8") as f:
                        idx = json.load(f)
                        indexed.append((fp, idx))
                except Exception:
                    shards.append(fp)

        # Sort indexed shards by first_ts then last_ts
        def _ts(d: dict, key: str) -> str:
            return d.get(key) or ""

        indexed.sort(key=lambda t: (_ts(t[1], "first_ts"), _ts(t[1], "last_ts")))
        ordered = [fp for fp, _ in indexed] + sorted(shards)

        # Stream write JSON array to avoid memory pressure.
        # Unique per builder: two concurrent builds of the same snapshot (the
        # periodic thread racing the shutdown path, or two processes sharing the
        # directory) must never write - and rename away - one shared temp file.
        temp_path = f"{self.snapshot_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        record_count = 0

        try:
            with open(temp_path, "w", encoding="utf-8") as out_f:
                out_f.write("[\n")
                first_record = True

                for fp in ordered:
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    # Validate JSON and write directly
                                    record = json.loads(line)
                                    if not first_record:
                                        out_f.write(",\n")
                                    json.dump(record, out_f, indent=2, ensure_ascii=False)
                                    first_record = False
                                    record_count += 1
                                except Exception as e:
                                    # Skip bad lines in snapshot to maximize robustness
                                    log_aggregated_error(
                                        logger, f"bad_line_snapshot_{fp}", f"[snapshot] skipping bad line in {fp}: {e}"
                                    )
                                    continue
                    except Exception:
                        # If we failed to read, attempt to repair trailing partial and retry once
                        try:
                            repair_trailing_partial(fp)
                            with open(fp, "r", encoding="utf-8") as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        record = json.loads(line)
                                        if not first_record:
                                            out_f.write(",\n")
                                        json.dump(record, out_f, indent=2, ensure_ascii=False)
                                        first_record = False
                                        record_count += 1
                                    except Exception as e:
                                        log_aggregated_error(
                                            logger,
                                            f"bad_line_retry_{fp}",
                                            f"[snapshot] skipping bad line in retry {fp}: {e}",
                                        )
                                        continue
                        except Exception:
                            continue

                out_f.write("\n]")
                out_f.flush()
                os.fsync(out_f.fileno())

            # Atomic replace
            os.replace(temp_path, self.snapshot_path)
            return record_count

        except Exception as e:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e
