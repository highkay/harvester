#!/usr/bin/env python3

"""
HTTP client utilities and GitHub-specific search functions for the search engine.
"""

import gzip
import itertools
import json
import os
import random
import re
import time
import traceback
import urllib.parse
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from core.models import Service
from tools.logger import get_logger
from tools.patterns import redact_api_keys_in_text
from tools.utils import encoding_url, isblank, trim

logger = get_logger("search")

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.trust_env = False
_HTTP_PROXY = ""
_RESPONSE_CACHE = None  # Optional[ResponseCache]
_QUOTA_TRACKER = None  # Optional[QuotaTracker]
_LINK_INDEX = None  # Optional[LinkIndex]
_SKIP_KNOWN_LINKS = False
_TEXT_MATCH = True
_CACHE_TTL_SEARCH = 60
_CACHE_TTL_CORE = 300
_ALLOWED_SEARCH_TYPES = frozenset({"code", "issues", "commits"})
# Prefer text-match fragments so SearchStage can regex keys from API JSON
_GITHUB_SEARCH_ACCEPT = "application/vnd.github.text-match+json"


def _new_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


# Direct (never proxied) session for provider API validation.
# Never mutated by set_proxy(); the proxy is for GitHub fetches only.
_DIRECT_SESSION = _new_session()


def _remount_edge_adapter() -> None:
    """Re-attach edge routing after session recreation."""
    try:
        from search.github.adapter import mount_edge_adapter
        from search.github.transport import get_edge_pool

        pool = get_edge_pool()
        if pool is not None:
            mount_edge_adapter(_HTTP_SESSION, pool)
    except Exception as e:
        logger.debug(f"[edge] remount skipped: {e}")


def http_error_status(error: requests.exceptions.HTTPError) -> int:
    response = getattr(error, "response", None)
    return response.status_code if response is not None else 0


def http_error_message(error: requests.exceptions.HTTPError) -> str:
    response = getattr(error, "response", None)
    if response is None:
        return str(error)

    try:
        message = response.text.removeprefix("\n").removesuffix("\n")
        if len(message) > 300:
            message = message[:300]
    except Exception:
        message = ""

    return message or response.reason or str(error)


def set_proxy(proxy: Optional[str]) -> None:
    """Configure the process-wide requests session used by search HTTP requests."""
    global _HTTP_SESSION, _HTTP_PROXY

    proxy = trim(proxy)

    if not proxy:
        _HTTP_PROXY = ""
        _HTTP_SESSION = _new_session()
        _remount_edge_adapter()
        logger.info("HTTP proxy disabled")
        return

    parsed = urllib.parse.urlparse(proxy)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError("proxy scheme must be one of: http, https, socks5")
    if not parsed.hostname:
        raise ValueError("proxy must include a host")

    try:
        parsed.port
    except ValueError as e:
        raise ValueError(f"invalid proxy port: {e}") from e

    _HTTP_SESSION = _new_session(proxy)
    _HTTP_PROXY = proxy
    _remount_edge_adapter()
    logger.info(f"HTTP proxy enabled: {scheme}://{parsed.hostname}:{parsed.port or ''}")


def configure_github_transport(
    *,
    workspace: str,
    proxy: str = "",
    transport_config: Any = None,
) -> None:
    """
    Initialize edge pool, response cache, link index and quota tracker.

    transport_config: optional GithubTransportConfig from config.schemas
    """
    global _RESPONSE_CACHE, _QUOTA_TRACKER, _LINK_INDEX, _SKIP_KNOWN_LINKS
    global _TEXT_MATCH, _CACHE_TTL_SEARCH, _CACHE_TTL_CORE, _GITHUB_SEARCH_ACCEPT

    from search.github.adapter import mount_edge_adapter
    from search.github.cache import ResponseCache
    from search.github.index import init_link_index
    from search.github.quota import QuotaTracker
    from search.github.transport import init_github_transport

    edge_enabled = True
    edge_source = "auto"
    hosts_url = "https://hosts.ohmygh.com/v1/hosts"
    gx_bin = "gx"
    refresh_interval = 3600
    max_edges = 32
    verify = True
    prefer_over_proxy = False
    doh_enabled = True
    doh_endpoints = None
    cache_enabled = True
    cache_dir = "cache/github_api"
    max_entries = 1000
    quota_tracking = True
    index_enabled = True
    index_dir = "cache/search_index"
    skip_known = False
    text_match = True

    if transport_config is not None:
        edge = getattr(transport_config, "edge_pool", None)
        doh = getattr(transport_config, "doh", None)
        cache = getattr(transport_config, "cache", None)
        index = getattr(transport_config, "index", None)
        if edge is not None:
            edge_enabled = bool(edge.enabled)
            edge_source = edge.source
            hosts_url = edge.hosts_url
            gx_bin = edge.gx_bin
            refresh_interval = edge.refresh_interval
            max_edges = edge.max_edges
            verify = edge.verify
            prefer_over_proxy = edge.prefer_over_proxy
        if doh is not None:
            doh_enabled = bool(doh.enabled)
            doh_endpoints = list(doh.endpoints or [])
        if cache is not None:
            cache_enabled = bool(cache.enabled)
            cache_dir = cache.directory
            max_entries = cache.max_entries
            _CACHE_TTL_SEARCH = int(cache.ttl_search)
            _CACHE_TTL_CORE = int(cache.ttl_core)
        if index is not None:
            index_enabled = bool(index.enabled)
            index_dir = index.directory
            skip_known = bool(index.skip_known_links)
        quota_tracking = bool(getattr(transport_config, "quota_tracking", True))
        text_match = bool(getattr(transport_config, "text_match", True))

    pool = init_github_transport(
        workspace=workspace,
        proxy=proxy,
        edge_enabled=edge_enabled,
        edge_source=edge_source,
        hosts_url=hosts_url,
        gx_bin=gx_bin,
        refresh_interval=refresh_interval,
        max_edges=max_edges,
        verify=verify,
        prefer_over_proxy=prefer_over_proxy,
        doh_enabled=doh_enabled,
        doh_endpoints=doh_endpoints,
    )
    if pool is not None:
        mount_edge_adapter(_HTTP_SESSION, pool)

    if not os.path.isabs(cache_dir):
        cache_path = os.path.join(workspace, cache_dir)
    else:
        cache_path = cache_dir
    _RESPONSE_CACHE = ResponseCache(directory=cache_path, max_entries=max_entries, enabled=cache_enabled)
    _QUOTA_TRACKER = QuotaTracker(enabled=quota_tracking)

    if not os.path.isabs(index_dir):
        index_path = os.path.join(workspace, index_dir)
    else:
        index_path = index_dir
    _LINK_INDEX = init_link_index(index_path, enabled=index_enabled)
    _SKIP_KNOWN_LINKS = bool(skip_known and index_enabled)
    _TEXT_MATCH = text_match
    _GITHUB_SEARCH_ACCEPT = (
        "application/vnd.github.text-match+json" if text_match else "application/vnd.github+json"
    )
    logger.info(
        f"[transport] configured edge={'on' if pool else 'off'}, "
        f"cache={'on' if cache_enabled else 'off'}, index={'on' if index_enabled else 'off'}, "
        f"quota={'on' if quota_tracking else 'off'}, text_match={'on' if text_match else 'off'}"
    )


def get_link_index():
    """Return the process-wide link index if configured."""
    return _LINK_INDEX


def should_skip_known_links() -> bool:
    return bool(_SKIP_KNOWN_LINKS and _LINK_INDEX is not None and _LINK_INDEX.enabled)


def request(method: str, url: str, timeout: float = 10, use_proxy: bool = True, **kwargs: Any) -> requests.Response:
    """Send a request through the configured global session."""
    session = _HTTP_SESSION if use_proxy else _DIRECT_SESSION
    response = session.request(method=method, url=url, timeout=max(1, timeout), **kwargs)
    response.raise_for_status()
    return response


from constant.search import API_RESULTS_PER_PAGE, WEB_RESULTS_PER_PAGE
from constant.system import (
    CHAT_RETRY_INTERVAL,
    COLLECT_RETRY_INTERVAL,
    DEFAULT_HEADERS,
    DEFAULT_QUESTION,
    GITHUB_API_INTERVAL,
    GITHUB_API_RATE_LIMIT_BACKOFF,
    GITHUB_API_TIMEOUT,
    GITHUB_WEB_COUNT_DELAY_MAX,
    NO_RETRY_ERROR_CODES,
    SERVICE_TYPE_GITHUB_API,
    SERVICE_TYPE_GITHUB_WEB,
)
from core.exceptions import NetworkError, ValidationError
from core.models import RateLimitConfig
from core.types import IAuthProvider
from tools.coordinator import get_user_agent
from tools.ratelimit import RateLimiter
from tools.resources import managed_network
from tools.retry import network_retry
from tools.state import (
    GithubCredentialLimited,
    credential_bucket_key,
    github_credential_state,
    mask_credential,
)
from tools.utils import handle_exceptions


class GitHubClient:
    """GitHub-specific HTTP client with rate limiting and dependency injection"""

    def __init__(
        self,
        limiter: Optional[RateLimiter] = None,
        resource_provider: Optional[IAuthProvider] = None,
        limits: Optional[Dict[str, RateLimitConfig]] = None,
    ):
        """Initialize GitHub client

        Args:
            limiter: Rate limiter for request throttling
            resource_provider: Resource provider for credentials and user agents
        """
        self.limiter = limiter
        self.resource_provider = resource_provider
        self.limits = limits or {}

    def _get_user_agent(self) -> str:
        """Get User-Agent string using dependency injection or fallback

        Returns:
            str: User-Agent string
        """
        if self.resource_provider:
            return self.resource_provider.get_user_agent()
        else:
            # Fallback to global function for backward compatibility
            return get_user_agent()

    def _service(self, url: str) -> Optional[str]:
        """Detect service type from URL"""
        if not url:
            return None

        url_lower = url.lower()
        if "api.github.com" in url_lower:
            return SERVICE_TYPE_GITHUB_API
        elif "github.com" in url_lower:
            return SERVICE_TYPE_GITHUB_WEB

        return None

    def _bucket_name(self, service: str, credential: Optional[str] = None) -> str:
        """Get the limiter bucket name for a service and credential"""
        if service and credential:
            return credential_bucket_key(service, credential)
        return service

    def _ensure_bucket(self, service: str, credential: Optional[str] = None) -> str:
        """Create a per-credential bucket from the service template"""
        bucket_name = self._bucket_name(service, credential)
        if (
            self.limiter
            and service
            and credential
            and service in self.limits
            and not self.limiter._get_bucket(bucket_name)
        ):
            self.limiter.add_service(bucket_name, self.limits[service])
        return bucket_name

    def _limit(self, service: str, credential: Optional[str] = None) -> bool:
        """Apply rate limiting, return True if request can proceed"""
        if not self.limiter or not service:
            return True

        bucket_name = self._ensure_bucket(service, credential)

        # Try immediate acquisition
        if self.limiter.acquire(bucket_name):
            return True

        # Wait for tokens
        wait = self.limiter.wait_time(bucket_name)
        if wait > 0:
            bucket = self.limiter._get_bucket(bucket_name)
            max_value = bucket.burst if bucket else "unknown"
            label = f"{service}/{mask_credential(credential)}" if credential else service
            logger.info(f"Rate limit hit for {label}, waiting {wait:.2f}s, max: {max_value}")
            time.sleep(wait)
            return self.limiter.acquire(bucket_name)

        return False

    def _report(self, service: str, success: bool, credential: Optional[str] = None) -> None:
        """Report request result for adaptive adjustment"""
        if self.limiter and service:
            self.limiter.report_result(self._bucket_name(service, credential), success)

    def _handle_error(self, service: str, status: int, message: str) -> None:
        """Handle GitHub-specific errors"""
        if status == 403 and service == SERVICE_TYPE_GITHUB_API:
            if "rate limit" in message.lower():
                logger.info("GitHub API rate limit exceeded, backing off")
                time.sleep(GITHUB_API_RATE_LIMIT_BACKOFF)  # Wait for rate limit reset

    def get(
        self,
        url: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retries: int = 3,
        interval: float = 0,
        timeout: float = 10,
        credential: Optional[str] = None,
    ) -> str:
        """Make rate-limited HTTP GET request to GitHub"""
        content, _ = self.get_with_headers(url, headers, params, retries, interval, timeout, credential)
        return content

    def get_with_headers(
        self,
        url: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retries: int = 3,
        interval: float = 0,
        timeout: float = 10,
        credential: Optional[str] = None,
        use_cache: bool = True,
    ) -> Tuple[str, Dict[str, str]]:
        """Make rate-limited HTTP GET request to GitHub and return headers"""
        service = self._service(url)
        headers = dict(headers or {})

        # Build final URL early so cache keys match the real request
        encoded_url = self._build_url(url, params)

        # Server-side resource quota (search/core) — wait before burning a request
        if service == SERVICE_TYPE_GITHUB_API and _QUOTA_TRACKER is not None and credential:
            from search.github.quota import resource_from_url

            _QUOTA_TRACKER.wait_if_needed(credential, resource_from_url(encoded_url))

        # TTL cache fast path (API only)
        cache_key = ""
        cached_entry = None
        if (
            use_cache
            and service == SERVICE_TYPE_GITHUB_API
            and _RESPONSE_CACHE is not None
            and _RESPONSE_CACHE.enabled
        ):
            from search.github.cache import ResponseCache

            cache_key = ResponseCache.make_key(
                "GET",
                encoded_url,
                ResponseCache.fingerprint_auth(credential or ""),
            )
            cached_entry = _RESPONSE_CACHE.get(cache_key)
            if cached_entry and cached_entry.fresh:
                logger.debug(f"[cache] TTL hit for {encoded_url}")
                return cached_entry.body, cached_entry.headers
            if cached_entry and cached_entry.etag:
                headers.setdefault("If-None-Match", cached_entry.etag)

        # Apply rate limiting
        if service and not self._limit(service, credential):
            logger.debug(f"Rate limit acquisition failed for {service}")
            return "", {}

        content, response_headers, status = self._http_get(
            url, headers, params, retries, interval, timeout, return_status=True
        )

        # 304 Not Modified → serve cached body
        if status == 304 and cached_entry is not None:
            if _RESPONSE_CACHE is not None and cache_key:
                _RESPONSE_CACHE.touch(cache_key, response_headers)
            logger.debug(f"[cache] ETag revalidated (304) for {encoded_url}")
            content = cached_entry.body
            # Prefer fresh rate-limit headers from the 304 response
            merged = dict(cached_entry.headers)
            merged.update(response_headers or {})
            response_headers = merged
            status = 200

        success = bool(content) or status == 304

        # Report result for adaptive adjustment
        self._report(service, success, credential)

        if service == SERVICE_TYPE_GITHUB_API and _QUOTA_TRACKER is not None:
            _QUOTA_TRACKER.update_from_headers(credential or "", response_headers or {})

        if (
            use_cache
            and service == SERVICE_TYPE_GITHUB_API
            and success
            and content
            and _RESPONSE_CACHE is not None
            and _RESPONSE_CACHE.enabled
            and cache_key
            and status == 200
        ):
            from search.github.cache import classify_ttl

            ttl = classify_ttl(encoded_url, _CACHE_TTL_SEARCH, _CACHE_TTL_CORE)
            _RESPONSE_CACHE.put(
                cache_key=cache_key,
                url=encoded_url,
                body=content,
                headers=response_headers or {},
                ttl=ttl,
                status=200,
            )

        if service and credential and self.is_rate_limited_content(service, content):
            self.mark_credential_limited(
                service=service,
                credential=credential,
                headers=response_headers,
                content=content,
                reason="response content indicates rate limit",
            )

        if service and credential and success:
            github_credential_state.mark_success(service, credential)

        return content, response_headers

    def mark_credential_limited(
        self,
        service: str,
        credential: str,
        headers: Optional[Dict[str, str]] = None,
        content: str = "",
        reason: str = "",
    ) -> None:
        """Mark a credential as rate limited and raise a retry signal"""
        wait = self._extract_wait(headers, content)
        wait = github_credential_state.mark_limited(service, credential, wait)
        label = "API token" if service == SERVICE_TYPE_GITHUB_API else "Web session"
        masked = mask_credential(credential)
        logger.warning(f"[GithubCrawl] GitHub {label} rate limited: {masked}, cooling down {wait:.1f}s")
        raise GithubCredentialLimited(service=service, credential=credential, wait=wait, reason=reason)

    def is_rate_limited_content(self, service: str, content: str) -> bool:
        """Detect GitHub rate-limit responses from response content"""
        if isblank(content):
            return False

        if service == SERVICE_TYPE_GITHUB_WEB:
            return bool(re.search(r"Search failed\. Please try again later\.", content, flags=re.I))

        text = content
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                text = str(data.get("message", ""))
        except Exception:
            pass

        patterns = [
            r"rate limit",
            r"secondary rate limit",
            r"abuse detection",
            r"please wait",
            r"try again later",
        ]
        return any(re.search(pattern, text, flags=re.I) for pattern in patterns)

    def _http_get(
        self,
        url: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retries: int = 3,
        interval: float = 1.0,
        timeout: float = 10,
        return_status: bool = False,
    ):
        """HTTP GET request that preserves response headers.

        When return_status is True, returns (content, headers, status_code).
        """
        if isblank(url):
            raise ValidationError("URL cannot be empty", field="url")

        headers = headers or DEFAULT_HEADERS.copy()
        timeout = max(1, timeout)
        retries = max(1, retries)
        interval = max(0.1, interval)
        encoded_url = self._build_url(url, params)
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                # Use session directly so 304 is not raised as HTTPError
                response = _HTTP_SESSION.request(
                    method="GET",
                    url=encoded_url,
                    headers=headers,
                    timeout=timeout,
                )
                status = response.status_code
                response_headers = dict(response.headers)

                if status == 304:
                    result = ("", response_headers, 304) if return_status else ("", response_headers)
                    return result

                if status >= 400:
                    # Reuse error classification path
                    http_error = requests.exceptions.HTTPError(
                        f"HTTP {status}", response=response
                    )
                    raise http_error

                body = self._decode_response(response.content)
                if return_status:
                    return body, response_headers, status
                return body, response_headers
            except GithubCredentialLimited:
                raise
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                reason = http_error_message(e)
                response_headers = dict(e.response.headers) if e.response is not None else {}
                if self._is_http_rate_limited(code, reason):
                    service = self._service(url)
                    credential = self._credential_from_headers(headers or {}, service)
                    if service and credential:
                        self.mark_credential_limited(service, credential, response_headers, reason, reason)

                if code == 429 or code >= 500:
                    last_error = ConnectionError(f"HTTP {code} error: {reason}")
                elif code == 404:
                    raise FileNotFoundError(f"File not found (HTTP {code}), url: {url}")
                elif code in (401, 403):
                    # Prefer resource quota wait over hard auth failure when headers say so
                    if self._is_http_rate_limited(code, reason):
                        last_error = ConnectionError(f"HTTP {code} error: {reason}")
                    else:
                        raise NetworkError(f"Authentication failed (HTTP {code})")
                else:
                    raise NetworkError(f"HTTP {code} error: {reason}")
            except requests.exceptions.Timeout as e:
                last_error = TimeoutError(f"Request timeout: {e}")
            except requests.exceptions.RequestException as e:
                last_error = ConnectionError(f"Request error: {e}")
            except Exception as e:
                if "timeout" in str(e).lower():
                    last_error = TimeoutError(f"Request timeout: {e}")
                else:
                    raise NetworkError(f"Unexpected error: {e}")

            if attempt < retries - 1 and last_error:
                time.sleep(interval * (2**attempt) + random.random() * 0.1)

        if last_error:
            raise last_error
        if return_status:
            return "", {}, 0
        return "", {}

    def _build_url(self, url: str, params: Optional[Dict] = None) -> str:
        encoded_url = encoding_url(url)
        if params and isinstance(params, dict):
            data = urllib.parse.urlencode(params)
            separator = "&" if "?" in encoded_url else "?"
            encoded_url += f"{separator}{data}"
        return encoded_url

    def _decode_response(self, content: bytes) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return gzip.decompress(content).decode("utf-8")
            except Exception:
                raise NetworkError("Failed to decode response content")

    def _is_http_rate_limited(self, code: int, message: str) -> bool:
        text = message or ""
        return code == 429 or (code == 403 and re.search(r"rate limit|abuse detection|please wait", text, re.I))

    def _credential_from_headers(self, headers: Dict, service: Optional[str]) -> str:
        if service == SERVICE_TYPE_GITHUB_API:
            auth = trim(headers.get("Authorization", ""))
            if auth.lower().startswith("bearer "):
                return auth[7:]
            return auth

        cookie = trim(headers.get("Cookie", ""))
        match = re.search(r"user_session=([^;]+)", cookie, flags=re.I)
        return match.group(1) if match else ""

    def _extract_wait(self, headers: Optional[Dict[str, str]], content: str = "") -> Optional[float]:
        wait = self._wait_from_headers(headers or {})
        if wait and wait > 0:
            return wait
        return self._wait_from_content(content)

    def _wait_from_headers(self, headers: Dict[str, str]) -> Optional[float]:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        retry_after = trim(normalized.get("retry-after", ""))
        if retry_after:
            if retry_after.isdigit():
                return float(retry_after)
            try:
                retry_at = parsedate_to_datetime(retry_after)
                return max(0.0, retry_at.timestamp() - time.time())
            except Exception:
                pass

        reset_at = trim(normalized.get("x-ratelimit-reset", ""))
        if reset_at.isdigit():
            return max(0.0, float(reset_at) - time.time())

        return None

    def _wait_from_content(self, content: str) -> Optional[float]:
        text = content or ""
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                text = str(data.get("message", text))
        except Exception:
            pass

        match = re.search(r"(?:retry after|try again in|wait)\s+(\d+)\s*(second|minute|hour)s?", text, flags=re.I)
        if match:
            value = float(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith("hour"):
                return value * 3600
            if unit.startswith("minute"):
                return value * 60
            return value

        if re.search(r"few minutes", text, flags=re.I):
            return 180.0

        return None


# Global GitHub client instance
_github_client: Optional[GitHubClient] = None


def init_github_client(limits: Dict[str, RateLimitConfig]) -> None:
    """Initialize GitHub client with rate limiter"""
    global _github_client
    limiter = RateLimiter(limits)
    _github_client = GitHubClient(limiter, limits=limits)
    logger.info("GitHub client initialized with rate limiting")


def get_github_client() -> GitHubClient:
    """Get GitHub client instance"""
    if not _github_client:
        # Fallback client without rate limiting
        return GitHubClient()
    return _github_client


def get_github_stats() -> Dict[str, Dict[str, float]]:
    """Get rate limiter statistics"""
    if _github_client and _github_client.limiter:
        stats = _github_client.limiter.get_stats()
        # Convert back to dict format for backward compatibility
        result = {}
        for service, bucket_stats in stats.services.items():
            result[service] = {
                "rate": bucket_stats.rate,
                "burst": bucket_stats.burst,
                "tokens": bucket_stats.tokens,
                "utilization": bucket_stats.utilization,
                "consecutive_success": bucket_stats.consecutive_success,
                "consecutive_failures": bucket_stats.consecutive_failures,
                "adaptive": bucket_stats.adaptive,
                "original_rate": bucket_stats.original_rate,
            }
        return result
    return {}


def log_github_stats() -> None:
    """Log current rate limiter statistics"""
    if not _github_client or not _github_client.limiter:
        return

    stats = _github_client.limiter.get_stats()
    for service, bucket_stats in stats.services.items():
        logger.info(
            f"Rate limiter [{service}]: rate={bucket_stats.rate:.2f}/s, "
            f"tokens={bucket_stats.tokens:.1f}/{bucket_stats.burst}, "
            f"utilization={bucket_stats.utilization:.1%}"
        )


@network_retry
def http_get(
    url: str,
    headers: Optional[Dict] = None,
    params: Optional[Dict] = None,
    retries: int = 3,
    interval: float = 1.0,
    timeout: float = 10,
    use_proxy: bool = True,
) -> str:
    """HTTP GET request with configurable retry handling

    Args:
        url: URL to request
        headers: HTTP headers
        params: URL parameters
        retries: Number of retry attempts (default: 3, minimum: 1)
        interval: Initial delay between retries in seconds (default: 1.0, minimum: 0.1)
        timeout: Request timeout in seconds

    Returns:
        str: Response content

    Raises:
        ValidationError: For invalid input
        NetworkError: For network-related issues
        FileNotFoundError: For access resource not exists
        ConnectionError: For connection failures (will be retried)
        TimeoutError: For timeout errors (will be retried)

    Note:
        The @network_retry decorator automatically extracts retries and interval
        parameters to configure retry behavior dynamically. Retry logic uses
        exponential backoff with jitter for optimal performance.
    """

    # Input validation
    if isblank(url):
        raise ValidationError("URL cannot be empty", field="url")

    # Setup request
    headers = headers or DEFAULT_HEADERS.copy()
    timeout = max(1, timeout)

    try:
        # Encode URL and add parameters
        encoded_url = encoding_url(url)
        if params and isinstance(params, dict):
            data = urllib.parse.urlencode(params)
            separator = "&" if "?" in encoded_url else "?"
            encoded_url += f"{separator}{data}"

        with managed_network(
            request("GET", encoded_url, headers=headers, timeout=timeout, use_proxy=use_proxy), "http_connection"
        ) as response:
            # Handle response
            content = response.content
            status_code = response.status_code

            if status_code != 200:
                raise NetworkError(f"HTTP {status_code} error for URL: {url}")

            # Decode content
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    return gzip.decompress(content).decode("utf-8")
                except Exception:
                    raise NetworkError("Failed to decode response content")

    except requests.exceptions.HTTPError as e:
        # Handle HTTP errors with basic classification
        code = http_error_status(e)
        reason = http_error_message(e)
        if code == 429:
            # Rate limit errors should be retried
            raise ConnectionError(f"Rate limit exceeded (HTTP {code})")
        elif code == 404:
            raise FileNotFoundError(f"File not found (HTTP {code}), url: {url}")
        elif code in (401, 403):
            # Auth errors should not be retried
            raise NetworkError(f"Authentication failed (HTTP {code})")
        elif code >= 500:
            # Server errors should be retried
            raise ConnectionError(f"Server error (HTTP {code}): {reason}")
        else:
            # Client errors should not be retried
            raise NetworkError(f"HTTP {code} error: {reason}")

    except requests.exceptions.Timeout as e:
        raise TimeoutError(f"Request timeout: {e}")

    except requests.exceptions.RequestException as e:
        # URL errors are usually network-related and should be retried
        raise ConnectionError(f"Request error: {e}")

    except Exception as e:
        # Classify unknown errors
        if "timeout" in str(e).lower():
            # Timeout errors should be retried
            raise TimeoutError(f"Request timeout: {e}")
        else:
            # Other errors should not be retried
            raise NetworkError(f"Unexpected error: {e}")


def chat(
    url: str,
    headers: Dict,
    model: str = "",
    params: Optional[Dict] = None,
    retries: int = 2,
    timeout: int = 10,
    use_proxy: bool = True,
) -> Tuple[int, str]:
    """Make chat API request with retry logic."""

    def output(code: int, message: str, debug: bool = False) -> None:
        safe_headers = redact_api_keys_in_text(str(headers))
        safe_message = redact_api_keys_in_text(str(message))
        text = f"[chat] failed to request URL: {url}, headers: {safe_headers}, status code: {code}, message: {safe_message}"
        if debug:
            logger.debug(text)
        else:
            logger.error(text)

    url, model = trim(url), trim(model)
    if not url:
        logger.error("[chat] url cannot be empty")
        return 400, None

    if not isinstance(headers, dict):
        logger.error("[chat] headers must be a dict")
        return 400, None
    elif len(headers) == 0:
        headers["content-type"] = "application/json"

    if not params or not isinstance(params, dict):
        if not model:
            logger.error("[chat] model cannot be empty")
            return 400, None

        params = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": DEFAULT_QUESTION}],
        }

    payload = json.dumps(params).encode("utf8")
    timeout = max(1, timeout)
    retries = max(1, retries)
    code, message, attempt = 400, None, 0

    while attempt < retries:
        try:
            with request("POST", url, data=payload, headers=headers, timeout=timeout, use_proxy=use_proxy) as response:
                code = response.status_code
                message = response.text
                break
        except requests.exceptions.HTTPError as e:
            code = http_error_status(e)
            if code != 401:
                try:
                    # read response body
                    message = http_error_message(e)

                    # not a json string, use reason instead
                    if not message.startswith("{") or not message.endswith("}"):
                        message = e.response.reason if e.response is not None else str(e)
                except Exception:
                    message = str(e)

                # print http status code and error message
                output(code=code, message=message, debug=False)

            if code in NO_RETRY_ERROR_CODES:
                break
        except Exception:
            output(code=code, message=traceback.format_exc(), debug=True)

        attempt += 1
        time.sleep(CHAT_RETRY_INTERVAL)

    return code, message


def normalize_search_type(search_type: str = "code") -> str:
    """Normalize and validate GitHub search type."""
    value = trim(search_type).lower() or "code"
    if value not in _ALLOWED_SEARCH_TYPES:
        logger.warning(f"[search] unsupported search_type '{search_type}', falling back to code")
        return "code"
    return value


def search_github_web(query: str, session: str, page: int, search_type: str = "code") -> str:
    """Use github web search instead of rest api due to it not support regex syntax."""
    if page <= 0 or isblank(session) or isblank(query):
        return ""

    # Web HTML extraction currently only implements code blob links
    search_type = normalize_search_type(search_type)
    if search_type != "code":
        logger.warning(f"[search] web search only supports type=code; got {search_type}")
        search_type = "code"

    url = f"https://github.com/search?o=desc&p={page}&type={search_type}&q={query}"
    headers: Dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Referer": "https://github.com",
        "User-Agent": get_user_agent(),
        "Cookie": f"user_session={session}",
    }

    client = get_github_client()
    content = client.get(url=url, headers=headers, credential=session)
    if re.search(r"<title>Sign in to GitHub · GitHub</title>", content, flags=re.I):
        logger.error(
            f"[GithubCrawl] Session has expired: {mask_credential(session)}, "
            "please provide a valid session and try again"
        )
        return ""

    return content


def _extract_api_links(items: List[Any], search_type: str = "code") -> List[str]:
    """Extract html_url links from GitHub search API items."""
    links: set[str] = set()
    for item in items:
        if not item or not isinstance(item, dict):
            continue
        link = item.get("html_url", "")
        if isblank(link) and search_type == "commits":
            # Prefer html_url; fall back to API url only as last resort
            link = item.get("html_url") or item.get("url", "")
        if isblank(link):
            link = item.get("html_url") or item.get("url", "")
        if isblank(link):
            continue
        # Prefer human-facing pages for gather
        if str(link).startswith("https://api.github.com/"):
            continue
        links.add(link)
    return list(links)


def _api_search_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": _GITHUB_SEARCH_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_search_url(query: str, page: int, peer_page: int, search_type: str) -> str:
    if search_type == "code":
        return (
            f"https://api.github.com/search/code?q={query}"
            f"&sort=indexed&order=desc&per_page={peer_page}&page={page}"
        )
    return f"https://api.github.com/search/{search_type}?q={query}&order=desc&per_page={peer_page}&page={page}"


def search_github_api(
    query: str,
    token: str,
    page: int = 1,
    peer_page: int = API_RESULTS_PER_PAGE,
    search_type: str = "code",
) -> List[str]:
    """Authenticated GitHub search API. Default type=code (needs token)."""
    links, _, _ = search_api_with_count(
        query=query, token=token, page=page, peer_page=peer_page, search_type=search_type
    )
    return links


def search_web_with_count(
    query: str,
    session: str,
    page: int = 1,
    callback: Optional[Callable[[List[str], str], None]] = None,
    search_type: str = "code",
) -> Tuple[List[str], int, str]:
    """
    Search GitHub web and return results, total count, and content.
    Returns: (results_list, total_count, content)
    """
    if page <= 0 or isblank(session) or isblank(query):
        return [], 0, ""

    # Get results from web search
    content = search_github_web(query, session, page, search_type=search_type)
    if isblank(content):
        return [], 0, ""

    # Extract links from content
    try:
        regex = r'href="(/[^\s"]+/blob/(?:[^"]+)?)#L\d+"'
        groups = re.findall(regex, content, flags=re.I)
        uris = list(set(groups)) if groups else []
        links = set()

        for uri in uris:
            links.add(f"https://github.com{uri}")

        results = list(links)
    except Exception:
        results = []

    # Call extract callback if provided
    if callback and isinstance(callback, Callable) and results:
        try:
            callback(results, content)
        except Exception as e:
            logger.error(f"[search] callback failed: {e}")

    # Get total count (only for first page to avoid redundant calls)
    if page == 1:
        total = estimate_web_total(query, session, content)
    else:
        # For non-first pages, we don't need total count, use 0 as placeholder
        total = 0

    return results, total, content


def search_api_with_count(
    query: str,
    token: str,
    page: int = 1,
    peer_page: int = API_RESULTS_PER_PAGE,
    search_type: str = "code",
) -> Tuple[List[str], int, str]:
    """
    Search GitHub API and return results, total count, and raw content.

    Args:
        query: Search query string
        token: GitHub API token for authentication
        page: Page number to retrieve (default: 1)
        peer_page: Results per page (default: API_RESULTS_PER_PAGE)
        search_type: code | issues | commits

    Returns:
        Tuple containing:
        - List[str]: List of GitHub URLs found
        - int: Total count of results available
        - str: Raw JSON response content
    """
    if isblank(token) or isblank(query):
        return [], 0, ""

    search_type = normalize_search_type(search_type)
    peer_page, page = min(max(peer_page, 1), API_RESULTS_PER_PAGE), max(1, page)
    url = _api_search_url(query, page, peer_page, search_type)
    headers = _api_search_headers(token)

    client = get_github_client()
    content = client.get(
        url=url,
        headers=headers,
        interval=GITHUB_API_INTERVAL,
        timeout=GITHUB_API_TIMEOUT,
        credential=token,
    )
    if isblank(content):
        return [], 0, ""

    try:
        data = json.loads(content)
        items = data.get("items", [])
        total = data.get("total_count", 0)
        # Flatten text_matches / issue body / commit message into content so regex
        # key extraction works even before gather downloads full pages.
        enriched = _enrich_search_content_for_extract(content, items, search_type)
        return _extract_api_links(items, search_type), total, enriched
    except Exception:
        return [], 0, content or ""


def _enrich_search_content_for_extract(content: str, items: List[Any], search_type: str) -> str:
    """Append high-signal text fields so collect() can regex keys from API JSON."""
    fragments: List[str] = [content or ""]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        # text-match fragments (when Accept: text-match+json)
        for match in item.get("text_matches") or []:
            if isinstance(match, dict) and match.get("fragment"):
                fragments.append(str(match["fragment"]))
        if search_type == "issues":
            if item.get("title"):
                fragments.append(str(item["title"]))
            if item.get("body"):
                fragments.append(str(item["body"]))
        elif search_type == "commits":
            commit = item.get("commit") or {}
            if isinstance(commit, dict):
                if commit.get("message"):
                    fragments.append(str(commit["message"]))
        elif search_type == "code":
            # path can contain env-like names; name is filename
            if item.get("path"):
                fragments.append(str(item["path"]))
            if item.get("name"):
                fragments.append(str(item["name"]))
    return "\n".join(fragments)


def search_with_count(
    query: str,
    session: str,
    page: int,
    with_api: bool,
    peer_page: int,
    callback: Optional[Callable[[List[str], str], None]] = None,
    search_type: str = "code",
) -> Tuple[List[str], int, str]:
    """
    Unified search interface that returns results, total count, and content.
    Returns: (results_list, total_count, content)
    """
    keywords = urllib.parse.quote_plus(query)
    if with_api:
        return search_api_with_count(keywords, session, page, peer_page, search_type=search_type)
    else:
        return search_web_with_count(keywords, session, page, callback, search_type=search_type)


@handle_exceptions(default_result=0, log_level="error")
def get_total_num(query: str, token: str) -> int:
    """Get total number of results from GitHub API."""
    if isblank(token) or isblank(query):
        return 0

    url = f"https://api.github.com/search/code?q={query}&sort=indexed&order=desc&per_page=20&page=1"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    client = get_github_client()
    content = client.get(url=url, headers=headers, interval=1, credential=token)
    data = json.loads(content)
    return data.get("total_count", 0)


def estimate_web_total(query: str, session: str, content: Optional[str] = None) -> int:
    """
    Get total count for web search using GitHub's blackbird_count API.
    Performs a single search and then queries the count API.
    """
    if isblank(session) or isblank(query):
        return 0

    try:
        message = urllib.parse.unquote_plus(query)
    except Exception:
        message = query

    try:
        if content is None:
            # Perform initial search to trigger count calculation and get content for fallback
            content = search_github_web(query=query, session=session, page=1)

        content = trim(content)
        if not content:
            logger.warning(f"[search] initial search failed for query: {message}, using conservative estimate")
            # Conservative estimate
            return WEB_RESULTS_PER_PAGE

        # Check if query is already encoded to avoid double encoding
        if "%" in query and any(c in query for c in ["%2F", "%5B", "%5D", "%7B", "%7D"]):
            encoded = query.replace(" ", "+")
        else:
            encoded = urllib.parse.quote_plus(query)

        # Query the blackbird_count API
        url = f"https://github.com/search/blackbird_count?saved_searches=^&q={encoded}"
        headers = {
            "User-Agent": get_user_agent(),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"https://github.com/search?q={encoded}^&type=code",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": f"user_session={session}",
        }

        # Random delay to ensure count is calculated
        time.sleep(random.random() * GITHUB_WEB_COUNT_DELAY_MAX)

        client = get_github_client()
        response = client.get(url=url, headers=headers, interval=1, credential=session)
        if response:
            data = json.loads(response)
            if not data.get("failed", True):
                count = data.get("count", 0)
                mode = data.get("mode", "unknown")
                logger.info(f"[search] got {count} results, mode: {mode}, query: {message}")

                # Return count if valid, otherwise try page extraction
                return count if count > 0 else extract_count_from_page(content, query)

        # Fallback: extract count from search page
        return extract_count_from_page(content, query)

    except GithubCredentialLimited:
        raise
    except Exception as e:
        logger.error(f"[search] estimation failed for query: {message}, error: {e}, using conservative estimate")
        # Conservative estimate
        return WEB_RESULTS_PER_PAGE


def extract_count_from_page(content: str, query: str) -> int:
    """Extract result count from GitHub search page content."""
    if isblank(content):
        return WEB_RESULTS_PER_PAGE

    try:
        message = urllib.parse.unquote_plus(query)

        # Try different patterns GitHub uses to show result counts
        patterns = [
            r"We\'ve found ([\d,]+) code results",
            r"([\d,]+) code results",
            r'data-total-count="([\d,]+)"',
            r'"total_count":(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, content, re.I)
            if match:
                text = match.group(1).replace(",", "")
                count = int(text)
                logger.info(f"[search] extracted {count} results from page for query: {message}")
                return count

        # If no count found, use conservative estimate
        logger.warning(f"[search] could not extract count from page for query: {message}")
        return WEB_RESULTS_PER_PAGE

    except Exception as e:
        logger.error(f"[search] failed to extract count from page: {e}")
        return WEB_RESULTS_PER_PAGE


def search_code(
    query: str,
    session: str,
    page: int,
    with_api: bool,
    peer_page: int,
    callback: Optional[Callable[[List[str], str], None]] = None,
    search_type: str = "code",
) -> Tuple[List[str], str]:
    """
    Search GitHub with unified interface (historically code-only; now multi-type).
    Returns: (results_list, content)
    """
    keyword = urllib.parse.quote_plus(trim(query))
    if not keyword:
        return [], ""

    search_type = normalize_search_type(search_type)

    if with_api:
        results, _, content = search_api_with_count(
            query=keyword,
            token=session,
            page=page,
            peer_page=peer_page,
            search_type=search_type,
        )
        return results, content

    content = search_github_web(query=keyword, session=session, page=page, search_type=search_type)
    if isblank(content):
        return [], ""

    try:
        regex = r'href="(/[^\s"]+/blob/(?:[^"]+)?)#L\d+"'
        groups = re.findall(regex, content, flags=re.I)
        uris = list(set(groups)) if groups else []
        links = set()

        for uri in uris:
            links.add(f"https://github.com{uri}")

        results = list(links)

        # Call extract callback if provided
        if callback and isinstance(callback, Callable) and results:
            try:
                callback(results, content)
            except Exception as e:
                logger.error(f"[search] callback failed: {e}")

        return results, content
    except Exception:
        return [], ""


@handle_exceptions(default_result=[], log_level="error")
def collect(
    key_pattern: str,
    url: str = "",
    retries: int = 3,
    address_pattern: str = "",
    endpoint_pattern: str = "",
    model_pattern: str = "",
    text: Optional[str] = None,
) -> List[Service]:
    """Extract API keys and related information from URLs or text content

    Args:
        key_pattern: Regex pattern to match API keys
        url: URL to fetch content from (if text not provided)
        retries: Number of retry attempts for HTTP requests
        address_pattern: Regex pattern to match service addresses
        endpoint_pattern: Regex pattern to match endpoints
        model_pattern: Regex pattern to match model names
        text: Text content to search (if provided, url is ignored)

    Returns:
        List[Service]: List of Service objects with extracted information
    """
    if (not isinstance(url, str) and not isinstance(text, str)) or not isinstance(key_pattern, str):
        return []

    if text:
        content = text
    else:
        content = http_get(url=url, retries=retries, interval=COLLECT_RETRY_INTERVAL)

    if not content:
        return []

    # extract keys from content
    key_pattern = trim(key_pattern)
    keys = extract(text=content, regex=key_pattern)
    if not keys:
        return []

    # extract api addresses from content
    address_pattern = trim(address_pattern)
    addresses = extract(text=content, regex=address_pattern)
    if address_pattern and not addresses:
        return []
    if not addresses:
        addresses.append("")

    # extract api endpoints from content
    endpoint_pattern = trim(endpoint_pattern)
    endpoints = extract(text=content, regex=endpoint_pattern)
    if endpoint_pattern and not endpoints:
        return []
    if not endpoints:
        endpoints.append("")

    # extract models from content
    model_pattern = trim(model_pattern)
    models = extract(text=content, regex=model_pattern)
    if model_pattern and not models:
        return []
    if not models:
        models.append("")

    candidates = list()

    # combine keys, addresses and endpoints
    for key, address, endpoint, model in itertools.product(keys, addresses, endpoints, models):
        candidates.append(Service(address=address, endpoint=endpoint, key=key, model=model))

    return candidates


@handle_exceptions(default_result=[], log_level="error")
def extract(text: str, regex: str) -> List[str]:
    """Extract strings from text using regex pattern."""
    content, pattern = trim(text), trim(regex)
    if not content or not pattern:
        return []

    items: set[str] = set()
    groups = re.findall(pattern, content)
    for x in groups:
        words: List[str] = []
        if isinstance(x, str):
            words.append(x)
        elif isinstance(x, (tuple, list)):
            words.extend(list(x))
        else:
            logger.error(f"Unknown type: {type(x)}, value: {x}. Please optimize your regex")
            continue

        for word in words:
            key = trim(word)
            if key:
                items.add(key)

    return list(items)
