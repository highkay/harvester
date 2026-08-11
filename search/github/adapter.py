#!/usr/bin/env python3

"""
requests adapter that dials api.github.com via edge IPs with correct SNI.

TCP connects to the edge IP; TLS SNI and Host remain api.github.com
(so certificate validation still works). Inspired by ohmygh/gx.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Optional, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util import connection as urllib3_connection

from tools.logger import get_logger

from .transport import API_HOST, EdgePool, get_edge_pool

logger = get_logger("search")

_tls = threading.local()
_patch_lock = threading.Lock()
_patched = False
_original_create_connection = urllib3_connection.create_connection


def _edge_create_connection(
    address: Tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: Any = None,
    socket_options: Any = None,
):
    """urllib3 create_connection wrapper that overrides the dial target only."""
    host, port = address
    override = getattr(_tls, "edge_ip", None)
    if override and host == API_HOST:
        host = override
    return _original_create_connection(
        (host, port),
        timeout=timeout,
        source_address=source_address,
        socket_options=socket_options,
    )


def _ensure_patch() -> None:
    global _patched
    if _patched:
        return
    with _patch_lock:
        if _patched:
            return
        urllib3_connection.create_connection = _edge_create_connection
        _patched = True
        logger.debug("[edge] installed urllib3 create_connection patch for edge routing")


class EdgeHTTPAdapter(HTTPAdapter):
    """Mount on a requests Session for https://api.github.com."""

    def __init__(self, edge_pool: Optional[EdgePool] = None, *args, **kwargs):
        self._edge_pool = edge_pool
        super().__init__(*args, **kwargs)
        _ensure_patch()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        pool = self._edge_pool if self._edge_pool is not None else get_edge_pool()
        edge_ip: Optional[str] = None
        previous = getattr(_tls, "edge_ip", None)

        if pool is not None:
            try:
                pool.ensure_fresh()
                edge_ip = pool.next_ip()
            except Exception as e:
                logger.debug(f"[edge] failed to pick edge IP: {e}")

        if edge_ip:
            _tls.edge_ip = edge_ip
            logger.debug(f"[edge] {request.method} {request.url} via {edge_ip}")
        else:
            _tls.edge_ip = None

        start = time.time()
        try:
            response = super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
            if pool and edge_ip:
                pool.mark_success(edge_ip, rtt_ms=(time.time() - start) * 1000.0)
            return response
        except Exception:
            if pool and edge_ip:
                pool.mark_failure(edge_ip)
            raise
        finally:
            _tls.edge_ip = previous


def mount_edge_adapter(session, edge_pool: Optional[EdgePool] = None) -> None:
    """Attach edge routing to a requests session for api.github.com only."""
    pool = edge_pool if edge_pool is not None else get_edge_pool()
    if pool is None:
        return
    adapter = EdgeHTTPAdapter(edge_pool=pool, pool_connections=16, pool_maxsize=16)
    # Mount both with and without trailing path so url matching is reliable
    session.mount(f"https://{API_HOST}/", adapter)
    session.mount(f"https://{API_HOST}", adapter)
    logger.info(f"[edge] mounted edge adapter for {API_HOST} ({pool.size} IPs)")
