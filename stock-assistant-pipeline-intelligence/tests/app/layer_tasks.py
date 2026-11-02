import asyncio

import pytest

from app import layer_tasks as layer_tasks_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    """Runs before every test here (autouse): starts each one with no Tasks registered, so
    get_layer_tasks_not_registered can't see what register_layer_tasks_success registers."""
    monkeypatch.setattr(layer_tasks_module, "_layer_tasks", None)


def get_layer_tasks_not_registered():
    with pytest.raises(ServiceUnavailableError) as raised:
        layer_tasks_module.get_layer_tasks()

    assert raised.value.error_code == "COMMON-5001"
    assert raised.value.category is ErrorCategory.COMMON


async def register_layer_tasks_success():
    """The registered dict itself is returned, so a Task replaced in it later is visible."""
    first = asyncio.create_task(asyncio.sleep(0))
    restarted = asyncio.create_task(asyncio.sleep(0))
    tasks = {"entity_analysis": first}

    layer_tasks_module.register_layer_tasks(tasks)
    tasks["entity_analysis"] = restarted

    assert layer_tasks_module.get_layer_tasks() == {"entity_analysis": restarted}
    await asyncio.gather(first, restarted)
