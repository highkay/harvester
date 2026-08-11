#!/usr/bin/env python3

"""
Task Factory for the asynchronous pipeline system.
Provides factory methods for creating tasks from configuration and serialized data.
"""

from typing import Any, Dict, Optional, Union

from core.models import (
    AcquisitionTask,
    CheckTask,
    InspectTask,
    Patterns,
    ProviderTask,
    SearchTask,
    Service,
)
from tools.logger import get_logger

logger = get_logger("stage")


class TaskFactory:
    """Factory for creating tasks from configuration and serialized data"""

    @staticmethod
    def create_search_task(
        provider: str,
        query: str,
        regex: str = "",
        page: int = 1,
        use_api: bool = False,
        max_pages: Optional[int] = None,
        search_type: str = "code",
        address_pattern: str = "",
        endpoint_pattern: str = "",
        model_pattern: str = "",
    ) -> SearchTask:
        """Create a search task"""
        return SearchTask(
            provider=provider,
            query=query,
            regex=regex,
            page=page,
            use_api=use_api,
            max_pages=max_pages,
            search_type=search_type or "code",
            address_pattern=address_pattern,
            endpoint_pattern=endpoint_pattern,
            model_pattern=model_pattern,
        )

    @staticmethod
    def create_acquisition_task(provider: str, url: str, patterns: Union[Dict[str, str], Patterns]) -> AcquisitionTask:
        """Create an acquisition task with extraction patterns"""
        if isinstance(patterns, Patterns):
            return AcquisitionTask(
                provider=provider,
                url=url,
                key_pattern=patterns.key_pattern,
                address_pattern=patterns.address_pattern,
                endpoint_pattern=patterns.endpoint_pattern,
                model_pattern=patterns.model_pattern,
            )
        else:
            return AcquisitionTask(
                provider=provider,
                url=url,
                key_pattern=patterns.get("key_pattern", ""),
                address_pattern=patterns.get("address_pattern", ""),
                endpoint_pattern=patterns.get("endpoint_pattern", ""),
                model_pattern=patterns.get("model_pattern", ""),
            )

    @staticmethod
    def create_check_task(provider: str, service: Service) -> CheckTask:
        """Create a check task for API key validation"""
        return CheckTask(provider=provider, service=service)

    @staticmethod
    def create_inspect_task(provider: str, service: Service) -> InspectTask:
        """Create an inspect task for inspecting API capabilities"""
        return InspectTask(provider=provider, service=service)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> ProviderTask:
        """Create task from serialized dictionary"""
        task_type = data.get("type")

        if task_type == "SearchTask":
            return SearchTask.from_dict(data)
        elif task_type == "AcquisitionTask":
            return AcquisitionTask.from_dict(data)
        elif task_type == "CheckTask":
            return CheckTask.from_dict(data)
        elif task_type == "InspectTask":
            return InspectTask.from_dict(data)
        else:
            raise ValueError(f"Unknown task type: {task_type}")
