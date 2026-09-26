#!/usr/bin/env python3

"""
Core Exception Classes

This module provides essential exception classes for the application.
"""

from typing import Optional

from .enums import ErrorReason


class BaseError(Exception):
    """Base exception class for the application"""

    def __init__(
        self,
        message: str,
        reason: ErrorReason = ErrorReason.UNKNOWN,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.cause = cause

    def is_retryable(self) -> bool:
        """Check if error is retryable based on reason"""
        return self.reason.is_retryable()


class NetworkError(BaseError):
    """Network-related errors"""

    def __init__(self, message: str, reason: ErrorReason = ErrorReason.NETWORK_ERROR, **kwargs):
        super().__init__(message=message, reason=reason, **kwargs)


class RateLimiterDeniedError(ConnectionError):
    """A request was dropped by OUR OWN process-wide rate limiter.

    Raised by ``search/client.py::GitHubClient.get_with_headers`` when the
    shared ``github_api`` bucket starves a request past its bounded wait. The
    bucket is one process-wide template (per-credential keys) that the daily
    chain's overlapping scans all draw from, so a denial is self-inflicted
    concurrency, not an upstream fault — the distinct TYPE lets the stage layer
    log it at WARNING without string-matching the message.

    Subclasses ``ConnectionError`` deliberately: every existing retry predicate
    (``RetryCore.should_retry_error``) treats ConnectionError as retryable, and
    the stage retry machinery must keep requeueing these.
    """


class ValidationError(BaseError):
    """Input validation errors"""

    def __init__(self, message: str, field: Optional[str] = None, **kwargs):
        super().__init__(
            message=message,
            reason=ErrorReason.BAD_REQUEST,
            **kwargs,
        )
        self.field = field


# Additional exception classes for core functionality
class CoreException(BaseError):
    """Core system exception"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class BusinessLogicError(BaseError):
    """Business logic errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class ProcessingError(BaseError):
    """Processing-related errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class RetrievalError(BaseError):
    """Data retrieval errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class ConfigurationError(BaseError):
    """Configuration-related errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)
