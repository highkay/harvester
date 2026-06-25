#!/usr/bin/env python3

"""
Standardized logging system for the async pipeline project.
Based on gemini-balance logger with modular organization.

Features:
- Color-coded console output with ANSI support
- File logging with rotation (main log + error-only log)
- API key redaction for security
- Module-specific loggers
- Automatic log cleanup
- Fixed-width formatting for readability

Log files are saved to the 'logs' directory:
- pipeline_YYYYMMDD_HHMMSS.log: All log levels
- pipeline_errors_YYYYMMDD_HHMMSS.log: ERROR and CRITICAL only
"""

import atexit
import ctypes
import json
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from constant.system import DEFAULT_LOG_CLEANUP_DELETE
from core.models import LogFileInfo, LoggingStats

from .patterns import redact_api_keys_in_text

# ANSI color codes for different log levels
COLORS = {
    "DEBUG": "\033[34m",  # Blue
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bold Red
}


# Global color toggle for console output
_COLOR_ENABLED = True


def set_color_enabled(enabled: bool) -> None:
    """Enable or disable ANSI color in console logs."""
    global _COLOR_ENABLED
    _COLOR_ENABLED = bool(enabled)


# Enable ANSI support on Windows (safe mode)
try:
    if platform.system() == "Windows" and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
except Exception:
    # Silently ignore if console mode change is not supported
    pass


class ColoredFormatter(logging.Formatter):
    """Custom log formatter with color support and file location"""

    def format(self, record: logging.LogRecord) -> str:
        # Create a copy of the record to avoid modifying the original
        record_copy = logging.LogRecord(
            name=record.name,
            level=record.levelno,
            pathname=record.pathname,
            lineno=record.lineno,
            msg=record.msg,
            args=record.args,
            exc_info=record.exc_info,
            func=record.funcName,
            stack_info=getattr(record, "stack_info", None),
        )

        # Copy additional attributes
        for key, value in record.__dict__.items():
            if not hasattr(record_copy, key):
                setattr(record_copy, key, value)

        # Get color code for log level
        color = COLORS.get(record_copy.levelname, "")
        # Add color code and reset code
        if _COLOR_ENABLED and _is_tty():
            record_copy.levelname = f"{color}{record_copy.levelname}\033[0m"
        else:
            record_copy.levelname = str(record_copy.levelname)

        # Create fixed-width string with filename and line number
        record_copy.fileloc = f"[{record_copy.filename}:{record_copy.lineno}]"
        return super().format(record_copy)


class APIKeyRedactionFormatter(logging.Formatter):
    """Custom formatter that redacts API keys in log messages"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        # Format the record normally first
        formatted_msg = super().format(record)
        # Redact API keys in the formatted message
        return self._redact_api_keys_in_message(formatted_msg)

    def _redact_api_keys_in_message(self, message: str) -> str:
        """Replace API keys in log message with redacted versions"""
        try:
            return redact_api_keys_in_text(message)
        except Exception as e:
            # Log the error but do not expose the original message
            logger = logging.getLogger(__name__)
            logger.error(f"Error redacting API keys in log: {e}")
            return "[LOG_REDACTION_ERROR]"


# JSON formatter for structured file logs (optional)
class JSONFormatter(logging.Formatter):
    """Simple JSON formatter for structured logging to files."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = {
                "ts": self.formatTime(record, datefmt="%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "file": record.filename,
                "line": record.lineno,
                "message": record.getMessage(),
            }
            # Include any extra context fields if present
            for key, value in record.__dict__.items():
                if key not in payload and not key.startswith("_"):
                    payload[key] = value
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            # Fallback to plain text if JSON formatting fails
            return f"{record.levelname} {record.filename}:{record.lineno} {record.getMessage()}"


def _is_tty() -> bool:
    """Detect if stdout is a TTY for safe color output."""
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False


class RedactionFilter(logging.Filter):
    """Filter that redacts API keys in log records before formatting."""

    # Cache standard LogRecord attributes to avoid repeated computation
    _standard_attrs = None

    def __init__(self) -> None:
        super().__init__()
        # Initialize standard attributes cache if not already done
        if RedactionFilter._standard_attrs is None:
            baseline_record = logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None)
            RedactionFilter._standard_attrs = set(baseline_record.__dict__.keys())

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Redact in message
            msg = record.getMessage()
            msg = redact_api_keys_in_text(msg)
            # We cannot assign to getMessage(); update 'msg' only if it is a string
            if isinstance(record.msg, str):
                record.msg = msg

            # Redact any extra fields that might contain sensitive data
            # Use cached standard attributes to identify custom fields
            for key, value in record.__dict__.items():
                if not key.startswith("_") and isinstance(value, str) and key not in self._standard_attrs:
                    redacted = redact_api_keys_in_text(value)
                    setattr(record, key, redacted)
            return True
        except Exception:
            # In case of any failure, do not block logging
            return True


def _ensure_redaction_filter(target: logging.Filterer) -> None:
    """Attach API key redaction once to a logger or handler."""
    if not any(isinstance(item, RedactionFilter) for item in target.filters):
        target.addFilter(RedactionFilter())


# Log format with file location and fixed width
FORMATTER = ColoredFormatter("%(asctime)s | %(levelname)-17s | %(fileloc)-30s | %(message)s")
# Optional JSON formatter for file logs
FILE_FORMATTER_JSON = JSONFormatter()


# File log formatter mode: "text" or "json"
_FILE_LOG_FORMAT = "text"


def get_file_formatter() -> logging.Formatter:
    """Return the active formatter for file handlers based on configuration."""
    return FILE_FORMATTER_JSON if _FILE_LOG_FORMAT == "json" else FILE_FORMATTER


def set_file_log_format(fmt: str) -> None:
    """Set file logging format mode: "text" or "json"."""
    global _FILE_LOG_FORMAT
    if fmt not in ("text", "json"):
        raise ValueError("file log format must be 'text' or 'json'")
    _FILE_LOG_FORMAT = fmt


# File formatter without colors for file output
class FileFormatter(logging.Formatter):
    """File formatter that removes ANSI color codes and uses clean formatting"""

    def format(self, record: logging.LogRecord) -> str:
        # Create file location string
        record.fileloc = f"[{record.filename}:{record.lineno}]"
        return super().format(record)


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Cross-platform safe rotating file handler with hybrid rollover strategy"""

    def __init__(self, filename, mode="a", max_bytes=0, backup_count=0, encoding=None):
        """Initialize with delayed file creation and safe rollover"""
        super().__init__(filename, mode, max_bytes, backup_count, encoding, delay=True)
        self._initialized = False
        self._rollover_retries = 3
        self._rollover_delay = 0.1
        self._rollover_stats = {"rename_success": 0, "copy_success": 0, "failures": 0}

    def _ensure_initialized(self):
        """Ensure file is created and handler is initialized before first write"""
        if not self._initialized:
            Logger._ensure_logs_directory()
            # Create file if it doesn't exist
            if not os.path.exists(self.baseFilename):
                os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
                with open(self.baseFilename, "w", encoding=self.encoding):
                    pass
            self._initialized = True

    def emit(self, record: logging.LogRecord):
        """Ensure initialization before writing log record"""
        if not self._initialized:
            self._ensure_initialized()
        super().emit(record)

    def doRollover(self):
        """Perform safe rollover with hybrid strategy"""
        start_time = time.time()

        if self.stream:
            self.stream.close()
            self.stream = None

        # Try hybrid rollover approach
        success = self._try_rename_rollover()
        if success:
            self._rollover_stats["rename_success"] += 1
        else:
            success = self._try_copy_rollover()
            if success:
                self._rollover_stats["copy_success"] += 1

        if not success:
            # Fallback: just truncate current file
            self._truncate_current()
            self._rollover_stats["failures"] += 1

        # Reopen stream
        if not self.delay:
            self.stream = self._open()

        # Log performance if rollover took too long
        duration = time.time() - start_time
        if duration > 1.0:  # Log if rollover takes more than 1 second
            print(f"Slow rollover detected: {duration:.2f}s for {self.baseFilename}")

    def _try_rename_rollover(self):
        """Try fast rename-based rollover (preferred method)"""
        for i in range(self._rollover_retries):
            try:
                # Generate backup filename
                backup_name = self._get_backup_name(1)

                # Remove oldest backup if exists
                if self.backupCount > 0:
                    self._rotate_backups()

                # Rename current file to backup
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, backup_name)

                return True

            except (OSError, PermissionError) as e:
                if i < self._rollover_retries - 1:
                    time.sleep(self._rollover_delay * (i + 1))
                    continue
                # Log the failure but don't raise
                print(f"Rename rollover failed: {e}")
                return False

        return False

    def _try_copy_rollover(self):
        """Try copy+truncate rollover (fallback method)"""
        try:
            # Generate backup filename
            backup_name = self._get_backup_name(1)

            # Remove oldest backup if exists
            if self.backupCount > 0:
                self._rotate_backups()

            # Copy current file to backup
            if os.path.exists(self.baseFilename):
                shutil.copy2(self.baseFilename, backup_name)

                # Truncate current file
                with open(self.baseFilename, "w", encoding=self.encoding) as f:
                    f.truncate(0)
                    f.flush()

            return True

        except Exception as e:
            print(f"Copy rollover failed: {e}")
            return False

    def _truncate_current(self):
        """Fallback: just truncate current file"""
        try:
            with open(self.baseFilename, "w", encoding=self.encoding) as f:
                f.truncate(0)
                f.flush()
                os.fsync(f.fileno())  # Force OS to write to disk
        except Exception as e:
            print(f"File truncation failed: {e}")

    def _get_backup_name(self, index):
        """Generate backup filename"""
        return f"{self.baseFilename}.{index}"

    def _rotate_backups(self):
        """Rotate existing backup files"""
        # Remove the oldest backup
        oldest = self._get_backup_name(self.backupCount)
        if os.path.exists(oldest):
            try:
                os.remove(oldest)
            except OSError:
                pass

        # Rotate existing backups
        for i in range(self.backupCount - 1, 0, -1):
            old_name = self._get_backup_name(i)
            new_name = self._get_backup_name(i + 1)
            if os.path.exists(old_name):
                try:
                    os.rename(old_name, new_name)
                except OSError:
                    pass

    def get_stats(self):
        """Get handler statistics"""
        return {
            "rollover_stats": self._rollover_stats.copy(),
            "retries": self._rollover_retries,
            "delay": self._rollover_delay,
            "file": self.baseFilename,
        }


class FileFormatterWithRedaction(logging.Formatter):
    """File formatter that combines clean formatting with API key redaction"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        # Create file location string
        record.fileloc = f"[{record.filename}:{record.lineno}]"
        # Format the record normally first
        formatted_msg = super().format(record)
        # Then apply API key redaction
        return self._redact_api_keys_in_message(formatted_msg)

    def _redact_api_keys_in_message(self, message: str) -> str:
        """Replace API keys in log message with redacted versions"""
        try:
            return redact_api_keys_in_text(message)
        except Exception:
            # Return original message if redaction fails
            return message


FILE_FORMATTER = FileFormatterWithRedaction("%(asctime)s | %(levelname)-8s | %(fileloc)-30s | %(message)s")

# Log level mapping
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class Logger:
    """Centralized logger management system"""

    _loggers: Dict[str, logging.Logger] = {}
    _default_level = logging.INFO
    _logs_dir = None
    _file_handler = None
    _module_handlers: Dict[str, logging.Handler] = {}
    _archived_logs: set = set()  # Track which logs have been archived

    @staticmethod
    def _ensure_logs_directory():
        """Ensure logs directory exists and handle existing logs based on configuration"""
        if not Logger._logs_dir:
            return

        # Only create directory and handle existing logs if we haven't done it before
        if not hasattr(Logger, "_directory_initialized"):
            Logger._logs_dir.mkdir(exist_ok=True)

            # Use constant to determine cleanup mode
            if DEFAULT_LOG_CLEANUP_DELETE:
                Logger._delete_existing_logs()
            else:
                Logger._archive_existing_logs()

            Logger._directory_initialized = True

    @staticmethod
    def _delete_existing_logs():
        """Delete all existing log files"""
        if not Logger._logs_dir or not Logger._logs_dir.exists():
            return

        # Find and delete all .log files
        for log_file in Logger._logs_dir.glob("*.log"):
            try:
                log_file.unlink()
                Logger._archived_logs.add(log_file.name)
            except Exception as e:
                print(f"Failed to delete {log_file.name}: {e}")

    @staticmethod
    def _archive_existing_logs():
        """Archive all existing log files by adding timestamp to their names"""
        if not Logger._logs_dir or not Logger._logs_dir.exists():
            return

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        # Find all .log files (excluding already archived ones with timestamps)
        for log_file in Logger._logs_dir.glob("*.log"):
            # Skip files that look like archived logs (contain timestamp pattern)
            if "-" in log_file.stem and log_file.stem.split("-")[-1].isdigit():
                continue

            archived_name = log_file.stem + f"-{timestamp}.log"
            archived_path = Logger._logs_dir / archived_name
            try:
                log_file.rename(archived_path)
                print(f"Archived existing log: {log_file.name} -> {archived_name}")
                Logger._archived_logs.add(log_file.name)
            except Exception as e:
                print(f"Failed to archive {log_file.name}: {e}")

    @staticmethod
    def _delete_module_log(module_name: str):
        """Delete a specific module log file if it exists"""
        if not Logger._logs_dir or not Logger._logs_dir.exists():
            return

        log_file_name = f"{module_name}.log"

        # Skip if already processed in this session
        if log_file_name in Logger._archived_logs:
            return

        log_file = Logger._logs_dir / log_file_name
        if log_file.exists():
            try:
                log_file.unlink()
                Logger._archived_logs.add(log_file_name)
            except Exception as e:
                print(f"Failed to delete {log_file_name}: {e}")

    @staticmethod
    def _archive_module_log(module_name: str):
        """Archive a specific module log file if it exists"""
        if not Logger._logs_dir or not Logger._logs_dir.exists():
            return

        log_file_name = f"{module_name}.log"

        # Skip if already archived in this session
        if log_file_name in Logger._archived_logs:
            return

        log_file = Logger._logs_dir / log_file_name
        if log_file.exists():
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            archived_name = f"{module_name}-{timestamp}.log"
            archived_path = Logger._logs_dir / archived_name
            try:
                log_file.rename(archived_path)
                print(f"Archived existing log: {log_file_name} -> {archived_name}")
                Logger._archived_logs.add(log_file_name)
            except Exception as e:
                print(f"Failed to archive {log_file_name}: {e}")

    @staticmethod
    def _handle_module_log(module_name: str):
        """Handle existing module log file based on configuration"""
        if DEFAULT_LOG_CLEANUP_DELETE:
            Logger._delete_module_log(module_name)
        else:
            Logger._archive_module_log(module_name)

    @staticmethod
    def _setup_file_handlers():
        """Setup file handlers for logging to files"""
        if Logger._file_handler is not None:
            return  # Already setup

        # Set logs directory path (but don't create it yet)
        Logger._logs_dir = Path("logs")

        # Main log file (all levels) - without timestamp
        main_log_file = Logger._logs_dir / "main.log"
        Logger._file_handler = SafeRotatingFileHandler(
            main_log_file, max_bytes=50 * 1024 * 1024, backup_count=10, encoding="utf-8"  # 50MB
        )
        Logger._file_handler.setFormatter(get_file_formatter())
        Logger._file_handler.setLevel(logging.DEBUG)

    @staticmethod
    def _get_or_create_module_handler(module_name: str) -> logging.Handler:
        """Get or create a file handler for a specific module"""
        if module_name in Logger._module_handlers:
            return Logger._module_handlers[module_name]

        # Handle existing module log file before creating new handler
        Logger._handle_module_log(module_name)

        # Create module-specific log file
        module_log_file = Logger._logs_dir / f"{module_name}.log"
        module_handler = SafeRotatingFileHandler(
            module_log_file, max_bytes=20 * 1024 * 1024, backup_count=5, encoding="utf-8"  # 20MB
        )
        module_handler.setFormatter(get_file_formatter())
        module_handler.setLevel(logging.DEBUG)
        _ensure_redaction_filter(module_handler)

        Logger._module_handlers[module_name] = module_handler
        return module_handler

    @staticmethod
    def setup_logger(name: str, level: Optional[str] = None) -> logging.Logger:
        """
        Setup and get logger instance
        :param name: logger name
        :param level: log level (optional, uses default if not provided)
        :return: logger instance
        """
        # Use provided level or default
        if level:
            log_level = LOG_LEVELS.get(level.lower(), logging.INFO)
        else:
            log_level = Logger._default_level

        if name in Logger._loggers:
            # If logger exists, update its level if needed
            existing_logger = Logger._loggers[name]
            if existing_logger.level != log_level:
                existing_logger.setLevel(log_level)
            return existing_logger

        logger_instance = logging.getLogger(name)
        logger_instance.setLevel(log_level)
        logger_instance.propagate = False
        _ensure_redaction_filter(logger_instance)

        # Setup file handlers if not already done
        Logger._setup_file_handlers()

        # Add console handler with colored formatter
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(FORMATTER)
        console_handler.setLevel(log_level)
        _ensure_redaction_filter(console_handler)
        logger_instance.addHandler(console_handler)

        # Add module-specific file handler
        module_handler = Logger._get_or_create_module_handler(name)
        logger_instance.addHandler(module_handler)

        # Add main pipeline log handler (for centralized logging)
        if Logger._file_handler:
            _ensure_redaction_filter(Logger._file_handler)
            logger_instance.addHandler(Logger._file_handler)

        Logger._loggers[name] = logger_instance
        return logger_instance

    @staticmethod
    def get_logger(name: str) -> Optional[logging.Logger]:
        """
        Get existing logger
        :param name: logger name
        :return: logger instance or None
        """
        return Logger._loggers.get(name)

    @staticmethod
    def update_log_levels(log_level: str) -> None:
        """Update all existing loggers and their handlers to new log level"""
        log_level_str = log_level.lower()
        new_level = LOG_LEVELS.get(log_level_str, logging.INFO)
        Logger._default_level = new_level

        for _, logger_instance in Logger._loggers.items():
            if logger_instance.level != new_level:
                logger_instance.setLevel(new_level)

            # Also update all handlers for this logger
            for handler in logger_instance.handlers:
                if handler.level != new_level:
                    handler.setLevel(new_level)

    @staticmethod
    def set_default_level(level: str) -> None:
        """Set default log level for new loggers"""
        Logger._default_level = LOG_LEVELS.get(level.lower(), logging.INFO)

    @staticmethod
    def get_logs_directory() -> Optional[Path]:
        """Get the logs directory path"""
        return Logger._logs_dir

    @staticmethod
    def cleanup_old_logs(days: int = 7) -> None:
        """Clean up log files older than specified days"""
        if not Logger._logs_dir or not Logger._logs_dir.exists():
            return

        cutoff_time = time.time() - (days * 24 * 60 * 60)

        for log_file in Logger._logs_dir.glob("*.log*"):
            try:
                if log_file.stat().st_mtime < cutoff_time:
                    log_file.unlink()
                    print(f"Cleaned up old log file: {log_file}")
            except Exception as e:
                print(f"Failed to clean up log file {log_file}: {e}")

    @staticmethod
    def flush_all_handlers():
        """Flush all file handlers to ensure logs are written"""
        if Logger._file_handler:
            Logger._file_handler.flush()

        # Flush all module handlers
        for handler in Logger._module_handlers.values():
            handler.flush()

    @staticmethod
    def get_log_files_info() -> Dict[str, LogFileInfo]:
        """Get detailed information about current log files"""
        info = {}
        if Logger._logs_dir and Logger._logs_dir.exists():  # Only check if directory exists
            for log_file in Logger._logs_dir.glob("*.log"):
                try:
                    stat = log_file.stat()
                    size_mb = stat.st_size / (1024 * 1024)
                    modified_time = datetime.fromtimestamp(stat.st_mtime)

                    info[log_file.name] = LogFileInfo(
                        filename=log_file.name,
                        size=f"{size_mb:.2f} MB",
                        modified=modified_time.strftime("%Y-%m-%d %H:%M:%S"),
                        path=str(log_file.absolute()),
                    )
                except Exception as e:
                    info[log_file.name] = LogFileInfo(
                        filename=log_file.name, size="Unknown", modified="Unknown", path="", error=str(e)
                    )
        return info

    @staticmethod
    def get_log_stats() -> LoggingStats:
        """Get logging system statistics"""
        log_files_info = Logger.get_log_files_info()
        # log_files_info now returns Dict[str, LogFileInfo] directly
        return LoggingStats(
            active_loggers=len(Logger._loggers),
            log_files=log_files_info,
            logs_directory=str(Logger._logs_dir) if Logger._logs_dir else None,
        )

    @staticmethod
    def configure_rollover(retries: int = 3, delay: float = 0.1):
        """Configure rollover behavior for all handlers"""

        # Update main handler
        if Logger._file_handler and isinstance(Logger._file_handler, SafeRotatingFileHandler):
            Logger._file_handler._rollover_retries = retries
            Logger._file_handler._rollover_delay = delay

        # Update module handlers
        for handler in Logger._module_handlers.values():
            if isinstance(handler, SafeRotatingFileHandler):
                handler._rollover_retries = retries
                handler._rollover_delay = delay

    @staticmethod
    def get_rollover_stats():
        """Get rollover performance statistics"""
        stats = {}

        # Main handler stats
        if Logger._file_handler and isinstance(Logger._file_handler, SafeRotatingFileHandler):
            stats["main"] = Logger._file_handler._rollover_stats.copy()

        # Module handler stats
        for name, handler in Logger._module_handlers.items():
            if isinstance(handler, SafeRotatingFileHandler):
                stats[name] = handler._rollover_stats.copy()

        return stats


def configure_logging_from_env() -> None:
    """Configure file format and color from environment variables.

    Supported variables:
      - LOG_FILE_FORMAT: "text" or "json"
      - LOG_COLOR: "1"/"0" or "true"/"false" (case-insensitive)
    """

    fmt = os.getenv("LOG_FILE_FORMAT")
    if fmt and fmt.lower() in ("text", "json"):
        set_file_log_format(fmt.lower())

    color = os.getenv("LOG_COLOR")
    if color is not None:
        normalized = color.strip().lower()
        set_color_enabled(normalized in ("1", "true", "yes", "on"))


class ContextLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that injects common context fields into log records."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        # Merge adapter's extra with call-site extra (call-site wins)
        merged = {**self.extra, **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def get_context_logger(category: str, **context) -> ContextLoggerAdapter:
    """Get a logger with pre-attached context fields."""
    base = get_logger(category)
    return ContextLoggerAdapter(base, context)


def attach_context(logger: logging.Logger, **context) -> ContextLoggerAdapter:
    """Wrap an existing logger with additional context fields."""
    return ContextLoggerAdapter(logger, context)


class ErrorAggregator:
    """Aggregates similar errors to reduce log noise."""

    def __init__(self, window_sec: int = 60, max_count: int = 10):
        self.window_sec = window_sec
        self.max_count = max_count
        self._errors: Dict[str, Dict[str, Any]] = {}

    def should_log(self, error_key: str) -> tuple[bool, int]:
        """Check if error should be logged and return count.

        Returns: (should_log, current_count)
        """
        now = time.time()
        if error_key not in self._errors:
            self._errors[error_key] = {"count": 1, "first_seen": now, "last_seen": now}
            return True, 1

        error_info = self._errors[error_key]
        error_info["count"] += 1
        error_info["last_seen"] = now

        # Reset if window expired
        if now - error_info["first_seen"] > self.window_sec:
            self._errors[error_key] = {"count": 1, "first_seen": now, "last_seen": now}
            return True, 1

        # Log every max_count occurrences or first occurrence
        should_log = error_info["count"] == 1 or error_info["count"] % self.max_count == 0
        return should_log, error_info["count"]


# Global error aggregator for reducing log noise
_error_aggregator = ErrorAggregator()


def log_aggregated_error(logger: logging.Logger, error_key: str, message: str, *args, **kwargs):
    """Log error with aggregation to reduce noise.

    Args:
        logger: Logger instance
        error_key: Unique key for this type of error (e.g., "bad_line_parse")
        message: Log message
        *args, **kwargs: Standard logging arguments
    """
    should_log, count = _error_aggregator.should_log(error_key)
    if should_log:
        if count > 1:
            message = f"{message} (occurred {count} times)"
        logger.error(message, *args, **kwargs)


def get_logger(category: Optional[str] = None) -> logging.Logger:
    """Get or create a category logger using unified API."""
    name = (category or "main").strip() or "main"
    return Logger.setup_logger(name)


def init_logging(
    level: str = "INFO",
    cleanup_days: int = 7,
    file_format: str = "text",
    rollover_retries: int = 3,
    rollover_delay: float = 0.1,
):
    """
    Initialize logging system with specified level.
    :param level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    :param cleanup_days: Number of days to keep old log files (default: 7)
    :param file_format: File log format mode: "text" or "json"
    :param rollover_retries: Number of retries for rollover operations (default: 3)
    :param rollover_delay: Delay between rollover retries in seconds (default: 0.1)
    """
    Logger.update_log_levels(level)

    # Configure file log formatter mode
    set_file_log_format(file_format)

    # Clean up old log files
    Logger.cleanup_old_logs(cleanup_days)

    # Setup file handlers
    Logger._setup_file_handlers()

    # Configure rollover behavior
    Logger.configure_rollover(rollover_retries, rollover_delay)

    # Attach redaction filter to root; named loggers also receive it in setup_logger().
    _ensure_redaction_filter(logging.getLogger())

    # Setup exit handlers for graceful shutdown
    _setup_exit_handlers()

    # Log initialization message
    logger = get_logger("main")
    logger.info(f"Logging system initialized - Level: {level.upper()}")
    logger.info(f"Logs directory: {Logger.get_logs_directory()}")
    logger.info(f"Log cleanup: keeping files for {cleanup_days} days")
    logger.info(f"Rollover config: {rollover_retries} retries, {rollover_delay}s delay")


def _setup_exit_handlers():
    """Setup exit handlers for graceful logging shutdown"""
    # Only register exit handler, let main application handle signals
    # to avoid conflicts with application-level signal handling
    atexit.register(shutdown_logging)


# Utility functions for log management
def get_current_log_files():
    """Get detailed information about current log files"""
    return Logger.get_log_files_info()


def get_logging_stats() -> LoggingStats:
    """Get comprehensive logging system statistics"""
    return Logger.get_log_stats()


def cleanup_logs(days: int = 7):
    """Clean up log files older than specified days"""
    Logger.cleanup_old_logs(days)


def flush_logs():
    """Flush all log handlers to ensure data is written to files"""
    Logger.flush_all_handlers()


def get_rollover_health():
    """Get rollover health summary"""
    stats = Logger.get_rollover_stats()
    health = {}

    for name, stat in stats.items():
        total = stat["rename_success"] + stat["copy_success"] + stat["failures"]
        if total > 0:
            success_rate = (stat["rename_success"] + stat["copy_success"]) / total * 100
            health[name] = {
                "success_rate": f"{success_rate:.1f}%",
                "total_rollovers": total,
                "rename_success": stat["rename_success"],
                "copy_fallback": stat["copy_success"],
                "failures": stat["failures"],
            }

    return health


def shutdown_logging():
    """Gracefully shutdown logging system and close all handlers"""
    Logger.flush_all_handlers()

    # Close file handlers
    if Logger._file_handler:
        Logger._file_handler.close()

    # Close all module handlers
    for handler in Logger._module_handlers.values():
        handler.close()

    # Clear logger cache
    Logger._loggers.clear()

    # Reset handlers
    Logger._file_handler = None
    Logger._module_handlers.clear()


def setup_access_logging():
    """
    Configure access logging with API key redaction

    This function sets up custom access log formatting that automatically
    redacts API keys in HTTP access logs. It works by:

    1. Intercepting access log messages
    2. Using regex patterns to find API keys in URLs
    3. Replacing them with redacted versions (first6...last6)

    Supported API key formats:
    - Google/Gemini API keys: AIza[35 chars]
    - OpenAI API keys: sk-[48 chars]
    - OpenAI project keys: sk-proj-[48 chars]
    - OpenRouter keys: sk-or-v1-[20+ chars]
    - Cerebras keys: csk-[20+ chars]
    - Anthropic keys: anthrop[20+ chars]
    - Groq/GooeyAI keys: gsk_[20+ chars]
    - xAI/Grok keys: xai-[20+ chars]
    - StabilityAI keys: stab_[20+ chars]
    """
    # Get access logger (if using uvicorn or similar)
    access_logger = logging.getLogger("uvicorn.access")

    # Remove existing handlers to avoid duplicate logs
    for handler in access_logger.handlers[:]:
        access_logger.removeHandler(handler)

    # Create new handler with API key redaction formatter
    handler = logging.StreamHandler(sys.stdout)
    access_formatter = APIKeyRedactionFormatter("%(asctime)s | %(levelname)-8s | %(message)s")
    handler.setFormatter(access_formatter)

    # Add the handler to access logger
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False

    return access_logger


if __name__ == "__main__":
    # Test the logging system
    init_logging("DEBUG")

    # Test different loggers
    main_logger = get_logger("main")
    config_logger = get_logger("config")

    main_logger.info("Main logger test - this should appear in console and file")
    config_logger.debug("Config logger test - debug level")

    # Test error logging (should go to both main log and error log)
    main_logger.error("Test error message - should appear in error log too")

    # Test API key redaction
    test_logger = get_logger("provider")
    test_logger.info("Testing API key redaction: sk-1234567890abcdefghij1234567890abcdefghij")
    test_logger.info("Testing Gemini key: AIza1234567890abcdefghij1234567890abcde")

    # Flush all handlers to ensure logs are written
    Logger.flush_all_handlers()

    # Show logging statistics
    stats = get_logging_stats()
    print("\n=== Logging System Statistics ===")
    print(f"Active loggers: {stats.active_loggers}")
    print(f"Logs directory: {stats.logs_directory}")

    print("\n=== Log Files ===")
    for filename, info in stats.log_files.items():
        if info.error is None:
            print(f"  - {filename}")
            print(f"    Size: {info.size}")
            print(f"    Modified: {info.modified}")
        else:
            print(f"  - {filename} (Error: {info.error})")
