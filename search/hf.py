#!/usr/bin/env python3

"""
HuggingFace Hub search backend.

Discovers fetchable raw file URLs from public HF datasets so the pipeline's
gather→regex→validate stages can harvest leaked API keys from dataset files
(.env dumps, configs, notebooks). Unauthenticated; self-throttled.

Verified API facts (2026-08):
- ``GET /api/datasets?search=<kw>&sort=downloads&direction=-1&limit=<n>``
  returns one JSON array (≤1000 entries); ``page=``/``offset=`` are ignored.
- ``GET /api/datasets/{id}/tree/main?recursive=true`` returns up to 1000
  ``{type, oid, size, path}`` entries; further pages arrive via the ``Link``
  header (only the first page is read here; ``http_get`` hides headers).
- ``GET /datasets/{id}/resolve/main/{path}`` serves the raw file.
- Rate limit policy header advertises 500 requests per 300s per IP.
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from tools.logger import get_logger
from tools.utils import isblank

from .client import http_get

logger = get_logger("search")

_DATASETS_URL = "https://huggingface.co/api/datasets"
_TREE_URL = "https://huggingface.co/api/datasets/{dataset}/tree/main?recursive=true"
_RESOLVE_URL = "https://huggingface.co/datasets/{dataset}/resolve/main/{path}"

# Budgets: HF allows ~500 req / 300s per IP, so stay well under it.
_MIN_REQUEST_INTERVAL = 0.7  # seconds between HF requests (~85 req/min)
_MAX_DATASETS_PER_QUERY = 16  # datasets scanned per search keyword
_MAX_FILES_PER_DATASET = 50  # hard cap; some datasets hold 10k+ files
_MAX_FILE_SIZE = 2 * 1024 * 1024  # bytes; small files are likelier key dumps
_MAX_PATH_SEGMENTS = 8
_CACHE_TTL = 600.0  # reuse one discovery across the page tasks of a query

# Binary / model-weight / archive blobs never carry plaintext keys.
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp4",
        ".mp3",
        ".wav",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".bin",
        ".pt",
        ".safetensors",
        ".whl",
        ".so",
        ".dll",
        ".exe",
        ".parquet",
        ".arrow",
        ".onnx",
        ".h5",
        ".npy",
        ".npz",
        ".pickle",
        ".pkl",
    }
)

# Text-ish extensions worth regex scanning for keys. Everything else with a
# dotted extension is skipped: the gather HTTP layer decodes bodies as
# UTF-8/gzip text, so binary-ish files (.state, .ram, .DS_Store, ...) would
# fail to decode and produce nothing but noise.
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".config",
        ".css",
        ".csv",
        ".env",
        ".htm",
        ".html",
        ".ini",
        ".ipynb",
        ".js",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".mjs",
        ".php",
        ".properties",
        ".py",
        ".pyi",
        ".rb",
        ".rst",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

# Extension-less files are only scanned when their name betrays key material.
_KEY_HINT_NAMES = (
    "apikey",
    "auth",
    "config",
    "cookie",
    "credential",
    "dump",
    "env",
    "key",
    "leak",
    "password",
    "secret",
    "setting",
    "token",
)

_throttle_lock = threading.Lock()
_last_request_at = 0.0
_cache_lock = threading.Lock()
_discovery_cache: Dict[str, Tuple[float, List[str]]] = {}


def _throttle() -> None:
    """Block until the minimum interval since the previous HF request passed."""
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + _MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _get_json(url: str) -> Optional[Any]:
    """GET a HF API URL through the shared transport, parse JSON or None."""
    _throttle()
    try:
        content = http_get(url=url, retries=2, interval=1.0, timeout=15)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"[hf] request failed for {url}: {e}")
        return None

    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        logger.debug(f"[hf] invalid JSON from {url}")
        return None


def search_datasets(query: str) -> List[str]:
    """Return public dataset ids matching a keyword, most-downloaded first."""
    params = f"search={quote(query)}&sort=downloads&direction=-1&limit={_MAX_DATASETS_PER_QUERY}"
    data = _get_json(f"{_DATASETS_URL}?{params}")
    if not isinstance(data, list):
        return []

    dataset_ids: List[str] = []
    for item in data[:_MAX_DATASETS_PER_QUERY]:
        if isinstance(item, dict):
            dataset_id = str(item.get("id") or "")
            if dataset_id:
                dataset_ids.append(dataset_id)
    return dataset_ids


def _is_candidate_file(entry: Dict[str, Any]) -> bool:
    """Keep only small, shallow, text-like tree entries worth regex scanning."""
    if entry.get("type") != "file":
        return False

    path = str(entry.get("path") or "")
    if not path:
        return False
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) > _MAX_PATH_SEGMENTS:
        return False

    size = entry.get("size")
    if isinstance(size, (int, float)) and size > _MAX_FILE_SIZE:
        return False

    lowered = path.lower()
    if any(lowered.endswith(suffix) for suffix in _BINARY_SUFFIXES):
        return False

    name = path.rsplit("/", 1)[-1].lower()
    stem, sep, ext = name.rpartition(".")
    suffix = f".{ext}" if sep and stem else ""
    if suffix in _TEXT_SUFFIXES:
        return True
    if suffix:
        return False
    # Dotfiles (`.env`, `.credentials`) or extension-less names (`keys`,
    # `tokens`): keep only key-material names, prefix-matched.
    bare = name.lstrip(".")
    return bool(bare) and any(bare.startswith(hint) for hint in _KEY_HINT_NAMES)


def _file_size(entry: Dict[str, Any]) -> int:
    size = entry.get("size")
    return size if isinstance(size, int) else 0


def collect_dataset_files(dataset_id: str) -> List[str]:
    """List raw resolve-URLs of candidate files in one dataset's first tree page."""
    data = _get_json(_TREE_URL.format(dataset=quote(dataset_id, safe="/")))
    if not isinstance(data, list):
        return []

    entries = [entry for entry in data if isinstance(entry, dict) and _is_candidate_file(entry)]
    # Small files first: plaintext key dumps are tiny, curated datasets are huge
    entries.sort(key=_file_size)

    links: List[str] = []
    for entry in entries[:_MAX_FILES_PER_DATASET]:
        path = str(entry.get("path") or "")
        links.append(_RESOLVE_URL.format(dataset=dataset_id, path=quote(path, safe="/")))
    return links


def discover_links(query: str) -> List[str]:
    """Collect candidate file links across the top datasets for a keyword."""
    links: List[str] = []
    seen: set[str] = set()
    for dataset_id in search_datasets(query):
        for link in collect_dataset_files(dataset_id):
            if link not in seen:
                seen.add(link)
                links.append(link)
    return links


def _cached_discovery(query: str) -> List[str]:
    """Reuse one discovery run across the page tasks of a query (TTL cache)."""
    now = time.time()
    with _cache_lock:
        cached = _discovery_cache.get(query)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    links = discover_links(query)
    with _cache_lock:
        _discovery_cache[query] = (now, links)
    return links


def search_hf(query: str, page: int = 1, peer_page: int = 20) -> Tuple[List[str], str, int]:
    """
    Search HF Hub datasets for candidate key-bearing files.

    Returns (urls, content, total) in the search backend tuple shape, with
    content always empty: keys are regexed from raw file downloads by the
    GATHER stage, not from the search response.

    Args:
        query: keyword for the HF dataset search
        page: 1-based page number over the candidate link list
        peer_page: links per page (must match the stage's per-page budget)

    Returns:
        Tuple containing:
        - List[str]: raw resolve URLs for this page
        - str: always empty
        - int: total candidate links discovered for the query
    """
    query = (query or "").strip()
    if isblank(query) or page < 1:
        return [], "", 0

    peer_page = max(1, peer_page)
    links = _cached_discovery(query)
    total = len(links)
    start = (page - 1) * peer_page
    return links[start : start + peer_page], "", total
