"""
Atomic file operations and repair utilities.
"""

import contextlib
import os
import tempfile
import time
from typing import List

from tools.logger import get_logger
from tools.utils import handle_exceptions

logger = get_logger("storage")

# Cross-platform file lock support (best-effort)
try:
    import fcntl  # type: ignore

    _HAS_FCNTL = True
except Exception:
    fcntl = None  # type: ignore
    _HAS_FCNTL = False

try:
    import msvcrt  # type: ignore

    _HAS_MSVCRT = True
except Exception:
    msvcrt = None  # type: ignore
    _HAS_MSVCRT = False


@contextlib.contextmanager
def _exclusive_file_lock(fobj):
    """Best-effort exclusive file lock around writes.

    Falls back to no-op if platform lock is unavailable.
    """
    locked = False
    try:
        if _HAS_FCNTL:
            try:
                fcntl.flock(fobj.fileno(), fcntl.LOCK_EX)
                locked = True
            except Exception:
                pass
        elif _HAS_MSVCRT:
            try:
                # Lock first byte; this is a conventional approach for simple exclusivity
                msvcrt.locking(fobj.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except Exception:
                pass

        yield

    finally:
        if locked:
            try:
                if _HAS_FCNTL:
                    fcntl.flock(fobj.fileno(), fcntl.LOCK_UN)
                elif _HAS_MSVCRT:
                    msvcrt.locking(fobj.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass


def _retry_on_windows_lock(max_retries: int = 3, base_delay: float = 0.1):
    """Retry decorator for Windows file locking issues with exponential backoff"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (PermissionError, OSError) as e:
                    last_exception = e
                    # Check for Windows-specific file locking errors
                    if hasattr(e, "winerror") and e.winerror == 32:  # WinError 32: file in use
                        if attempt < max_retries:
                            delay = base_delay * (2**attempt)  # Exponential backoff
                            logger.debug(
                                f"Windows file lock detected, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries + 1})"
                            )
                            time.sleep(delay)
                            continue
                    # Re-raise if not a retryable Windows lock error or max retries exceeded
                    raise
            # If we get here, all retries failed
            raise last_exception

        return wrapper

    return decorator


class AtomicFileWriter:
    """Atomic file operations to prevent corruption during writes"""

    @staticmethod
    @_retry_on_windows_lock(max_retries=3, base_delay=0.1)
    @handle_exceptions(default_result=None, log_level="error", reraise=True)
    def write_atomic(filepath: str, content: str) -> None:
        """Write content to file atomically using temp file + rename with Windows-safe retry"""
        directory = os.path.dirname(filepath)
        os.makedirs(directory, exist_ok=True)

        temp_path = None
        try:
            # Create temp file in same directory to ensure atomic rename
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
            ) as temp_file:
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = temp_file.name

            # On Windows, acquire lock on target file during rename to prevent conflicts
            if _HAS_MSVCRT and os.path.exists(filepath):
                try:
                    with open(filepath, "r+b") as target_file, _exclusive_file_lock(target_file):
                        os.replace(temp_path, filepath)
                except (PermissionError, OSError):
                    # Fallback to direct rename if locking fails
                    os.replace(temp_path, filepath)
            else:
                # Direct rename on Unix or when target doesn't exist
                os.replace(temp_path, filepath)

        except Exception:
            # Clean up temp file on failure
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    @handle_exceptions(default_result=None, log_level="error", reraise=True)
    def append_atomic(filepath: str, lines: List[str]) -> None:
        """Append lines to file atomically with fsync and resource management.

        Raises:
            OSError: propagated to the caller (mirrors write_atomic) so the
                write path can re-queue the batch. Swallowing it here used to
                turn a full disk / permission error / AV lock into a reported
                success and silently lost the batch.
        """
        directory = os.path.dirname(filepath)
        os.makedirs(directory, exist_ok=True)

        # Use standard file operations
        with open(filepath, "a", encoding="utf-8") as f, _exclusive_file_lock(f):
            for line in lines:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
            f.flush()
            os.fsync(f.fileno())


def repair_trailing_partial(shard_path: str) -> None:
    """Best-effort trim the last line if it is a partial JSON object.

    This function reads last bytes and ensures the file ends with a newline.
    Utility function for both snapshot building and recovery operations.
    """
    try:
        with open(shard_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return
            # Read last 4KB
            back = 4096
            start = max(0, size - back)
            f.seek(start)
            tail = f.read()
        # Ensure final newline
        if not tail.endswith(b"\n"):
            # Trim to last newline
            nl = tail.rfind(b"\n")
            if nl == -1:
                # No newline in last 4KB; drop the last chunk conservatively
                with open(shard_path, "rb+") as w:
                    w.truncate(start)
                return
            # Truncate to last full line
            with open(shard_path, "rb+") as w:
                w.truncate(start + nl + 1)
    except Exception as e:
        logger.error(f"[storage] failed to repair shard tail {shard_path}: {e}")
