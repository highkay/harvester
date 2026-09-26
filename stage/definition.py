#!/usr/bin/env python3

"""
Built-in stage definitions for the pipeline system.
Registers all standard pipeline stages with their dependencies.
"""

import math
import re
import time
import urllib.parse
from typing import List, Optional, Tuple

from constant.search import (
    API_MAX_PAGES,
    API_RESULTS_PER_PAGE,
    WEB_MAX_PAGES,
    WEB_RESULTS_PER_PAGE,
)
from constant.system import DEFAULT_HEADERS, SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB
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
from search.github.refine.engine import RefineEngine
from storage.persistence import (
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


# https://github.com/<owner>/<repo>/blob/<ref>/<path> — ref is matched as a
# single path segment (the shape GitHub search results carry); path is the rest.
_GITHUB_BLOB_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")


def github_blob_to_raw(url: str) -> str:
    """Map a GitHub blob page URL to its raw.githubusercontent.com file URL.

    The HTML blob page embeds the file text JSON-escaped inside its
    ``rawLines`` payload (measured on production 2026-09-23: 268,952 bytes
    with 444 ``\\"`` occurrences for a file whose raw body is 3,159 bytes
    with plain quotes), so quote-anchored extraction patterns cannot match
    it and every fetch pays an ~85x bandwidth amplification.

    Only ``https://github.com/<owner>/<repo>/blob/<ref>/<path>`` (optionally
    with a ``#L…`` fragment, which is stripped) is rewritten; the path is
    percent-decoded. Everything else — non-github URLs, huggingface
    ``resolve`` URLs, issue/commit pages, query-string URLs, malformed
    input — is returned byte-identical.

    Args:
        url: Candidate fetch URL (trailing whitespace/newline tolerant).

    Returns:
        str: The raw equivalent for blob pages, else the original input.
    """
    candidate = url.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return url
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query:
        return url
    match = _GITHUB_BLOB_PATH_RE.match(parsed.path)
    if not match:
        return url
    owner, repo, ref, path = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{urllib.parse.unquote(path)}"


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
        self._links_emitted = 0
        self._link_cap_logged = False

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
        if not cap or self._links_emitted < cap:
            return False
        if not self._link_cap_logged:
            logger.warning(
                f"[{self.name}] per-run link cap reached ({cap}) for {provider} — "
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

                for link in gather_links:
                    acquisition_task = TaskFactory.create_acquisition_task(task.provider, link, patterns)
                    output.add_task(acquisition_task, PipelineStage.GATHER.value)

                # Persist all discovered links (including ones skipped for gather)
                output.add_links(task.provider, results)
                self._links_emitted += len(results)

            # Handle first page results for pagination/refinement.
            # Reaching this gate with total == 0 now means the fetch SUCCEEDED
            # and the dork genuinely has no results — the only case allowed to
            # skip page tasks. Fetch failures can no longer masquerade as zero
            # results: the search-side rate-limiter denial and unparseable
            # bodies raise retryable ConnectionError inside search/client.py,
            # and transport errors (429/5xx/timeout) already did; they are
            # re-raised by the handler below so _worker_loop requeues the dork
            # instead of silently dropping its page tail + refinement branch.
            if task.page == 1 and total > 0:
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
        try:
            # Fetch the RAW file body for GitHub blob pages (see
            # github_blob_to_raw): the HTML blob page is ~85x larger and its
            # JSON-escaped quotes defeat the quote-anchored key patterns.
            # links.txt and the dedup id below keep the ORIGINAL blob URL.
            services = client.collect(
                key_pattern=task.key_pattern,
                url=github_blob_to_raw(task.url),
                retries=task.retries,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
                headers={**DEFAULT_HEADERS, "User-Agent": get_user_agent()},
            )

            # Gather-outcome counters (zero-yield tripwire observability):
            # bumped on the provider's live ResultManager, reached through
            # the same IProvider instance the result layer holds. No-op-safe
            # before the manager materializes; never raises.
            record_gather_outcome(
                self.resources.providers.get(task.provider),
                GATHER_OK if services else GATHER_EMPTY,
            )

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
