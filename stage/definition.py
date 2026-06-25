#!/usr/bin/env python3

"""
Built-in stage definitions for the pipeline system.
Registers all standard pipeline stages with their dependencies.
"""

import math
import time
from typing import List, Optional, Tuple

from constant.search import (
    API_LIMIT,
    API_MAX_PAGES,
    API_RESULTS_PER_PAGE,
    WEB_LIMIT,
    WEB_MAX_PAGES,
    WEB_RESULTS_PER_PAGE,
)
from constant.system import SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB
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
from tools.logger import get_logger
from tools.patterns import extract_github_query_pattern
from tools.state import GithubCredentialLimited
from tools.utils import get_service_name, handle_exceptions

from .base import BasePipelineStage, OutputHandler, StageOutput, StageResources
from .factory import TaskFactory
from .registry import register_stage

logger = get_logger("stage")


def _wait_for_rate_limit(resources: StageResources, service_type: str, label: str) -> None:
    """Block until the shared rate limiter grants a token."""
    while not resources.limiter.acquire(service_type):
        wait_time = resources.limiter.wait_time(service_type)
        if wait_time <= 0:
            wait_time = 0.1
        logger.debug(f"Rate limit hit for {label}, waiting {wait_time:.2f}s")
        time.sleep(wait_time)


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

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        search_task = task if isinstance(task, SearchTask) else SearchTask()
        return (
            f"{PipelineStage.SEARCH.value}:{task.provider}:{search_task.query}:{search_task.page}:{search_task.regex}"
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
                patterns = Patterns(
                    key_pattern=task.regex,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )
                for link in results:
                    acquisition_task = TaskFactory.create_acquisition_task(task.provider, link, patterns)
                    output.add_task(acquisition_task, PipelineStage.GATHER.value)

                # Add links to be saved
                output.add_links(task.provider, results)

            # Handle first page results for pagination/refinement
            if task.page == 1 and total > 0:
                self._handle_first_page_results(task, total, output)

            logger.info(
                f"[{self.name}] search completed for {task.provider}: {len(results) if results else 0} links, {len(keys)} keys"
            )

            return output

        except Exception as e:
            logger.error(f"[{self.name}] error, provider: {task.provider}, task: {task}, message: {e}")
            return None

    def _execute_first_page_search(self, task: SearchTask) -> Tuple[List[str], str, int]:
        """Execute first page search and get total count in single request"""
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

    def _execute_page_search(self, task: SearchTask) -> Tuple[List[str], str]:
        """Execute subsequent page search in single request"""
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
        """Handle first page results - decide pagination or refinement"""
        limit = API_LIMIT if task.use_api else WEB_LIMIT
        per_page = API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE

        # If needs refine query
        if total > limit:
            # Regenerate the query with less data
            partitions = int(math.ceil(total / limit))
            queries = RefineEngine.get_instance().generate_queries(query=task.query, partitions=partitions)

            # Add new query tasks to output
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
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )

                output.add_task(refined_task, PipelineStage.SEARCH.value)

            logger.info(
                f"[{self.name}] generated {len(queries)} refined tasks for provider: {task.provider}, query: {task.query}"
            )

        # If needs pagination and not refining
        elif total > per_page:
            page_tasks = self._generate_page_tasks(task, total, per_page)
            for page_task in page_tasks:
                output.add_task(page_task, PipelineStage.SEARCH.value)
            logger.info(
                f"[{self.name}] generated {len(page_tasks)} page tasks for provider: {task.provider}, query: {task.query}"
            )

    def _generate_page_tasks(self, task: SearchTask, total: int, per_page: int) -> List[SearchTask]:
        """Generate pagination tasks"""
        # Limit max pages
        max_pages = min(
            math.ceil(total / per_page),
            API_MAX_PAGES if task.use_api else WEB_MAX_PAGES,
        )

        page_tasks: List[SearchTask] = []
        for page in range(2, max_pages + 1):  # Start from page 2
            page_task = SearchTask(
                provider=task.provider,
                query=task.query,
                regex=task.regex,
                page=page,
                use_api=task.use_api,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
            )
            page_tasks.append(page_task)

        return page_tasks

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
            # Execute acquisition using global collect function
            services = client.collect(
                key_pattern=task.key_pattern,
                url=task.url,
                retries=task.retries,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
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
            logger.error(f"[{self.name}] error for provider: {task.provider}, task: {task}, message: {e}")
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

            # Report rate limit success
            self.resources.limiter.report_result(service_type, True)

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
                    ErrorReason.RATE_LIMITED,
                    ErrorReason.NO_MODEL,
                    ErrorReason.NO_ACCESS,
                ]:
                    output.add_result(task.provider, ResultType.WAIT_CHECK.value, [task.service])

                else:
                    output.add_result(task.provider, ResultType.INVALID.value, [task.service])

            return output

        except Exception as e:
            # Report rate limit failure
            self.resources.limiter.report_result(get_service_name(task.provider), False)
            logger.error(f"[{self.name}] error for provider: {task.provider}, task: {task}, message: {e}")

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
            logger.error(f"[{self.name}] inspect models error, provider: {task.provider}, task: {task}, message: {e}")
            return None
