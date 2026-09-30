#!/usr/bin/env python3

"""
Utility functions for the search engine.
"""

import functools
import traceback
import urllib.parse
from typing import Any, Callable, TypeVar

from requests.utils import requote_uri

from constant.system import PROVIDER_SERVICE_PREFIX

from .logger import get_logger

logger = get_logger("tools")
F = TypeVar("F", bound=Callable[..., Any])


def handle_exceptions(
    default_result: Any = None, log_level: str = "error", reraise: bool = False, exception_types: tuple = (Exception,)
) -> Callable[[F], F]:
    """Decorator for consistent exception handling.

    Args:
        default_result: Value to return on exception
        log_level: Logging level (debug, info, warning, error, critical)
        reraise: Whether to reraise the exception after logging
        exception_types: Tuple of exception types to catch

    Returns:
        Decorated function with exception handling
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except exception_types as e:
                # Extract context information
                context = {
                    "function": func.__name__,
                    "module": func.__module__,
                    "args_count": len(args),
                }

                # Log the exception
                log_message = f"Exception in {func.__name__}: {str(e)}"
                log_func = getattr(logger, log_level, logger.error)
                log_func(f"{log_message} | Context: {context}")

                # Log traceback for debugging
                if log_level in ("error", "critical"):
                    logger.debug(f"Traceback for {func.__name__}:\n{traceback.format_exc()}")

                if reraise:
                    raise

                return default_result

        return wrapper

    return decorator


def trim(text: str) -> str:
    """Trim whitespace from text, return empty string if invalid."""
    if not text or type(text) != str:
        return ""
    return text.strip()


def isblank(text: str) -> bool:
    """Check if text is blank or invalid."""
    return not text or type(text) != str or not text.strip()


def encoding_url(url: str) -> str:
    """Normalize a URL to its ASCII wire form.

    Contract:
    - Hostname: non-ASCII labels are IDNA-encoded (``xn--…`` punycode applies
      to hostnames ONLY); userinfo and port are preserved.
    - Path/query/fragment: non-ASCII (e.g. CJK) and unsafe characters
      (spaces) are percent-encoded as UTF-8; existing valid %-escapes are
      kept as-is (never double-encoded to ``%25…``).
    - Well-formed pure-ASCII URLs return byte-identical, and the transform
      is idempotent: ``encoding_url(encoding_url(u)) == encoding_url(u)``.

    The previous punycode-the-whole-URL behaviour mangled CJK *paths*
    (``code/第四章`` -> ``code/xn--wbs215fqga``), making GitHub raw 404 on
    files that exist (96.3% of prod 404s measured 2026-09-30).
    """
    if not url:
        return ""

    text = url.strip()
    parts = urllib.parse.urlsplit(text)
    try:
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                _idna_encode_netloc(parts.netloc),
                requote_uri(parts.path),
                requote_uri(parts.query),
                requote_uri(parts.fragment),
            )
        )
    except (ValueError, UnicodeError):
        # Malformed host/port structure or un-IDNA-encodable label: hand the
        # original URL to requests, which quotes non-ASCII on the wire itself.
        return text


def _idna_encode_netloc(netloc: str) -> str:
    """IDNA-encode a non-ASCII hostname label-wise; ASCII hosts pass through.

    Preserves userinfo and port verbatim and never touches the host case, so
    an ASCII netloc (including IPv6 literals) returns byte-identical.
    """
    if not netloc or netloc.isascii():
        return netloc

    userinfo, sep, hostinfo = netloc.rpartition("@")
    host = hostinfo
    port_suffix = ""
    if ":" in hostinfo:
        host, _, port = hostinfo.partition(":")
        port_suffix = f":{port}"

    labels = ".".join(label.encode("idna").decode("ascii") for label in host.split("."))
    return f"{userinfo}{sep}{labels}{port_suffix}"


def get_service_name(provider: str) -> str:
    """Get service name for rate limiting

    Args:
        provider: Provider name to process

    Returns:
        str: Processed service name for rate limiting
    """
    name = trim(provider)
    if not name:
        return ""

    return f"{PROVIDER_SERVICE_PREFIX}:{name}"
