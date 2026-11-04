import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.common.errors import ServiceUnavailableError
from app.db import connection as connection_module
from app.db.connection import DatabaseClient, TransactionConnection
from app.models.entity_event import EventTypeEnum
from app.models.event import EntitySource, Event
from app.persistent import event_repository as repository_module
from app.persistent.event_repository import EventRepository

EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
OTHER_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e2")
FIRST_SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
LAST_SEEN_AT = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
AGGREGATION_UPDATED_AT = datetime(2026, 9, 8, 3, 0, 5, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 9, 8, 3, 5, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 8, 1, 5, tzinfo=UTC)


def _id(n: int) -> UUID:
    return UUID(int=n)


def _active_event(event_id: UUID = EVENT_ID, entity_event_ids: list[UUID] | None = None) -> Event:
    return Event(
        event_id=event_id,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=FIRST_SEEN_AT,
        last_seen_at=LAST_SEEN_AT,
        aggregation_updated_at=AGGREGATION_UPDATED_AT,
        entity_event_ids=entity_event_ids or [_id(1), _id(2)],
    )


def _row(event_id: UUID = EVENT_ID) -> dict:
    """An events row as Postgres returns it, JSONB columns as text."""
    return {
        "event_id": event_id,
        "exchange": "HKEX",
        "stock_code": "00700",
        "event_type_primary": "EARNINGS",
        "event_type_secondary": json.dumps(["DIVIDEND", "MA"]),
        "source_list": json.dumps(
            [
                {
                    "source_name": "HKEX",
                    "url": "https://example.com/1",
                    "published_at": "2026-09-08T01:00:00+00:00",
                },
                {
                    "source_name": "MINGPAO",
                    "url": "https://example.com/2",
                    "published_at": "2026-09-08T03:00:00+00:00",
                },
            ]
        ),
        "first_seen_at": FIRST_SEEN_AT,
        "last_seen_at": LAST_SEEN_AT,
        "brief_summary": None,
        "aggregation_updated_at": AGGREGATION_UPDATED_AT,
        "published_aggregation_updated_at": None,
        "updated_at": WRITTEN_AT,
        "created_at": CREATED_AT,
    }


def _persisted_event(event_id: UUID = EVENT_ID) -> Event:
    """The Event _row() reads back as."""
    return Event(
        event_id=event_id,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=FIRST_SEEN_AT,
        last_seen_at=LAST_SEEN_AT,
        event_type_secondary=[EventTypeEnum.DIVIDEND, EventTypeEnum.MA],
        source_list=[
            EntitySource("HKEX", "https://example.com/1", FIRST_SEEN_AT),
            EntitySource("MINGPAO", "https://example.com/2", LAST_SEEN_AT),
        ],
        aggregation_updated_at=AGGREGATION_UPDATED_AT,
        updated_at=WRITTEN_AT,
        created_at=CREATED_AT,
    )


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(repository_module, "_event_repository", None)


@pytest.fixture
def tx():
    return AsyncMock(spec=TransactionConnection)


@pytest.fixture
def db(tx):
    client = AsyncMock(spec=DatabaseClient)

    @contextlib.asynccontextmanager
    async def transaction():
        yield tx

    client.transaction = MagicMock(side_effect=transaction)
    return client


def _upsert_args(event: Event) -> tuple:
    """The 10 values one Event's UPSERT binds, in order, for an Event without
    event_type_secondary or source_list."""
    return (
        event.event_id,
        "HKEX",
        "00700",
        EventTypeEnum.EARNINGS,
        FIRST_SEEN_AT,
        LAST_SEEN_AT,
        AGGREGATION_UPDATED_AT,
        None,
        None,
        event.entity_event_ids,
    )


async def persist_events_success(db, tx):
    """Each Event's UPSERT and memberships are written in one transaction; only the Events
    actually written are returned, fully populated."""
    written, unchanged = _active_event(), _active_event(OTHER_EVENT_ID, [_id(3)])
    tx.execute_returning.side_effect = [_row(), None]

    result = await EventRepository(db).persist_events([written, unchanged])

    assert result == [_persisted_event()]
    db.transaction.assert_called_once_with()
    assert [call.args[1:] for call in tx.execute_returning.await_args_list] == [
        _upsert_args(written),
        _upsert_args(unchanged),
    ]
    assert [call.args[0] for call in tx.execute_returning.await_args_list] == [
        repository_module._UPSERT_EVENT_SQL
    ] * 2
    mappings = [call.args[1:] for call in tx.execute.await_args_list]
    assert [mapping[:2] for mapping in mappings] == [
        (EVENT_ID, _id(1)),
        (EVENT_ID, _id(2)),
        (OTHER_EVENT_ID, _id(3)),
    ]
    # One flush time for every membership it writes.
    assert len({mapping[2] for mapping in mappings}) == 1
    assert [call.args[0] for call in tx.execute.await_args_list] == [
        repository_module._INSERT_EVENT_ENTITY_MAP_SQL
    ] * 3


async def persist_events_given_secondary_and_sources(db, tx):
    """An Event that already has event_type_secondary and source_list is written with them."""
    event = _active_event()
    event.event_type_secondary = [EventTypeEnum.DIVIDEND]
    event.source_list = [EntitySource("HKEX", "https://example.com/1", FIRST_SEEN_AT)]
    tx.execute_returning.return_value = _row()

    result = await EventRepository(db).persist_events([event])

    assert result == [_persisted_event()]
    args = tx.execute_returning.await_args.args
    assert args[0] == repository_module._UPSERT_EVENT_SQL
    assert args[1:8] == _upsert_args(event)[:7]
    assert json.loads(args[8]) == ["DIVIDEND"]
    assert json.loads(args[9]) == [
        {
            "source_name": "HKEX",
            "url": "https://example.com/1",
            "published_at": "2026-09-08T01:00:00+00:00",
        }
    ]
    assert args[10] == [_id(1), _id(2)]
    assert [call.args[:3] for call in tx.execute.await_args_list] == [
        (repository_module._INSERT_EVENT_ENTITY_MAP_SQL, EVENT_ID, _id(1)),
        (repository_module._INSERT_EVENT_ENTITY_MAP_SQL, EVENT_ID, _id(2)),
    ]


async def persist_events_no_events(db, tx):
    assert await EventRepository(db).persist_events([]) == []
    tx.execute_returning.assert_not_awaited()
    tx.execute.assert_not_awaited()


async def fetch_event_success(db):
    db.fetch_one.return_value = _row()

    assert await EventRepository(db).fetch_event(EVENT_ID) == _persisted_event()
    db.fetch_one.assert_awaited_once_with(repository_module._SELECT_EVENT_SQL, EVENT_ID)


async def fetch_event_not_found(db):
    db.fetch_one.return_value = None

    assert await EventRepository(db).fetch_event(EVENT_ID) is None


async def fetch_unpublished_events_success(db):
    db.fetch_all.return_value = [_row(), _row(OTHER_EVENT_ID)]

    result = await EventRepository(db).fetch_unpublished_events([EVENT_ID, OTHER_EVENT_ID])

    assert result == [_persisted_event(), _persisted_event(OTHER_EVENT_ID)]
    db.fetch_all.assert_awaited_once_with(
        repository_module._SELECT_UNPUBLISHED_EVENTS_SQL, [EVENT_ID, OTHER_EVENT_ID]
    )


async def fetch_unpublished_events_none(db):
    db.fetch_all.return_value = []

    assert await EventRepository(db).fetch_unpublished_events([EVENT_ID]) == []


async def mark_published_success(db):
    await EventRepository(db).mark_published(EVENT_ID, AGGREGATION_UPDATED_AT)

    db.execute.assert_awaited_once_with(
        repository_module._MARK_PUBLISHED_SQL, EVENT_ID, AGGREGATION_UPDATED_AT
    )


async def fetch_mapped_event_id_success(db):
    db.fetch_one.return_value = {"event_id": EVENT_ID}

    assert await EventRepository(db).fetch_mapped_event_id(_id(1)) == EVENT_ID
    db.fetch_one.assert_awaited_once_with(repository_module._SELECT_MAPPED_EVENT_ID_SQL, _id(1))


async def fetch_mapped_event_id_none(db):
    db.fetch_one.return_value = None

    assert await EventRepository(db).fetch_mapped_event_id(_id(1)) is None


def get_event_repository_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        repository_module.get_event_repository()


def create_event_repository_success(monkeypatch, db):
    monkeypatch.setattr(connection_module, "_db_client", db)

    repository = repository_module.create_event_repository()

    assert repository_module.get_event_repository() is repository
