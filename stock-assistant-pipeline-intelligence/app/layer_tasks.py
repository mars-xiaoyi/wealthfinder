import asyncio
from enum import StrEnum

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError


class Layer(StrEnum):
    ENTITY_ANALYSIS = "entity_analysis"
    AGGREGATION = "aggregation"
    SCORING = "scoring"
    CACHE = "cache"


_layer_tasks: dict[Layer, asyncio.Task[None]] | None = None


def register_layer_tasks(tasks: dict[Layer, asyncio.Task[None]]) -> None:
    """Register the layer Tasks, keyed by layer. Later changes to the dict, such as a
    restarted Task, are visible through get_layer_tasks()."""
    global _layer_tasks
    _layer_tasks = tasks


def get_layer_tasks() -> dict[Layer, asyncio.Task[None]]:
    """Return the registered layer Tasks, keyed by layer."""
    if _layer_tasks is None:
        raise ServiceUnavailableError(
            "layer tasks are not registered", category=ErrorCategory.COMMON
        )
    return _layer_tasks
