import dataclasses
import json
from datetime import datetime

import asyncpg

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.db.connection import DatabaseClient, get_db_client
from app.models.event_score import EventScore, LLMScore, RuleScore
from app.models.versioned_metadata import VersionedMetadata

# Written only while the Event's stored aggregation_updated_at is still $11: otherwise the
# SELECT yields no row and nothing is inserted or updated. score_id and created_at are not in
# SET, so a re-score keeps them.
_UPSERT_EVENT_SCORE_SQL = """
    INSERT INTO event_scores (
        score_id, event_id, base_rule_score, abs_final_score, stock_impact_score, direction,
        llm_fallback, rule_score_detail, llm_score_detail, scored_at,
        scored_aggregation_updated_at, updated_at, created_at
    )
    SELECT $9, events.event_id, $2, $3, $4, $5, $6, $7, $8, $10, $11, now(), now()
    FROM events
    WHERE events.event_id = $1 AND events.aggregation_updated_at = $11
    ON CONFLICT (event_id) DO UPDATE SET
        base_rule_score = EXCLUDED.base_rule_score,
        abs_final_score = EXCLUDED.abs_final_score,
        stock_impact_score = EXCLUDED.stock_impact_score,
        direction = EXCLUDED.direction,
        llm_fallback = EXCLUDED.llm_fallback,
        rule_score_detail = EXCLUDED.rule_score_detail,
        llm_score_detail = EXCLUDED.llm_score_detail,
        scored_at = EXCLUDED.scored_at,
        scored_aggregation_updated_at = EXCLUDED.scored_aggregation_updated_at,
        updated_at = now()
    RETURNING *
"""


def _row_to_event_score(row: asyncpg.Record) -> EventScore:
    rule_score_detail = json.loads(row["rule_score_detail"])
    llm_score_detail = json.loads(row["llm_score_detail"])
    return EventScore(
        score_id=row["score_id"],
        event_id=row["event_id"],
        abs_final_score=row["abs_final_score"],
        base_rule_score=row["base_rule_score"],
        stock_impact_score=row["stock_impact_score"],
        direction=row["direction"],
        llm_fallback=row["llm_fallback"],
        rule_score_detail=VersionedMetadata(
            version=rule_score_detail["version"], output=RuleScore(**rule_score_detail["output"])
        ),
        llm_score_detail=VersionedMetadata(
            version=llm_score_detail["version"],
            output=LLMScore.model_validate(llm_score_detail["output"]),
        ),
        scored_at=row["scored_at"],
        scored_aggregation_updated_at=row["scored_aggregation_updated_at"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
    )


class EventScoreRepository:
    """Reads and writes the event_scores table."""

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def persist_score_if_not_stale(
        self, score: EventScore, expected_aggregation_updated_at: datetime
    ) -> EventScore | None:
        """
        Write the Event's score, replacing any score it already has, unless the stored Event's
        aggregation_updated_at is no longer expected_aggregation_updated_at. Returns the score
        as stored, or None if nothing was written. A re-score keeps the row's score_id.
        Raises ServiceUnavailableError or DatabaseError.
        """
        values = {
            "event_id": score.event_id,
            "base_rule_score": score.base_rule_score,
            "abs_final_score": score.abs_final_score,
            "stock_impact_score": score.stock_impact_score,
            "direction": score.direction,
            "llm_fallback": score.llm_fallback,
            "rule_score_detail": json.dumps(
                {
                    "version": score.rule_score_detail.version,
                    "output": dataclasses.asdict(score.rule_score_detail.output),
                }
            ),
            "llm_score_detail": json.dumps(
                {
                    "version": score.llm_score_detail.version,
                    "output": score.llm_score_detail.output.model_dump(),
                },
                ensure_ascii=False,
            ),
            "score_id": score.score_id,
            "scored_at": score.scored_at,
            "expected_aggregation_updated_at": expected_aggregation_updated_at,
        }
        # values' key order is _UPSERT_EVENT_SCORE_SQL's $1...$11 order.
        row = await self._db.execute_returning(_UPSERT_EVENT_SCORE_SQL, *values.values())
        return _row_to_event_score(row) if row is not None else None


_event_score_repository: EventScoreRepository | None = None


def get_event_score_repository() -> EventScoreRepository:
    """Return the process's one EventScoreRepository instance."""
    if _event_score_repository is None:
        raise ServiceUnavailableError(
            "event score repository is not initialized", category=ErrorCategory.DB
        )
    return _event_score_repository


def create_event_score_repository() -> EventScoreRepository:
    """Create the EventScoreRepository and register it as this module's singleton."""
    global _event_score_repository
    _event_score_repository = EventScoreRepository(get_db_client())
    return _event_score_repository
