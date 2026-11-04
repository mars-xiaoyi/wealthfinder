import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.common.errors import ServiceUnavailableError
from app.db import connection as connection_module
from app.db.connection import DatabaseClient
from app.models.event_score import EventScore, LLMScore, RuleScore
from app.models.versioned_metadata import VersionedMetadata
from app.persistent import event_score_repository as repository_module
from app.persistent.event_score_repository import EventScoreRepository

SCORE_ID = UUID("00000000-0000-0000-0000-0000000000a1")
STORED_SCORE_ID = UUID("00000000-0000-0000-0000-0000000000a0")
EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
AGGREGATION_UPDATED_AT = datetime(2026, 9, 8, 3, 0, 5, tzinfo=UTC)
SCORED_AT = datetime(2026, 9, 8, 3, 0, 7, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 9, 8, 3, 0, 8, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 8, 1, 5, tzinfo=UTC)
RULE_SCORE_DETAIL = VersionedMetadata(
    version="v1.0.0",
    output=RuleScore(
        event_type_score=9.0,
        source_authority_score=9.0,
        sentiment_strength_score=6.4,
        source_heat_score=5.0,
    ),
)
LLM_SCORE_DETAIL = VersionedMetadata(
    version="v1.2.0", output=LLMScore(stock_impact_score=-2.5, adjustment_reason="盈利遜預期")
)


def _score() -> EventScore:
    """A score as the caller builds it; updated_at and created_at are placeholders."""
    return EventScore(
        score_id=SCORE_ID,
        event_id=EVENT_ID,
        abs_final_score=2.6447,
        base_rule_score=6.41,
        stock_impact_score=-2.5,
        direction=-1,
        llm_fallback=False,
        rule_score_detail=RULE_SCORE_DETAIL,
        llm_score_detail=LLM_SCORE_DETAIL,
        scored_at=SCORED_AT,
        scored_aggregation_updated_at=AGGREGATION_UPDATED_AT,
        updated_at=SCORED_AT,
        created_at=SCORED_AT,
    )


def _row() -> dict:
    """The event_scores row of an earlier score of the Event, re-scored: JSONB columns as
    text."""
    return {
        "score_id": STORED_SCORE_ID,
        "event_id": EVENT_ID,
        "abs_final_score": 2.6447,
        "base_rule_score": 6.41,
        "stock_impact_score": -2.5,
        "direction": -1,
        "llm_fallback": False,
        "rule_score_detail": json.dumps(
            {
                "version": "v1.0.0",
                "output": {
                    "event_type_score": 9.0,
                    "source_authority_score": 9.0,
                    "sentiment_strength_score": 6.4,
                    "source_heat_score": 5.0,
                },
            }
        ),
        "llm_score_detail": json.dumps(
            {
                "version": "v1.2.0",
                "output": {"stock_impact_score": -2.5, "adjustment_reason": "盈利遜預期"},
            }
        ),
        "scored_at": SCORED_AT,
        "scored_aggregation_updated_at": AGGREGATION_UPDATED_AT,
        "updated_at": WRITTEN_AT,
        "created_at": CREATED_AT,
    }


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(repository_module, "_event_score_repository", None)


@pytest.fixture
def db():
    return AsyncMock(spec=DatabaseClient)


async def persist_score_if_not_stale_success(db):
    db.execute_returning.return_value = _row()

    result = await EventScoreRepository(db).persist_score_if_not_stale(
        _score(), AGGREGATION_UPDATED_AT
    )

    assert result == EventScore(
        score_id=STORED_SCORE_ID,
        event_id=EVENT_ID,
        abs_final_score=2.6447,
        base_rule_score=6.41,
        stock_impact_score=-2.5,
        direction=-1,
        llm_fallback=False,
        rule_score_detail=RULE_SCORE_DETAIL,
        llm_score_detail=LLM_SCORE_DETAIL,
        scored_at=SCORED_AT,
        scored_aggregation_updated_at=AGGREGATION_UPDATED_AT,
        updated_at=WRITTEN_AT,
        created_at=CREATED_AT,
    )
    args = db.execute_returning.await_args.args
    assert args[0] == repository_module._UPSERT_EVENT_SCORE_SQL
    assert args[1:7] == (EVENT_ID, 6.41, 2.6447, -2.5, -1, False)
    assert json.loads(args[7]) == {
        "version": "v1.0.0",
        "output": {
            "event_type_score": 9.0,
            "source_authority_score": 9.0,
            "sentiment_strength_score": 6.4,
            "source_heat_score": 5.0,
        },
    }
    assert json.loads(args[8]) == {
        "version": "v1.2.0",
        "output": {"stock_impact_score": -2.5, "adjustment_reason": "盈利遜預期"},
    }
    assert args[9:] == (SCORE_ID, SCORED_AT, AGGREGATION_UPDATED_AT)


async def persist_score_if_not_stale_stale(db):
    db.execute_returning.return_value = None

    result = await EventScoreRepository(db).persist_score_if_not_stale(
        _score(), AGGREGATION_UPDATED_AT
    )

    assert result is None
    args = db.execute_returning.await_args.args
    assert args[0] == repository_module._UPSERT_EVENT_SCORE_SQL
    assert args[1] == EVENT_ID
    assert args[11] == AGGREGATION_UPDATED_AT


def get_event_score_repository_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        repository_module.get_event_score_repository()


def create_event_score_repository_success(monkeypatch, db):
    monkeypatch.setattr(connection_module, "_db_client", db)

    repository = repository_module.create_event_score_repository()

    assert repository_module.get_event_score_repository() is repository
