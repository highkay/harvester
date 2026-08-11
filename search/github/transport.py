#!/usr/bin/env python3

"""
GitHub API edge IP pool and DoH helpers, inspired by ohmygh/gx.

Connects to api.github.com via known edge IPs with correct SNI so that
system DNS pollution does not block authenticated code search.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("search")

API_HOST = "api.github.com"
DEFAULT_HOSTS_URL = "https://hosts.ohmygh.com/v1/hosts"
DEFAULT_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://doh.pub/dns-query",
)

# Seed edges from a recent hosts feed / common GitHub API Anycast ranges
BUILTIN_SEEDS: Tuple[str, ...] = (
    "20.205.243.168",
    "20.27.177.116",
    "20.200.245.245",
    "20.207.73.85",
    "140.82.112.5",
    "140.82.114.6",
    "140.82.116.6",
    "4.237.22.34",
)


@dataclass
class EdgeEndpoint:
    """A verified or candidate API edge IP."""

    ip: str
    rtt_ms: float = 0.0
    failures: int = 0
    cooldown_until: float = 0.0
    last_ok: float = 0.0


@dataclass
class EdgePoolConfigRuntime:
    """Runtime view of edge pool settings (decoupled from config schemas)."""

    enabled: bool = True
    source: str = "auto"
    hosts_url: str = DEFAULT_HOSTS_URL
    gx_bin: str = "gx"
    refresh_interval: int = 3600
    max_edges: int = 32
    verify: bool = True
    prefer_over_proxy: bool = False
    doh_enabled: bool = True
    doh_endpoints: Sequence[str] = field(default_factory=lambda: list(DEFAULT_DOH_ENDPOINTS))
    cache_path: str = ""


class EdgePool:
    """Thread-safe rotating pool of api.github.com edge IPs."""

    def __init__(self, config: EdgePoolConfigRuntime):
        self.config = config
        self._lock = threading.RLock()
        self._edges: List[EdgeEndpoint] = []
        self._index = 0
        self._last_refresh = 0.0
        self._refreshing = False
        self._bg_thread: Optional[threading.Thread] = None

        if config.cache_path:
            self._load_cache()

        if not self._edges:
            self._edges = [EdgeEndpoint(ip=ip) for ip in BUILTIN_SEEDS[: config.max_edges]]

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._edges)

    def ensure_fresh(self, force: bool = False, blocking: bool = False) -> None:
        """Refresh the pool when stale or empty of usable edges.

        By default kicks a background refresh so request paths stay non-blocking.
        """
        with self._lock:
            now = time.time()
            needs = force or (now - self._last_refresh >= self.config.refresh_interval)
            usable = sum(1 for e in self._edges if e.cooldown_until <= now)
            if not needs and usable > 0:
                return
            if self._refreshing:
                return
            # Always keep a usable seed/cache pool for immediate dials
            if not self._edges:
                self._edges = [EdgeEndpoint(ip=ip) for ip in BUILTIN_SEEDS[: self.config.max_edges]]

        if blocking:
            self._refresh_guarded(force=force)
        else:
            self.refresh_async(force=force)

    def refresh_async(self, force: bool = False) -> None:
        """Start a daemon refresh if one is not already running."""
        with self._lock:
            if self._refreshing:
                return
            if self._bg_thread and self._bg_thread.is_alive():
                return
            self._refreshing = True
            self._bg_thread = threading.Thread(
                target=self._refresh_guarded,
                kwargs={"force": force, "from_async": True},
                name="github-edge-refresh",
                daemon=True,
            )
            thread = self._bg_thread
        thread.start()

    def _refresh_guarded(self, force: bool = False, from_async: bool = False) -> None:
        try:
            if not from_async:
                with self._lock:
                    if self._refreshing:
                        return
                    self._refreshing = True
            self.refresh(force=force)
        except Exception as e:
            logger.warning(f"[edge] refresh failed: {e}")
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self, force: bool = False) -> int:
        """Reload edge IPs from configured sources and optionally verify them."""
        candidates = self._collect_candidates()
        if not candidates:
            logger.warning("[edge] no edge candidates collected; keeping previous pool")
            with self._lock:
                self._last_refresh = time.time()
            return self.size

        verified: List[EdgeEndpoint] = []
        if self.config.verify:
            # Parallel probe (gx-style quality filter, faster startup)
            workers = min(16, max(4, len(candidates)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._verify_edge, ip): ip for ip in candidates}
                for fut in as_completed(futures):
                    ip = futures[fut]
                    try:
                        ok, rtt = fut.result()
                    except Exception:
                        ok, rtt = False, 0.0
                    if ok:
                        verified.append(EdgeEndpoint(ip=ip, rtt_ms=rtt, last_ok=time.time()))
                    if len(verified) >= self.config.max_edges:
                        # Cancel outstanding probes best-effort
                        for pending in futures:
                            pending.cancel()
                        break
        else:
            verified = [EdgeEndpoint(ip=ip) for ip in candidates[: self.config.max_edges]]

        if not verified:
            logger.warning("[edge] edge verification produced empty pool; falling back to candidates")
            verified = [EdgeEndpoint(ip=ip) for ip in candidates[: self.config.max_edges]]

        verified.sort(key=lambda e: e.rtt_ms if e.rtt_ms > 0 else 1e9)
        with self._lock:
            self._edges = verified
            self._index = 0
            self._last_refresh = time.time()
            self._save_cache()

        logger.info(f"[edge] refreshed API edge pool: {len(verified)} IPs")
        return len(verified)

    def next_ip(self) -> Optional[str]:
        """Pick the next usable edge IP (round-robin, skip cooling)."""
        self.ensure_fresh()
        with self._lock:
            if not self._edges:
                return None

            now = time.time()
            n = len(self._edges)
            for _ in range(n):
                edge = self._edges[self._index % n]
                self._index = (self._index + 1) % n
                if edge.cooldown_until <= now:
                    return edge.ip

            # All cooling — pick the soonest to recover
            edge = min(self._edges, key=lambda e: e.cooldown_until)
            return edge.ip

    def mark_success(self, ip: str, rtt_ms: float = 0.0) -> None:
        with self._lock:
            edge = self._find(ip)
            if not edge:
                return
            edge.failures = 0
            edge.cooldown_until = 0.0
            edge.last_ok = time.time()
            if rtt_ms > 0:
                edge.rtt_ms = rtt_ms

    def mark_failure(self, ip: str, cooldown: float = 30.0) -> None:
        with self._lock:
            edge = self._find(ip)
            if not edge:
                return
            edge.failures += 1
            # Exponential-ish cool-down capped at 10 minutes
            wait = min(600.0, cooldown * (2 ** min(edge.failures - 1, 4)))
            edge.cooldown_until = time.time() + wait
            logger.debug(f"[edge] cooling {ip} for {wait:.1f}s after failure #{edge.failures}")

    def snapshot(self) -> List[str]:
        with self._lock:
            return [e.ip for e in self._edges]

    def _find(self, ip: str) -> Optional[EdgeEndpoint]:
        for edge in self._edges:
            if edge.ip == ip:
                return edge
        return None

    def _collect_candidates(self) -> List[str]:
        source = (self.config.source or "auto").lower()
        ordered: List[str] = []
        seen: Set[str] = set()

        def add_many(ips: Iterable[str]) -> None:
            for ip in ips:
                ip = trim(ip)
                if not ip or ip in seen:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if addr.version != 4:
                    continue
                seen.add(ip)
                ordered.append(ip)

        strategies = []
        if source == "auto":
            strategies = ["http", "gx", "doh", "builtin"]
        elif source == "http":
            strategies = ["http", "builtin"]
        elif source == "gx":
            strategies = ["gx", "builtin"]
        elif source == "builtin":
            strategies = ["builtin"]
        elif source == "disabled":
            return []
        else:
            strategies = ["http", "builtin"]

        for strategy in strategies:
            if strategy == "http":
                add_many(self._fetch_hosts_feed())
            elif strategy == "gx":
                add_many(self._fetch_from_gx())
            elif strategy == "doh" and self.config.doh_enabled:
                add_many(self._resolve_doh(API_HOST))
            elif strategy == "builtin":
                add_many(BUILTIN_SEEDS)

            if ordered:
                # Prefer first successful strategy; still append builtin as tail
                if strategy != "builtin":
                    add_many(BUILTIN_SEEDS)
                break

        return ordered[: max(self.config.max_edges * 2, self.config.max_edges)]

    def _fetch_hosts_feed(self) -> List[str]:
        url = self.config.hosts_url or DEFAULT_HOSTS_URL
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "harvester-edge-pool/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            domains = payload.get("domains") or {}
            ips = domains.get(API_HOST) or domains.get("api.github.com") or []
            if isinstance(ips, list) and ips:
                logger.debug(f"[edge] hosts feed returned {len(ips)} IPs from {url}")
                return [str(ip) for ip in ips]
        except Exception as e:
            logger.warning(f"[edge] hosts feed failed ({url}): {e}")
        return []

    def _fetch_from_gx(self) -> List[str]:
        """Optional: use local gx CLI or its cache as an IP source only."""
        gx_bin = self.config.gx_bin or "gx"
        path = shutil.which(gx_bin) if os.path.sep not in gx_bin else (gx_bin if os.path.isfile(gx_bin) else None)
        if path:
            try:
                completed = subprocess.run(
                    [path, "hosts", "refresh", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if completed.returncode == 0:
                    # gx writes ~/.gx/api-ips.json; prefer that structured list
                    pass
            except Exception as e:
                logger.debug(f"[edge] gx hosts refresh failed: {e}")

        home = Path.home() / ".gx" / "api-ips.json"
        if home.is_file():
            try:
                data = json.loads(home.read_text(encoding="utf-8"))
                ips = data.get("ips") or []
                if isinstance(ips, list) and ips:
                    logger.debug(f"[edge] loaded {len(ips)} IPs from {home}")
                    return [str(ip) for ip in ips]
            except Exception as e:
                logger.debug(f"[edge] failed reading {home}: {e}")
        return []

    def _resolve_doh(self, hostname: str) -> List[str]:
        ips: List[str] = []
        for endpoint in self.config.doh_endpoints or DEFAULT_DOH_ENDPOINTS:
            try:
                url = f"{endpoint}?name={hostname}&type=A"
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/dns-json",
                        "User-Agent": "harvester-edge-pool/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for answer in data.get("Answer") or []:
                    if int(answer.get("type", 0)) == 1 and answer.get("data"):
                        ips.append(str(answer["data"]).strip())
                if ips:
                    logger.debug(f"[edge] DoH {endpoint} resolved {hostname} -> {ips}")
                    break
            except Exception as e:
                logger.debug(f"[edge] DoH via {endpoint} failed: {e}")
        return ips

    def _verify_edge(self, ip: str, timeout: float = 5.0) -> Tuple[bool, float]:
        """TLS + GET / must expose x-ratelimit-limit (true API edge, not web)."""
        start = time.time()
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((ip, 443), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=API_HOST) as ssock:
                    req = (
                        f"GET /rate_limit HTTP/1.1\r\n"
                        f"Host: {API_HOST}\r\n"
                        f"User-Agent: harvester-edge-pool/1.0\r\n"
                        f"Accept: application/vnd.github+json\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode("ascii")
                    ssock.sendall(req)
                    data = b""
                    while len(data) < 4096:
                        chunk = ssock.recv(1024)
                        if not chunk:
                            break
                        data += chunk
            text = data.decode("utf-8", errors="replace").lower()
            # True API edges advertise rate-limit headers (403 still counts)
            if "x-ratelimit-limit" not in text:
                return False, 0.0
            rtt = (time.time() - start) * 1000.0
            return True, rtt
        except Exception:
            return False, 0.0

    def _load_cache(self) -> None:
        path = Path(self.config.cache_path)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ips = data.get("ips") or []
            edges = [EdgeEndpoint(ip=str(ip)) for ip in ips if ip]
            if edges:
                with self._lock:
                    self._edges = edges[: self.config.max_edges]
                    self._last_refresh = float(data.get("fetched_at") or 0)
                logger.debug(f"[edge] loaded {len(edges)} cached edges from {path}")
        except Exception as e:
            logger.debug(f"[edge] cache load failed: {e}")

    def _save_cache(self) -> None:
        path = self.config.cache_path
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": int(time.time()),
                "ips": [e.ip for e in self._edges],
            }
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[edge] cache save failed: {e}")


_edge_pool: Optional[EdgePool] = None
_edge_pool_lock = threading.Lock()


def get_edge_pool() -> Optional[EdgePool]:
    return _edge_pool


def init_edge_pool(config: EdgePoolConfigRuntime, background: bool = True) -> Optional[EdgePool]:
    """Initialize the process-wide edge pool (or disable it).

    background=True (default): use cache/seeds immediately and verify hosts in a daemon thread
    so pipeline startup is not blocked for tens of seconds.
    """
    global _edge_pool
    with _edge_pool_lock:
        if not config.enabled or config.source == "disabled":
            _edge_pool = None
            logger.info("[edge] API edge pool disabled")
            return None
        _edge_pool = EdgePool(config)
        if background:
            logger.info(
                f"[edge] using { _edge_pool.size } seed/cache IPs; verifying hosts feed in background"
            )
            _edge_pool.refresh_async(force=True)
        else:
            try:
                _edge_pool.refresh()
            except Exception as e:
                logger.warning(f"[edge] initial refresh failed: {e}")
        return _edge_pool


def init_github_transport(
    *,
    workspace: str,
    proxy: str = "",
    edge_enabled: bool = True,
    edge_source: str = "auto",
    hosts_url: str = DEFAULT_HOSTS_URL,
    gx_bin: str = "gx",
    refresh_interval: int = 3600,
    max_edges: int = 32,
    verify: bool = True,
    prefer_over_proxy: bool = False,
    doh_enabled: bool = True,
    doh_endpoints: Optional[Sequence[str]] = None,
) -> Optional[EdgePool]:
    """High-level init used by the pipeline."""
    proxy = trim(proxy)
    enabled = edge_enabled
    if proxy and not prefer_over_proxy:
        logger.info("[edge] global proxy is set; edge pool disabled (set prefer_over_proxy=true to override)")
        enabled = False

    cache_path = os.path.join(workspace, "cache", "github_edges.json")
    cfg = EdgePoolConfigRuntime(
        enabled=enabled,
        source=edge_source,
        hosts_url=hosts_url or DEFAULT_HOSTS_URL,
        gx_bin=gx_bin or "gx",
        refresh_interval=refresh_interval,
        max_edges=max_edges,
        verify=verify,
        prefer_over_proxy=prefer_over_proxy,
        doh_enabled=doh_enabled,
        doh_endpoints=list(doh_endpoints or DEFAULT_DOH_ENDPOINTS),
        cache_path=cache_path,
    )
    return init_edge_pool(cfg)
