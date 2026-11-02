import json
from datetime import UTC, datetime
from unittest import mock
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.aggregation import event_aggregation_store as store_module
from app.aggregation.event_aggregation_store import EventAggregationStore
from app.common.errors import ServiceUnavailableError
from app.models.entity_event import EventTypeEnum
from app.models.event import Event
from app.redis import state_client as state_client_module
from app.redis.state_client import RedisStateClient

KEY = "agg:event:HKEX:00700:EARNINGS"
EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
ENTITY_EVENT_IDS = [
    UUID("00000000-0000-0000-0000-0000000000a1"),
    UUID("00000000-0000-0000-0000-0000000000a2"),
]
FIRST_SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
LAST_SEEN_AT = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 8, 3, 0, 5, tzinfo=UTC)
ACTIVE_EVENT = Event(
    event_id=EVENT_ID,
    exchange="HKEX",
    stock_code="00700",
    event_type_primary=EventTypeEnum.EARNINGS,
    first_seen_at=FIRST_SEEN_AT,
    last_seen_at=LAST_SEEN_AT,
    aggregation_updated_at=UPDATED_AT,
    entity_event_ids=ENTITY_EVENT_IDS,
)
FIELDS = {
    "event_id": str(EVENT_ID),
    "first_seen_at": "2026-09-08T01:00:00+00:00",
    "last_seen_at": "2026-09-08T03:00:00+00:00",
    "aggregation_updated_at": "2026-09-08T03:00:05+00:00",
    "entity_event_ids": json.dumps([str(i) for i in ENTITY_EVENT_IDS]),
}


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(store_module, "_event_aggregation_store", None)


@pytest.fixture
def state_client(monkeypatch):
    client = AsyncMock(spec=RedisStateClient)
    monkeypatch.setattr(state_client_module, "_redis_state_client", client)
    return client


async def get_active_event_success(state_client):
    state_client.hgetall.return_value = FIELDS

    result = await EventAggregationStore().get_active_event("HKEX", "00700", EventTypeEnum.EARNINGS)

    assert result == ACTIVE_EVENT
    state_client.hgetall.assert_awaited_once_with(KEY)


async def get_active_event_none(state_client):
    state_client.hgetall.return_value = None

    result = await EventAggregationStore().get_active_event("HKEX", "00700", EventTypeEnum.EARNINGS)

    assert result is None


async def upsert_active_event_success(state_client):
    """The hash and its TTL are written in one transaction."""
    pipe = MagicMock()
    state_client.multi_exec.side_effect = lambda build: build(pipe)

    await EventAggregationStore().upsert_active_event(ACTIVE_EVENT, 15000)

    state_client.multi_exec.assert_awaited_once()
    assert pipe.mock_calls == [
        mock.call.hset(KEY, mapping=FIELDS),
        mock.call.expire(KEY, 15000),
    ]


def get_event_aggregation_store_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        store_module.get_event_aggregation_store()


def create_event_aggregation_store_success(state_client):
    store = store_module.create_event_aggregation_store()

    assert store_module.get_event_aggregation_store() is store
