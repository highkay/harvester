#!/usr/bin/env python3

"""
Built-in stage definitions for the pipeline system.
Registers all standard pipeline stages with their dependencies.
"""

import math
import re
import threading
import time
import urllib.parse
from typing import Dict, List, NamedTuple, Optional, Tuple

from constant.search import (
    API_MAX_PAGES,
    API_RESULTS_PER_PAGE,
    WEB_MAX_PAGES,
    WEB_RESULTS_PER_PAGE,
)
from constant.system import (
    DEFAULT_HEADERS,
    DEFAULT_SHUTDOWN_TIMEOUT,
    SERVICE_TYPE_GITHUB_API,
    SERVICE_TYPE_GITHUB_WEB,
)
from core.enums import ErrorReason, PipelineStage, ResultType
from core.models import (
    AcquisitionTask,
    CheckTask,
    InspectTask,
    Patterns,
    ProviderTask,
    SearchTask,
    Service,
)
from core.types import IProvider
from search import client
from search.github.fetch_store import FetchStore, default_directory
from search.github.refine.engine import RefineEngine
from storage.persistence import (
    GATHER_304,
    GATHER_304_EMPTY,
    GATHER_304_REPLAYED,
    GATHER_EMPTY,
    GATHER_ERROR_404,
    GATHER_ERROR_OTHER,
    GATHER_OK,
    record_gather_outcome,
)
from tools.coordinator import get_user_agent
from tools.logger import get_logger
from tools.patterns import extract_github_query_pattern
from tools.retry import RetryCore
from tools.state import GithubCredentialLimited
from tools.utils import get_service_name, handle_exceptions

from .base import BasePipelineStage, OutputHandler, StageOutput, StageResources
from .factory import TaskFactory
from .registry import register_stage

logger = get_logger("stage")


def _is_retryable_for_requeue(error: Exception) -> bool:
    """Whether the stage retry machinery should requeue on this error.

    Reuses RetryCore.should_retry_error — the SAME predicate every RetryPolicy
    delegates to (ConnectionError, TimeoutError, and "rate limit" / "too many
    requests" messages; no local marker list). The search-side rate-limiter
    denial and unparseable-body failures from search/client.py are raised as
    ConnectionError precisely so this predicate catches them. The sentinel
    budget (attempt=0, max_retries=1) makes the call answer "is this error
    CLASS retryable"; real per-task attempt budgeting stays in
    BasePipelineStage._worker_loop / the configured RetryPolicy.
    """
    return RetryCore.should_retry_error(error, attempt=0, max_retries=1)


def _wait_for_rate_limit(resources: StageResources, service_type: str, label: str) -> None:
    """Block until the shared rate limiter grants a token."""
    while not resources.limiter.acquire(service_type):
        wait_time = resources.limiter.wait_time(service_type)
        if wait_time <= 0:
            wait_time = 0.1
        logger.debug(f"Rate limit hit for {label}, waiting {wait_time:.2f}s")
        time.sleep(wait_time)


# https://github.com/<owner>/<repo>/blob/<ref>/<path> — parsed STRUCTURALLY
# (host prefix + regex on the remainder), NOT via urlsplit: a '#' can be a
# genuine part of the path (directory "C#Code/", AutoSDK snapshot filenames
# like "_#G.Models….verified.cs"), and urlsplit truncates it as a fragment —
# a proven false-404 class on production (2026-09-30 evidence: links.txt holds
# 367 lines with '#', 107 unique). ref is matched as a single path segment
# (the shape GitHub search results carry); path is the rest.
_GITHUB_BLOB_PREFIX = "https://github.com/"
_GITHUB_BLOB_PATH_RE = re.compile(r"^([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")

# The only legitimate fragment on a blob URL is a trailing line anchor.
# The GitHub UI emits column-precision anchors too (#L4C1 / #L4C1-L4C14),
# where a 'C<column>' suffix may follow each line number.
_BLOB_LINE_ANCHOR_RE = re.compile(r"#L\d+(?:C\d+)?(?:-L\d+(?:C\d+)?)?$")

# Kept literal when re-quoting the decoded path: '/' separators plus '@' and
# ' ' (requests requotes a raw space to %20 on the wire). Everything else
# outside quote()'s unreserved set is percent-encoded — notably '#' -> %23 —
# while valid pre-existing escapes round-trip through unquote+quote unchanged
# (%23 -> '#' -> %23, never %2523).
_BLOB_PATH_SAFE = "/@ "


_REPO_LINK_CAP = 25

# A dork's pagination tail is exhausted once this many consecutive pages add
# no NEW fetchable link (measured 2026-10-20: deepseek kept ~900 page tasks
# running for hours after gather drained, every page returning only links that
# the repo cap or the seen-set already rejected). Skipping the rest saves the
# shared GitHub search budget without changing which links get fetched.
_DORK_EMPTY_PAGE_LIMIT = 3


def _github_repo_key(url: str) -> str:
    """``owner/repo`` (lowercased) for a github.com URL, else "".

    The cap unit for the per-run repo link cap (2026-10-19 audit, E/P7:
    benchmark/dataset repos flood every provider's corpus — one benchmark
    repo alone contributed 1,587 links across 4 providers). Non-github links
    (hf resolve URLs, …) return "" and stay uncapped.
    """
    marker = "github.com/"
    i = url.find(marker)
    if i < 0:
        return ""
    parts = url[i + len(marker):].split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return ""
    return f"{parts[0]}/{parts[1]}".lower()


class _FetchResult(NamedTuple):
    """One raw fetch + extraction exchange (see ``_fetch_and_extract``).

    ``replayed`` means a 304 re-emitted stored candidates; ``empty_304`` means
    a 304 found an explicit empty outcome and skipped the fetch entirely (the
    two are mutually exclusive; plain 304 fallbacks have both False).
    """

    services: List[Service]
    saw_304: bool
    replayed: bool = False
    empty_304: bool = False


def github_blob_to_raw(url: str) -> str:
    """Map a GitHub blob page URL to its raw.githubusercontent.com file URL.

    The HTML blob page embeds the file text JSON-escaped inside its
    ``rawLines`` payload (measured on production 2026-09-23: 268,952 bytes
    with 444 ``\\"`` occurrences for a file whose raw body is 3,159 bytes
    with plain quotes), so quote-anchored extraction patterns cannot match
    it and every fetch pays an ~85x bandwidth amplification.

    Only ``https://github.com/<owner>/<repo>/blob/<ref>/<path>`` (optionally
    with a trailing line anchor ``#L<num>`` / ``#L<num>-L<num>``, including the
    column-precision forms ``#L4C1`` / ``#L4C1-L4C14``, which is stripped) is
    rewritten. Any OTHER '#' in the URL is path content — it is
    percent-encoded for the wire (raw '#' and pre-encoded '%23' both end up
    as '%23', non-ASCII as UTF-8 escapes); the decoded path keeps '@' and ' '
    literal (requests requotes the space). Everything else — non-github URLs,
    huggingface ``resolve`` URLs, issue/commit pages, query-string URLs,
    malformed input — is returned byte-identical, and links.txt / dedup keep
    the ORIGINAL blob URL.

    Args:
        url: Candidate fetch URL (trailing whitespace/newline tolerant).

    Returns:
        str: The raw equivalent for blob pages, else the original input.
    """
    candidate = url.strip()
    if candidate[: len(_GITHUB_BLOB_PREFIX)].lower() != _GITHUB_BLOB_PREFIX:
        return url
    rest = candidate[len(_GITHUB_BLOB_PREFIX) :]
    if "?" in rest:  # query-string URLs stay byte-identical
        return url
    match = _GITHUB_BLOB_PATH_RE.match(rest)
    if not match:
        return url
    owner, repo, ref, path = match.groups()
    path = _BLOB_LINE_ANCHOR_RE.sub("", path)
    try:
        path = urllib.parse.quote(urllib.parse.unquote(path), safe=_BLOB_PATH_SAFE)
    except UnicodeEncodeError:  # undecodable surrogate junk — keep as harvested
        return url
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


@register_stage(
    name=PipelineStage.SEARCH.value,
    depends_on=[],
    produces_for=[PipelineStage.GATHER.value, PipelineStage.CHECK.value],
    description="Search GitHub for potential API keys",
)
class SearchStage(BasePipelineStage):
    """Pipeline stage for searching GitHub with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.SEARCH.value, resources, handler, **kwargs)
        # Per-run corpus bounding (global.max_links_per_run; 0 = unlimited).
        # The stage instance is per-Pipeline, i.e. per run, so this counter is
        # exactly "links this run discovered" — the same number that lands in
        # run_records.links_total.
        self._gather_links_queued = 0
        self._link_cap_logged = False
        # 2026-10-19 (measured by the six-agent audit): the same URL is
        # rediscovered by 2.5-4.2x across overlapping dorks/refinements WITHIN
        # a run, and the cap used to charge every rediscovery — nvidia/glm/
        # tavily hit the 120k cap after only ~40k unique links and silently
        # dropped 7-19% of their pagination, with which dorks lost the tail
        # decided by queue hash order. The seen-set makes the cap count
        # UNIQUE links (bounded by the cap itself).
        self._gather_links_seen: set[str] = set()
        # Per-repo fetch cap (same audit, E/P7): benchmark/dataset repos flood
        # every provider's corpus. owner/repo -> links queued this run.
        self._repo_link_counts: dict[str, int] = {}
        self._repo_cap_logged: set[str] = set()
        # Dork-tail convergence (2026-10-20): consecutive no-new-link pages per
        # provider|search_type|query, and the dorks whose tail is exhausted.
        self._dork_tail_streak: dict[str, int] = {}
        self._dork_exhausted: set[str] = set()
        # Dork-tail observability (one INFO summary per run in stop()). The
        # stage instance is per-run and shipped configs pin threads.search: 1,
        # so these counters have exactly one writer per run (same argument as
        # _gather_links_queued/_dork_tail_streak above).
        self._search_pages_executed = 0
        self._pages_skipped_by_tail = 0
        self._tail_summary_logged = False

    def _dork_key(self, task: SearchTask) -> str:
        """Dork identity for tail convergence (provider + source + query)."""
        search_type = getattr(task, "search_type", "code") or "code"
        return f"{task.provider}|{search_type}|{task.query}"

    def _dork_tail_limit(self) -> int:
        """Consecutive unproductive pages before a dork is exhausted.

        ``global.dork_tail_empty_page_limit`` (0 = guard disabled). Falls back
        to the module constant when the key is absent (older configs, mocked
        resources) or unusable (non-int, MagicMock) — the same defensive shape
        as :meth:`_link_cap`.
        """
        raw = getattr(
            getattr(self.resources.config, "global_config", None),
            "dork_tail_empty_page_limit",
            None,
        )
        if isinstance(raw, bool) or not isinstance(raw, int):
            return _DORK_EMPTY_PAGE_LIMIT
        return max(0, raw)

    def _tail_summary_line(self) -> str:
        """One-line dork-tail guard accounting for this run."""
        return (
            f"dork-tail guard: search_pages_executed={self._search_pages_executed} "
            f"pages_skipped_by_tail={self._pages_skipped_by_tail} "
            f"dorks_exhausted={len(self._dork_exhausted)}"
        )

    def tail_summary_text(self) -> Optional[str]:
        """Summary for run_records/error_message, or None when never fired."""
        if self._pages_skipped_by_tail <= 0 and not self._dork_exhausted:
            return None
        return self._tail_summary_line()

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop workers, then emit the per-run dork-tail summary exactly once.

        Only when the guard actually fired (a tail page was skipped or a dork
        was exhausted); a run with the guard dormant stays silent.
        """
        try:
            super().stop(timeout)
        finally:
            if not self._tail_summary_logged and self.tail_summary_text() is not None:
                self._tail_summary_logged = True
                logger.info(f"[{self.name}] {self._tail_summary_line()}")

    def _link_cap(self) -> int:
        """Per-run link cap from global.max_links_per_run (0 = unlimited).

        Only a real int counts: `int(MagicMock()) == 1`, so coercing an
        arbitrary attribute would silently cap every caller whose config is a
        stub (many stage tests) after the first link.
        """
        raw = getattr(
            getattr(self.resources.config, "global_config", None),
            "max_links_per_run",
            0,
        )
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 0
        return max(0, raw)

    def _link_cap_reached(self, provider: str) -> bool:
        """True once this run has emitted its cap of links (logs once)."""
        cap = self._link_cap()
        if not cap or self._gather_links_queued < cap:
            return False
        if not self._link_cap_logged:
            logger.warning(
                f"[{self.name}] per-run link cap reached ({cap} links queued for "
                f"gather) for {provider} — "
                f"dropping further search tasks (pagination/refinement); the "
                f"remaining dorks are left for the next run. Set "
                f"global.max_links_per_run=0 to disable."
            )
            self._link_cap_logged = True
        return True

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        search_task = task if isinstance(task, SearchTask) else SearchTask()
        search_type = getattr(search_task, "search_type", "code") or "code"
        return (
            f"{PipelineStage.SEARCH.value}:{task.provider}:{search_type}:"
            f"{search_task.query}:{search_task.page}:{search_task.regex}"
        )

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is a SearchTask."""
        return isinstance(task, SearchTask)

    def _pre_process(self, task: ProviderTask) -> bool:
        """Pre-process search task - validate query and provider."""

        # Check if provider is enabled
        if not self.resources.is_enabled(task.provider, "search"):
            logger.debug(f"[{self.name}] search disabled for provider: {task.provider}")
            return False

        # Validate query
        search_task = task if isinstance(task, SearchTask) else None
        if not search_task or not search_task.query:
            logger.warning(f"[{self.name}] empty query for provider: {task.provider}")
            return False

        return True

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute search task processing."""
        return self._search_worker(task)

    def _search_worker(self, task: SearchTask) -> Optional[StageOutput]:
        """Pure functional search worker"""
        if self._link_cap_reached(task.provider):
            return None
        if self._dork_key(task) in self._dork_exhausted:
            self._pages_skipped_by_tail += 1
            return None
        self._search_pages_executed += 1
        try:
            # Execute search based on page number
            if task.page == 1:
                results, content, total = self._execute_first_page_search(task)
            else:
                results, content = self._execute_page_search(task)
                total = 0

            # Create output object
            output = StageOutput(task=task)

            # Extract keys directly from search content
            keys = []
            if content and task.regex:
                keys = self._extract_keys_from_content(content, task)
                for key_service in keys:
                    check_task = TaskFactory.create_check_task(task.provider, key_service)
                    output.add_task(check_task, PipelineStage.CHECK.value)

                if keys:
                    logger.info(
                        f"[{self.name}] extracted {len(keys)} keys from search content, provider: {task.provider}"
                    )

            # Create acquisition tasks for links
            # queued_new is initialised OUTSIDE the `if results:` gate: a page
            # that returned zero links queues nothing, and that is exactly the
            # deep-tail case the convergence guard below must observe.
            queued_new = 0
            if results:
                search_type = getattr(task, "search_type", "code") or "code"
                patterns = Patterns(
                    key_pattern=task.regex,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )

                # Index order: filter known → enqueue gather → record all discoveries
                gather_links = list(results)
                link_index = client.get_link_index()
                if link_index is not None and link_index.enabled:
                    # The WRITE is gated on the same predicate as the
                    # read/dedup: with skip_known_links=false nothing ever
                    # reads the index, so add_many would only grow an
                    # evict-less sqlite file forever (links.db was 1.1GB on
                    # prod, write-only dead weight under every shipped config).
                    if client.should_skip_known_links():
                        gather_links = link_index.filter_new(results, task.provider)
                        skipped = len(results) - len(gather_links)
                        if skipped:
                            logger.info(
                                f"[{self.name}] skipped {skipped} known links for {task.provider} "
                                f"(index dedup)"
                            )
                        new_count = link_index.add_many(
                            results,
                            provider=task.provider,
                            search_type=search_type,
                            query=task.query,
                        )
                        if new_count < len(results):
                            logger.debug(
                                f"[{self.name}] link index: {new_count} new / {len(results)} total "
                                f"for {task.provider} ({search_type})"
                            )

                fresh = [link for link in gather_links if link not in self._gather_links_seen]
                self._gather_links_seen.update(fresh)
                fresh_set = set(fresh)
                self._gather_links_queued += len(fresh)

                for link in gather_links:
                    # Per-repo fetch cap: over-cap links stay in links.txt
                    # (discovery record, via add_links below) but are neither
                    # queued nor fetched this run. Logged once per repo per run.
                    repo = _github_repo_key(link)
                    if repo:
                        repo_n = self._repo_link_counts.get(repo, 0)
                        if repo_n >= _REPO_LINK_CAP:
                            if repo not in self._repo_cap_logged:
                                self._repo_cap_logged.add(repo)
                                logger.info(
                                    f"[{self.name}] repo link cap ({_REPO_LINK_CAP}/run) reached "
                                    f"for {repo} — its further links stay recorded but unfetched"
                                )
                            continue
                        self._repo_link_counts[repo] = repo_n + 1
                    if link in fresh_set:
                        queued_new += 1
                    acquisition_task = TaskFactory.create_acquisition_task(task.provider, link, patterns)
                    output.add_task(acquisition_task, PipelineStage.GATHER.value)

                # Persist all discovered links (including ones skipped for gather)
                output.add_links(task.provider, results)

            # Dork-tail convergence: once a dork's pages stop contributing
            # NEW fetchable links, its remaining pages can only burn search
            # quota. The tasks are already queued, so the saving comes from
            # the early return above (no HTTP request per skipped page).
            #
            # PRODUCTIVE means the page contributed at least one link that will
            # be fetched THIS run (queued_new, measured AFTER the 25/repo cap —
            # a page whose every link lands in an already-capped repo fetches
            # nothing) OR the page yielded keys extracted from the search
            # content (a dork can pay for itself through in-result extraction).
            # Anything else — including a page that returned zero links — is
            # unproductive. Pages > 1 only: page 1 is never counted. A
            # configured limit of 0 disables the guard entirely (never
            # exhaust) — the knob is read once per page from the run config.
            tail_limit = self._dork_tail_limit()
            if task.page > 1 and tail_limit > 0:
                dork = self._dork_key(task)
                if queued_new or keys:
                    self._dork_tail_streak.pop(dork, None)
                else:
                    streak = self._dork_tail_streak.get(dork, 0) + 1
                    self._dork_tail_streak[dork] = streak
                    if streak >= tail_limit:
                        self._dork_exhausted.add(dork)
                        logger.info(
                            f"[{self.name}] dork tail exhausted for {task.provider} "
                            f"({task.query[:80]}): {streak} consecutive pages added no new "
                            f"fetchable links — dropping its remaining pages"
                        )

            # Handle first page results for pagination/refinement.
            # Reaching this gate with total == 0 now means the fetch SUCCEEDED
            # and the dork genuinely has no results — the only case allowed to
            # skip page tasks. Fetch failures can no longer masquerade as zero
            # results: the search-side rate-limiter denial and unparseable
            # bodies raise retryable ConnectionError inside search/client.py,
            # and transport errors (429/5xx/timeout) already did; they are
            # re-raised by the handler below so _worker_loop requeues the dork
            # instead of silently dropping its page tail + refinement branch.
            # Also check the cap HERE, not only at worker entry: a first-page
            # task that itself pushed the run over the cap would otherwise
            # queue its pages 2..N + ~27 refinement tasks, all of which are
            # then dropped at entry — thousands of doomed tasks churning the
            # queue before the bound takes effect.
            if task.page == 1 and total > 0 and not self._link_cap_reached(task.provider):
                self._handle_first_page_results(task, total, output)

            total_note = f", total: {total}" if task.page == 1 else ""
            logger.info(
                f"[{self.name}] search completed for {task.provider}: "
                f"{len(results) if results else 0} links, {len(keys)} keys{total_note}"
            )

            return output

        except Exception as e:
            # Identity is provider:class:sha256-digest — never the task repr or
            # the raw dedup id: CHECK/INSPECT ids embed the raw candidate key
            # and the global RedactionFilter misses prefix-less formats
            # (SerpApi 64-hex). See BasePipelineStage._safe_task_identity.
            logger.log(
                self._error_log_level(e),
                f"[{self.name}] error, task: {self._safe_task_identity(task)}, "
                f"message: {e}",
            )
            # Retryable transport failures (ConnectionError — which now also
            # carries the rate-limiter denial and unparseable-body cases —
            # TimeoutError, "rate limit" markers) must escape to
            # BasePipelineStage._worker_loop: its requeue/retry_policy
            # machinery only fires when processing RAISES. Everything else
            # stays a logged drop (return None), as before.
            if _is_retryable_for_requeue(e):
                raise
            return None

    def _handle_processing_error(self, task: ProviderTask, error: Exception) -> Optional[StageOutput]:
        """Let retryable errors escape process_task so _worker_loop requeues.

        BasePipelineStage.process_task catches everything _execute_task raises
        and routes it here; the default implementation returns None, which is
        why the loop's retry_policy / max_retries_requeued machinery was
        unreachable for real stages — the worker-level re-raise alone would
        still be swallowed one frame up. Same RetryCore predicate decides;
        non-retryable errors stay swallowed (logged drop). CheckStage /
        InspectStage deliberately do NOT override this — see the note on
        CheckStage._check_worker (verdict routing is not idempotent).
        """
        if _is_retryable_for_requeue(error):
            raise error
        return None

    def _execute_first_page_search(self, task: SearchTask) -> Tuple[List[str], str, int]:
        """Execute first page search and get total count in single request"""
        search_type = getattr(task, "search_type", "code") or "code"
        if search_type == "hf":
            # HuggingFace Hub search is unauthenticated: no credential gate
            results, total, content = self._execute_hf_search(task)
            return results, content, total

        while True:
            # Get auth via injected provider
            if task.use_api:
                auth_token = self.resources.auth.get_token()
            else:
                auth_token = self.resources.auth.get_session()

            if not auth_token:
                return [], "", 0

            try:
                # Execute search with count - now returns content as well
                results, total, content = client.search_with_count(
                    query=self._preprocess_query(task.query, task.use_api),
                    session=auth_token,
                    page=task.page,
                    with_api=task.use_api,
                    peer_page=API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE,
                    search_type=getattr(task, "search_type", "code") or "code",
                )
                return results, content, total
            except GithubCredentialLimited as e:
                logger.warning(
                    f"[{self.name}] GitHub credential cooling during first-page search, "
                    f"retry with another credential, wait: {e.wait:.1f}s"
                )

    def _preprocess_query(self, query: str, use_api: bool) -> str:
        """Github Rest API search syntax don't support regex, so we need remove it if exists"""
        if use_api and extract_github_query_pattern(query):
            keyword = RefineEngine.get_instance().clean_regex(query=query)
            if keyword:
                query = keyword

        return query

    def _execute_hf_search(self, task: SearchTask) -> Tuple[List[str], int, str]:
        """Search HuggingFace Hub: no GitHub credentials, no dork preprocessing"""
        peer_page = API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE
        return client.search_with_count(
            query=task.query,
            session="",
            page=task.page,
            with_api=task.use_api,
            peer_page=peer_page,
            search_type="hf",
        )

    def _execute_page_search(self, task: SearchTask) -> Tuple[List[str], str]:
        """Execute subsequent page search in single request"""
        search_type = getattr(task, "search_type", "code") or "code"
        if search_type == "hf":
            results, _, content = self._execute_hf_search(task)
            return results, content

        while True:
            # Get auth via injected provider
            if task.use_api:
                auth_token = self.resources.auth.get_token()
            else:
                auth_token = self.resources.auth.get_session()

            if not auth_token:
                return [], ""

            try:
                # Execute search - now returns content as well
                results, content = client.search_code(
                    query=self._preprocess_query(task.query, task.use_api),
                    session=auth_token,
                    page=task.page,
                    with_api=task.use_api,
                    peer_page=API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE,
                    search_type=getattr(task, "search_type", "code") or "code",
                )
                return results, content
            except GithubCredentialLimited as e:
                logger.warning(
                    f"[{self.name}] GitHub credential cooling during page search, "
                    f"retry with another credential, wait: {e.wait:.1f}s"
                )

    def _apply_rate_limit(self, use_api: bool) -> bool:
        """Apply rate limiting for GitHub requests"""
        service_type = SERVICE_TYPE_GITHUB_API if use_api else SERVICE_TYPE_GITHUB_WEB
        label = f'Github {"Rest API" if use_api else "Web"}'
        _wait_for_rate_limit(self.resources, service_type, label)
        return True

    def _handle_first_page_results(self, task: SearchTask, total: int, output: StageOutput) -> None:
        """Handle first page results - refine and/or paginate.

        Refinement never replaces pagination. The refine engine only partitions a
        query along ``language:`` (popular programming languages) and ``size:``, so
        a dork qualified by ``extension:``/``filename:``/``path:`` — exactly where
        API keys live — has no satisfiable language partition at all. Measured
        2026-09-22: ``"ollama" "api_key" extension:env`` returns 3144 results, its
        27 refined ``language:`` queries return 0 (only ``language:Shell`` = 5), so
        the dork was capped at its first page (100 links) and the tail of every
        oversized dork was silently dropped. Pages 2..10 stay walkable, so always
        emit them.
        """
        per_page = API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE
        limit = self._max_pages(task) * per_page
        search_type = getattr(task, "search_type", "code") or "code"

        # Regex refine is only meaningful for code search
        if total > limit and search_type == "code":
            # Regenerate the query with less data
            partitions = int(math.ceil(total / limit))
            queries = RefineEngine.get_instance().generate_queries(query=task.query, partitions=partitions)

            generated = 0
            for query in queries:
                if not query:
                    logger.warning(
                        f"[{self.name}] skip refined query due to empty for query: {task.query}, provider: {task.provider}"
                    )
                    continue
                elif query == task.query:
                    logger.warning(
                        f"[{self.name}] discard refined query same as original: {query}, provider: {task.provider}"
                    )
                    continue

                refined_task = SearchTask(
                    provider=task.provider,
                    query=query,
                    regex=task.regex,
                    page=1,
                    use_api=task.use_api,
                    max_pages=task.max_pages,
                    search_type=search_type,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )

                output.add_task(refined_task, PipelineStage.SEARCH.value)
                generated += 1

            logger.info(
                f"[{self.name}] generated {generated} refined tasks for provider: {task.provider}, query: {task.query}"
            )

        # Pagination, independently of refinement (see docstring)
        if total > per_page:
            page_tasks = self._generate_page_tasks(task, total, per_page)
            for page_task in page_tasks:
                output.add_task(page_task, PipelineStage.SEARCH.value)
            logger.info(
                f"[{self.name}] generated {len(page_tasks)} page tasks for provider: {task.provider}, "
                f"query: {task.query}, type: {search_type}"
            )

    def _generate_page_tasks(self, task: SearchTask, total: int, per_page: int) -> List[SearchTask]:
        """Generate pagination tasks"""
        # Limit max pages
        max_pages = min(math.ceil(total / per_page), self._max_pages(task))
        search_type = getattr(task, "search_type", "code") or "code"

        page_tasks: List[SearchTask] = []
        for page in range(2, max_pages + 1):  # Start from page 2
            page_task = SearchTask(
                provider=task.provider,
                query=task.query,
                regex=task.regex,
                page=page,
                use_api=task.use_api,
                max_pages=task.max_pages,
                search_type=search_type,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
            )
            page_tasks.append(page_task)

        return page_tasks

    def _max_pages(self, task: SearchTask) -> int:
        """Return task-level max pages, falling back to transport defaults."""
        if task.max_pages is not None:
            if task.use_api:
                return min(task.max_pages, API_MAX_PAGES)
            return task.max_pages
        return API_MAX_PAGES if task.use_api else WEB_MAX_PAGES

    @handle_exceptions(default_result=[], log_level="error")
    def _extract_keys_from_content(self, content: str, task: SearchTask) -> List[Service]:
        """Extract keys directly from search content"""
        services = client.collect(
            key_pattern=task.regex,
            address_pattern=task.address_pattern,
            endpoint_pattern=task.endpoint_pattern,
            model_pattern=task.model_pattern,
            text=content,
        )

        return services


@register_stage(
    name=PipelineStage.GATHER.value,
    depends_on=[PipelineStage.SEARCH.value],
    produces_for=[PipelineStage.CHECK.value],
    description="Gather keys from discovered URLs",
)
class AcquisitionStage(BasePipelineStage):
    """Pipeline stage for acquiring keys from URLs with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.GATHER.value, resources, handler, **kwargs)
        # ETag / extraction-outcome store for raw fetches (2026-10-22, GAP-1).
        # PER RUN and owned by this stage — never a process-global singleton:
        # configure_github_transport close()s the singletons IT owns at every
        # scan start (search/client.py:668-670), which would silently close a
        # store shared with a concurrent run. Created lazily on the first fetch
        # (a stage that never gathers must not create ./data/cache/...),
        # retired in stop().
        self._fetch_store: Optional[FetchStore] = None
        self._fetch_store_lock = threading.Lock()
        self._fetch_store_tried = False
        # Per-run 304 accounting, logged once in stop() (guarded by stats_lock).
        self._gather_304 = 0
        self._gather_304_replayed = 0
        self._gather_304_empty = 0

    def _fetch_store_instance(self) -> Optional[FetchStore]:
        """This run's FetchStore, or None when conditional fetching is off.

        Returns None (fail-open) when the workspace is unusable or the store
        could not be opened: conditional fetching is an optimization, so a
        broken ./data must degrade to the plain unconditional fetch instead of
        failing the run. FetchStore itself logs the one warning.
        """
        with self._fetch_store_lock:
            if self._fetch_store is not None:
                return self._fetch_store
            if self._fetch_store_tried:
                return None
            self._fetch_store_tried = True
            global_config = getattr(self.resources.config, "global_config", None)
            workspace = getattr(global_config, "workspace", None)
            # A non-str workspace (a MagicMock in unit tests, a malformed
            # config) must never reach os.makedirs.
            if not isinstance(workspace, str) or not workspace.strip():
                logger.debug(f"[{self.name}] no workspace for the fetch store; raw fetches stay unconditional")
                return None
            store = FetchStore(default_directory(workspace))
            if not store.enabled:
                return None
            self._fetch_store = store
            logger.info(f"[{self.name}] conditional raw fetch enabled, store at {store.directory}")
            return store

    def _retire_fetch_store(self) -> None:
        """Prune, log the run's 304 summary once, then close the store."""
        with self._fetch_store_lock:
            store, self._fetch_store = self._fetch_store, None
        if store is None:
            return
        stats: dict = {}
        try:
            store.prune(retention_days=FetchStore.RETENTION_DAYS, max_rows=FetchStore.MAX_ROWS)
            stats = store.stats()
        finally:
            store.close()
        with self.stats_lock:
            revalidated, replayed, empty = (
                self._gather_304,
                self._gather_304_replayed,
                self._gather_304_empty,
            )
        logger.info(
            f"[{self.name}] gather_304={revalidated} gather_304_replayed={replayed} "
            f"gather_304_empty={empty} fetch_store={stats}"
        )

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop the workers first, then retire this run's fetch store.

        Called on pipeline teardown (manager/pipeline.py::_on_stop). The close
        runs even when the stage never started, and BasePipelineStage.stop()
        early-returns in that case — hence the unconditional call after it.
        """
        super().stop(timeout)
        self._retire_fetch_store()

    def _count_304(self, replayed: bool = False, empty: bool = False) -> None:
        with self.stats_lock:
            self._gather_304 += 1
            if replayed:
                self._gather_304_replayed += 1
            if empty:
                self._gather_304_empty += 1

    def _plain_collect(self, task: AcquisitionTask, raw_url: str, headers: Dict[str, str]) -> List[Service]:
        """The unconditional fetch path: download the body and extract from it."""
        return client.collect(
            key_pattern=task.key_pattern,
            url=raw_url,
            retries=task.retries,
            address_pattern=task.address_pattern,
            endpoint_pattern=task.endpoint_pattern,
            model_pattern=task.model_pattern,
            headers=headers,
        )

    def _fetch_and_extract(
        self, task: AcquisitionTask, raw_url: str, headers: Dict[str, str]
    ) -> _FetchResult:
        """Fetch one raw file, revalidating with ETag when an outcome is stored.

        Four outcomes:

        * **200** — extract from the fresh body, then store the etag and the
          outcome for this (provider, url, pattern_fp). The outcome row is
          written even when the extraction produced NOTHING (an explicit empty
          marker): zero-candidate links are ~99% of the corpus, so they are
          the ones that must not fall back to a full fetch on the next 304.
        * **304 + candidates** — REPLAY: the candidates go to CHECK with no
          body downloaded and the extractor never called, so
          ``materials_total`` / ``valid_keys_found`` and the pool's periodic
          re-validation of known keys both survive the ~80-92% cross-run link
          overlap (measured: nvidia 92.2 / glm 84.1 / tavily 83.8).
        * **304 + explicit empty marker** — SKIP: the body is unchanged and
          last time it yielded no candidate, so there is nothing to replay and
          nothing to extract. No download, no extractor call, nothing emitted
          to CHECK; counted ``gather_304_empty``.
        * **304 + no row** — first time this provider/pattern pair sees the
          URL, so fall back to a normal full fetch.

        A changed pattern fingerprint is a different outcome row, so neither
        stale candidates nor an empty skip can be reused under new patterns;
        provider is part of the row key, so nothing replays across providers;
        an empty etag never sends ``If-None-Match`` (no validator, no skip); a
        404 is never stored (only successful fetches write state).
        """
        store = self._fetch_store_instance()
        if store is None:
            return _FetchResult(self._plain_collect(task, raw_url, headers), False)

        pattern_fp = FetchStore.pattern_fingerprint(
            task.key_pattern, task.address_pattern, task.endpoint_pattern, task.model_pattern
        )
        state = store.get_state(raw_url)
        etag = state.etag if state is not None else ""

        try:
            result = client.http_get_raw(
                url=raw_url,
                headers=headers,
                etag=etag,
                retries=task.retries,
            )
        except (ConnectionError, TimeoutError):
            # Retryable transport failures keep escaping to _worker_loop's
            # requeue path — the same contract client.collect() has.
            raise
        except Exception as e:
            # Non-retryable failures (404 FileNotFoundError, 401/403
            # NetworkError, decode errors) degrade to "nothing collected"
            # exactly like collect() does, so the link is still written to
            # links.txt and counted GATHER_EMPTY instead of vanishing from the
            # corpus. Nothing is stored: a transient CDN 404 must not stick.
            logger.error(f"[{self.name}] raw fetch failed for {task.provider}: {e}")
            return _FetchResult([], False)

        if result.unchanged:
            stored = store.get_outcome(task.provider, raw_url, pattern_fp)
            if stored is not None:
                # ``[]`` is an explicit empty outcome, not "no row": the
                # extraction completed last time and produced nothing, so the
                # unchanged body has nothing to replay and nothing to extract.
                # Skip the fetch entirely — the R1 bulk case (kimi-coding 0.07%
                # / glm 0.70-0.76% extraction rates).
                self._count_304(replayed=bool(stored), empty=not stored)
                if stored:
                    logger.debug(f"[{self.name}] 304 replay of {len(stored)} candidate(s) for {task.provider}")
                    # fetched_at is deliberately NOT refreshed here, so a hot
                    # URL pays one full re-extraction every RETENTION_DAYS (14)
                    # when prune() retires its rows. That bounds the store AND
                    # self-heals a row whose stored candidates drifted from
                    # what the current extractor would produce.
                    return _FetchResult(list(stored), True, replayed=True)
                logger.debug(
                    f"[{self.name}] 304 skip: stored empty outcome for {task.provider} "
                    f"(no download, no extraction)"
                )
                return _FetchResult([], True, empty_304=True)
            # No outcome row for this (provider, pattern_fp): revalidate proved
            # the body is unchanged but we never extracted from it under these
            # patterns, so do a normal full fetch. The etag is refreshed from
            # the 304 (it may omit the header — the validator is unchanged by
            # definition) so the state row's fetched_at does not age out.
            self._count_304()
            services = self._plain_collect(task, raw_url, headers)
            if result.etag:
                store.put_state(raw_url, result.etag, result.status)
                store.put_outcome(task.provider, raw_url, pattern_fp, services)
            return _FetchResult(services, True)

        if result.status == 200 and result.content:
            services = client.collect(
                key_pattern=task.key_pattern,
                text=result.content,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
            )
            if result.etag:
                store.put_state(raw_url, result.etag, result.status)
                # Every completed extraction is stored, including an empty one:
                # the explicit marker is what lets the next 304 skip the body
                # fetch instead of paying two requests for the same bytes.
                store.put_outcome(task.provider, raw_url, pattern_fp, services)
            return _FetchResult(services, False)

        # Any other status (204, an empty 200 body): nothing to extract and
        # nothing worth caching.
        return _FetchResult([], False)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        acquisition_task = task if isinstance(task, AcquisitionTask) else AcquisitionTask()
        return f"{PipelineStage.GATHER.value}:{task.provider}:{acquisition_task.url}"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is an AcquisitionTask."""
        return isinstance(task, AcquisitionTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute acquisition task processing."""
        return self._acquisition_worker(task)

    def _acquisition_worker(self, task: AcquisitionTask) -> Optional[StageOutput]:
        """Pure functional acquisition worker implementation"""
        # Boundary guard (2026-10-19): a task.url carrying literal control
        # characters (\n \r \t) is malformed regardless of origin — the four
        # observed "<blob-url>\n<path-fragment>" cases (4 in 1,093,771 corpus
        # lines) get silently concatenated by urlsplit (it strips \n) into a
        # duplicated-path URL that 404s, polluting the 404 channel used to
        # spot genuinely dead links. Legitimate space-carrying paths (a
        # directory named "Chopping _tracker ") are NOT affected — a space is
        # not a control character and requotes fine.
        if any(c in task.url for c in ("\n", "\r", "\t")):
            logger.warning(
                f"[{self.name}] skipping malformed link with control characters "
                f"for {task.provider}: {task.url[:120]!r}"
            )
            return None
        try:
            # Fetch the RAW file body for GitHub blob pages (see
            # github_blob_to_raw): the HTML blob page is ~85x larger and its
            # JSON-escaped quotes defeat the quote-anchored key patterns.
            # links.txt and the dedup id below keep the ORIGINAL blob URL.
            # The fetch itself is ETag-conditional across runs: an unchanged
            # body replays the candidates stored for this
            # (provider, url, pattern fingerprint) instead of re-downloading.
            fetch = self._fetch_and_extract(
                task,
                github_blob_to_raw(task.url),
                {**DEFAULT_HEADERS, "User-Agent": get_user_agent()},
            )
            services = fetch.services

            # Gather-outcome counters (zero-yield tripwire observability):
            # bumped on the provider's live ResultManager, reached through
            # the same IProvider instance the result layer holds. No-op-safe
            # before the manager materializes; never raises. GATHER_304 is an
            # ORTHOGONAL sub-count (a 304 also bumps ok/empty for the work it
            # produced) so gather_ok + gather_empty keeps equalling the number
            # of links processed. gather_304_replayed / gather_304_empty split
            # the 304s into "candidates re-emitted" and "known-empty, fetch
            # skipped", the two cases that actually cost zero body bytes.
            provider = self.resources.providers.get(task.provider)
            record_gather_outcome(provider, GATHER_OK if services else GATHER_EMPTY)
            if fetch.saw_304:
                record_gather_outcome(provider, GATHER_304)
                if fetch.replayed:
                    record_gather_outcome(provider, GATHER_304_REPLAYED)
                elif fetch.empty_304:
                    record_gather_outcome(provider, GATHER_304_EMPTY)

            # Create output object
            output = StageOutput(task=task)

            # Create check tasks for found services
            if services:
                for service in services:
                    check_task = TaskFactory.create_check_task(task.provider, service)
                    output.add_task(check_task, PipelineStage.CHECK.value)

                # Add material keys to be saved
                output.add_result(task.provider, ResultType.MATERIAL.value, services)

            # Add the processed link to be saved
            output.add_links(task.provider, [task.url])

            return output

        except Exception as e:
            logger.error(f"[{self.name}] error, task: {self._safe_task_identity(task)}, message: {e}")
            # Count the failed fetch attempt (per ATTEMPT: a retryable error
            # requeued below bumps this again on each retry — the counter
            # measures transport health, not lost URLs). 404-class failures
            # are split out so the zero-yield tripwire can distinguish a
            # dead corpus from a broken transport.
            record_gather_outcome(
                self.resources.providers.get(task.provider),
                GATHER_ERROR_404 if isinstance(e, FileNotFoundError) else GATHER_ERROR_OTHER,
            )
            # Retryable transport failures must escape to _worker_loop's requeue
            # path: search/client.py collect() propagates ConnectionError
            # (HTTP 429/5xx after its own retry budget, TLS errors) and
            # TimeoutError. Before this, a gather fetch that died transiently
            # was lost for the run while its URL was still written to
            # links.txt — the corpus looked complete. Non-retryable errors
            # (404 FileNotFoundError, 401/403 NetworkError, ...) stay a logged
            # drop; collect() already degrades those to [] itself.
            if _is_retryable_for_requeue(e):
                raise
            return None

    def _handle_processing_error(self, task: ProviderTask, error: Exception) -> Optional[StageOutput]:
        """Let retryable errors escape process_task so _worker_loop requeues.

        Same contract as SearchStage._handle_processing_error: the default
        (return None) would swallow the worker's re-raise inside
        BasePipelineStage.process_task and the retry policy would never fire.
        """
        if _is_retryable_for_requeue(error):
            raise error
        return None


@register_stage(
    name=PipelineStage.CHECK.value,
    depends_on=[],
    produces_for=[PipelineStage.INSPECT.value],
    description="Validate API keys",
)
class CheckStage(BasePipelineStage):
    """Pipeline stage for validating API keys with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.CHECK.value, resources, handler, **kwargs)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        check_task = task if isinstance(task, CheckTask) else None
        if check_task and check_task.service:
            service = check_task.service
            return f"{PipelineStage.CHECK.value}:{task.provider}:{service.key}:{service.address}:{service.endpoint}"

        return f"{PipelineStage.CHECK.value}:{task.provider}:unknown"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is a CheckTask."""
        return isinstance(task, CheckTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute check task processing."""
        return self._check_worker(task)

    def _check_worker(self, task: CheckTask) -> Optional[StageOutput]:
        """Pure functional check worker implementation"""
        try:
            # Get provider instance
            provider = self.resources.providers.get(task.provider)
            if not provider or not isinstance(provider, IProvider):
                logger.error(f"[{self.name}] unknown provider: {task.provider}, type: {type(provider)}")
                return None

            # Apply rate limiting
            service_type = get_service_name(task.provider)
            _wait_for_rate_limit(self.resources, service_type, f"provider: {task.provider}")

            # Execute check
            result = provider.check(
                token=task.service.key,
                address=task.custom_url or task.service.address,
                endpoint=task.service.endpoint,
                model=task.service.model,
            )

            # Feed the limiter the REAL outcome. A retryable verdict
            # (RATE_LIMITED / TIMEOUT / NETWORK_ERROR / 5xx) is a transport
            # signal, not a key verdict, and must engage adjust_rate's backoff
            # path. Reporting True unconditionally (before 2026-09-23) made that
            # path unreachable: the bucket could only accelerate (x1.1 per 10
            # successes, capped at 2x base), which is exactly what holds a
            # provider at the 1-2 req/s that trips tavily's per-IP
            # bulk-validation block (and groq's/agnes' egress traps).
            self.resources.limiter.report_result(service_type, not result.reason.is_retryable())

            # Create output object
            output = StageOutput(task=task)

            # Handle result based on availability
            if result.available:
                # Create inspect task
                inspect_task = TaskFactory.create_inspect_task(task.provider, task.service)
                output.add_task(inspect_task, PipelineStage.INSPECT.value)

                # Add valid key to be saved
                output.add_result(task.provider, ResultType.VALID.value, [task.service])

            else:
                # Categorize based on error reason
                if result.reason == ErrorReason.NO_QUOTA:
                    output.add_result(task.provider, ResultType.NO_QUOTA.value, [task.service])

                elif result.reason in [
                    ErrorReason.NO_MODEL,
                    ErrorReason.NO_ACCESS,
                    ErrorReason.BAD_REQUEST,
                ] or result.reason.is_retryable():
                    # Retryable verdicts (rate limit / network / timeout / 5xx) are
                    # NOT key verdicts: measured 2026-09-22, ollama.com through the
                    # scan's socks exits answers TLS EOF/timeouts often enough that
                    # filing them as INVALID permanently burned live keys. The wait
                    # pool is recoverable (see the wait-pool recovery recipe).
                    #
                    # BAD_REQUEST is here on purpose too: providers emit it when
                    # the probe itself was rejected rather than the credential —
                    # provider/base.py maps HTTP 400 to BAD_REQUEST, qwen maps a
                    # non-Arrearage 400 to it, deepseek maps 400 to it, and
                    # opencode maps its 401-ModelError (model validated BEFORE
                    # auth) to it. A malformed probe / mismatched model id must
                    # not be a permanent discard. UNKNOWN stays invalid: the
                    # response was parsed but the verdict is genuinely unknowable.
                    output.add_result(task.provider, ResultType.WAIT_CHECK.value, [task.service])

                else:
                    output.add_result(task.provider, ResultType.INVALID.value, [task.service])

            return output

        except Exception as e:
            # Report rate limit failure
            self.resources.limiter.report_result(get_service_name(task.provider), False)
            logger.error(f"[{self.name}] error, task: {self._safe_task_identity(task)}, message: {e}")

            # CONSERVATIVE: retryable errors are deliberately NOT re-raised
            # here (unlike SearchStage/AcquisitionStage), so the stage retry
            # machinery never requeues a check. Reason: this stage's output
            # routes verdicts into result files and the routing is NOT
            # idempotent — storage's ResultBuffer.add does not dedupe, so a
            # partial handler failure followed by a requeue would double-write
            # the same key, and the requeued live probe can even return a
            # DIFFERENT verdict (the same key in both valid- and
            # invalid-keys.txt, confusing pool pushes). Recovery for
            # transient probe failures already exists downstream: providers
            # retry internally (search/client.py chat loop), retryable
            # verdicts route to wait-check-keys.txt (ErrorReason.is_retryable
            # branch above), and the wait-pool recovery recipe salvages them.
            return None


@register_stage(
    name=PipelineStage.INSPECT.value,
    depends_on=[],
    produces_for=[],
    description="Inspect API capabilities for validated keys",
)
class InspectStage(BasePipelineStage):
    """Pipeline stage for inspecting API capabilities with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.INSPECT.value, resources, handler, **kwargs)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        inspect_task = task if isinstance(task, InspectTask) else None
        if inspect_task and inspect_task.service:
            service = inspect_task.service
            return f"{PipelineStage.INSPECT.value}:{task.provider}:{service.key}:{service.address}"

        return f"{PipelineStage.INSPECT.value}:{task.provider}:unknown"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is an InspectTask."""
        return isinstance(task, InspectTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute inspect task processing."""
        return self._inspect_worker(task)

    def _inspect_worker(self, task: InspectTask) -> Optional[StageOutput]:
        """Pure functional inspect worker implementation"""
        try:
            # Get provider instance
            provider = self.resources.providers.get(task.provider)
            if not provider or not isinstance(provider, IProvider):
                logger.error(f"[{self.name}] unknown provider: {task.provider}, type: {type(provider)}")
                return None

            # Get model list
            models = provider.inspect(
                token=task.service.key, address=task.service.address, endpoint=task.service.endpoint
            )

            # Create output object
            output = StageOutput(task=task)

            # Add models to be saved
            if models:
                output.add_models(task.provider, task.service.key, models)

            return output

        except Exception as e:
            logger.error(f"[{self.name}] inspect models error, task: {self._safe_task_identity(task)}, message: {e}")
            # CONSERVATIVE: no retryable re-raise (see CheckStage._check_worker)
            # — inspect output is routed by the same non-deduping ResultBuffer,
            # and a lost model catalogue for one key is informational only
            # (the key verdict was already written by CheckStage).
            return None
