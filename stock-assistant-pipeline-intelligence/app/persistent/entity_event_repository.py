import json
import uuid
from datetime import UTC, datetime
from uuid import UUID

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.db.connection import DatabaseClient, get_db_client
from app.models.entity_event import EntityEvent

_INSERT_ENTITY_EVENT_SQL = """
    INSERT INTO entity_events (
        entity_event_id, exchange, source_url, source_name, published_at, stock_code,
        event_type_primary, sentiment_label, sentiment_score, headline, entity_summary,
        additional_outcome, metadata, created_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
    ON CONFLICT (source_url, stock_code) DO NOTHING
    RETURNING entity_event_id
"""

_SELECT_ENTITY_EVENT_IDS_BY_SOURCE_URL_SQL = (
    "SELECT entity_event_id FROM entity_events WHERE source_url = $1"
)


class EntityEventRepository:
    """Reads and writes the entity_events table."""

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def persist_entities(self, entities: list[EntityEvent]) -> list[EntityEvent]:
        """
        Write the entities as one unit: if any write fails, none is stored. Returns the ones
        newly stored, each with its entity_event_id and created_at set; an entity whose article
        and stock already have a row is left out.
        Raises ServiceUnavailableError if the database is unreachable, DatabaseError on any
        other database failure.
        """
        persisted = []
        async with self._db.transaction() as tx:
            for entity in entities:
                new_id = uuid.uuid4()
                now = datetime.now(UTC)
                llm = entity.analysis_outcome.output
                additional_outcome = {
                    "version": entity.analysis_outcome.version,
                    "output": {
                        "company_name_zh": entity.stock.company_name_zh,
                        "company_name_en": entity.stock.company_name_en,
                        "event_type_secondary": llm.event_type_secondary,
                        "headline": llm.headline,
                        "entity_summary": llm.entity_summary,
                    },
                }
                values = {
                    "entity_event_id": new_id,
                    "exchange": entity.stock.exchange,
                    "source_url": entity.source_url,
                    "source_name": entity.source_name,
                    "published_at": entity.published_at,
                    "stock_code": entity.stock.stock_code,
                    "event_type_primary": llm.event_type_primary,
                    "sentiment_label": llm.sentiment_label,
                    "sentiment_score": llm.sentiment_score,
                    "headline": llm.headline,
                    "entity_summary": llm.entity_summary,
                    "additional_outcome": json.dumps(additional_outcome, ensure_ascii=False),
                    "metadata": json.dumps({"cleaned_id": str(entity.source_id)}),
                    "created_at": now,
                    "updated_at": now,
                }
                # values' key order is _INSERT_ENTITY_EVENT_SQL's $1...$15 order.
                inserted = await tx.execute_returning(_INSERT_ENTITY_EVENT_SQL, *values.values())
                if inserted is not None:
                    entity.entity_event_id = new_id
                    entity.created_at = now
                    persisted.append(entity)
        return persisted

    async def fetch_entity_event_ids_by_source_url(self, source_url: str) -> list[UUID]:
        """
        Return the entity_event_id of every row stored for the article at source_url; an
        empty list if there is none.
        Raises ServiceUnavailableError if the database is unreachable, DatabaseError on any
        other database failure.
        """
        rows = await self._db.fetch_all(_SELECT_ENTITY_EVENT_IDS_BY_SOURCE_URL_SQL, source_url)
        return [row["entity_event_id"] for row in rows]


_entity_event_repository: EntityEventRepository | None = None


def get_entity_event_repository() -> EntityEventRepository:
    """
    Return the process's one EntityEventRepository instance.
    Raises ServiceUnavailableError if called before create_entity_event_repository() has run.
    """
    if _entity_event_repository is None:
        raise ServiceUnavailableError(
            "entity event repository is not initialized", category=ErrorCategory.DB
        )
    return _entity_event_repository


def create_entity_event_repository() -> EntityEventRepository:
    """Create the EntityEventRepository and register it as this module's singleton."""
    global _entity_event_repository
    _entity_event_repository = EntityEventRepository(get_db_client())
    return _entity_event_repository
