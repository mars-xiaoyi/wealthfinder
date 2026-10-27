import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.common.errors import ServiceUnavailableError
from app.common.stock import Stock
from app.db import connection as connection_module
from app.db.connection import DatabaseClient, TransactionConnection
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.versioned_metadata import VersionedMetadata
from app.persistent import entity_event_repository as repository_module
from app.persistent.entity_event_repository import EntityEventRepository

CLEANED_ID = UUID("00000000-0000-0000-0000-00000000000a")
SOURCE_URL = "https://example.com/a"
PUBLISHED_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
HSBC = Stock("00005", "HKEX", "匯豐控股", "HSBC HOLDINGS")


def _entity_event(stock: Stock) -> EntityEvent:
    return EntityEvent(
        source_id=CLEANED_ID,
        source_url=SOURCE_URL,
        source_name="MINGPAO",
        published_at=PUBLISHED_AT,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code=stock.stock_code,
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=EventTypeEnum.DIVIDEND,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline="業績超預期",
                entity_summary="季度收入按年增長。",
            ),
        ),
        stock=stock,
    )


def _expected_args(stock: Stock, entity: EntityEvent) -> tuple:
    """The 15 values one insert binds, in column order."""
    return (
        entity.entity_event_id,
        stock.exchange,
        SOURCE_URL,
        "MINGPAO",
        PUBLISHED_AT,
        stock.stock_code,
        EventTypeEnum.EARNINGS,
        SentimentEnum.POSITIVE,
        0.8,
        "業績超預期",
        "季度收入按年增長。",
        {
            "version": "v1.0.0",
            "output": {
                "company_name_zh": stock.company_name_zh,
                "company_name_en": stock.company_name_en,
                "event_type_secondary": "DIVIDEND",
                "headline": "業績超預期",
                "entity_summary": "季度收入按年增長。",
            },
        },
        {"cleaned_id": str(CLEANED_ID)},
        entity.created_at,
        entity.created_at,
    )


def _bound_args(call) -> tuple:
    """One execute_returning call's bound values, with the two JSONB strings decoded."""
    args = list(call.args[1:])
    args[11] = json.loads(args[11])
    args[12] = json.loads(args[12])
    return tuple(args)


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(repository_module, "_entity_event_repository", None)


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


async def persist_entities_success(db, tx):
    tencent, hsbc = _entity_event(TENCENT), _entity_event(HSBC)
    tx.execute_returning.side_effect = lambda query, *args: {"entity_event_id": args[0]}

    persisted = await EntityEventRepository(db).persist_entities([tencent, hsbc])

    assert persisted == [tencent, hsbc]
    assert isinstance(tencent.entity_event_id, UUID)
    assert isinstance(hsbc.entity_event_id, UUID)
    assert tencent.entity_event_id != hsbc.entity_event_id
    assert tencent.created_at is not None
    assert hsbc.created_at is not None
    db.transaction.assert_called_once_with()
    calls = tx.execute_returning.await_args_list
    assert [_bound_args(call) for call in calls] == [
        _expected_args(TENCENT, tencent),
        _expected_args(HSBC, hsbc),
    ]
    assert calls[0].args[0] == repository_module._INSERT_ENTITY_EVENT_SQL


async def persist_entities_conflict(db, tx):
    """An entity whose (source_url, stock_code) row already exists is left out of the result."""
    tencent, hsbc = _entity_event(TENCENT), _entity_event(HSBC)
    tx.execute_returning.side_effect = [None, {"entity_event_id": "id"}]

    persisted = await EntityEventRepository(db).persist_entities([tencent, hsbc])

    assert persisted == [hsbc]
    assert tencent.entity_event_id is None
    assert tencent.created_at is None
    assert isinstance(hsbc.entity_event_id, UUID)
    assert hsbc.created_at is not None


async def persist_entities_no_entities(db, tx):
    assert await EntityEventRepository(db).persist_entities([]) == []
    tx.execute_returning.assert_not_awaited()


async def fetch_entity_event_ids_by_source_url_success(db):
    ids = [UUID(int=1), UUID(int=2)]
    db.fetch_all.return_value = [{"entity_event_id": entity_event_id} for entity_event_id in ids]

    result = await EntityEventRepository(db).fetch_entity_event_ids_by_source_url(SOURCE_URL)

    assert result == ids
    db.fetch_all.assert_awaited_once_with(
        repository_module._SELECT_ENTITY_EVENT_IDS_BY_SOURCE_URL_SQL, SOURCE_URL
    )


async def fetch_entity_event_ids_by_source_url_none(db):
    db.fetch_all.return_value = []

    assert await EntityEventRepository(db).fetch_entity_event_ids_by_source_url(SOURCE_URL) == []


def get_entity_event_repository_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        repository_module.get_entity_event_repository()


def create_entity_event_repository_success(monkeypatch, db):
    monkeypatch.setattr(connection_module, "_db_client", db)

    repository = repository_module.create_entity_event_repository()

    assert repository_module.get_entity_event_repository() is repository
