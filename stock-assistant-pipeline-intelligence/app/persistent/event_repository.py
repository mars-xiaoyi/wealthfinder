import json
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.db.connection import DatabaseClient, get_db_client
from app.models.entity_event import EventTypeEnum
from app.models.event import EntitySource, Event
from app.models.event_entity_map import EventEntityMap

# Only newer merge state is written: an unchanged Event, or an older snapshot of one, writes
# nothing and returns no row. created_at is not in SET, so a re-flush keeps it.
# Missing event_type_secondary and source_list are computed from the constituent entity_events
# rows, matched by $10 rather than through event_entity_map, whose rows are written after this.
# A row without a publication time is dated by when it was stored.
_UPSERT_EVENT_SQL = """
    INSERT INTO events (
        event_id, exchange, stock_code, event_type_primary, first_seen_at, last_seen_at,
        aggregation_updated_at, event_type_secondary, source_list, updated_at, created_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7,
        COALESCE($8::jsonb, (
            -- The two most frequent secondary types; the more recently reported one wins a tie.
            SELECT COALESCE(
                jsonb_agg(secondary.event_type ORDER BY secondary.frequency DESC,
                          secondary.latest DESC, secondary.event_type),
                '[]'::jsonb
            )
            FROM (
                SELECT additional_outcome->'output'->>'event_type_secondary' AS event_type,
                       count(*) AS frequency,
                       max(COALESCE(published_at, created_at)) AS latest
                FROM entity_events
                WHERE entity_event_id = ANY($10::uuid[])
                  AND additional_outcome->'output'->>'event_type_secondary' IS NOT NULL
                GROUP BY 1
                ORDER BY frequency DESC, latest DESC, event_type
                LIMIT 2
            ) AS secondary
        )),
        COALESCE($9::jsonb, (
            -- One entry per source, from its most recent report.
            SELECT jsonb_agg(
                jsonb_build_object('source_name', source.source_name, 'url', source.source_url,
                                   'published_at', source.published_at)
                ORDER BY source.source_name
            )
            FROM (
                SELECT DISTINCT ON (source_name)
                       source_name, source_url, COALESCE(published_at, created_at) AS published_at
                FROM entity_events
                WHERE entity_event_id = ANY($10::uuid[])
                ORDER BY source_name, COALESCE(published_at, created_at) DESC
            ) AS source
        )),
        now(), now()
    )
    ON CONFLICT (event_id) DO UPDATE SET
        first_seen_at = EXCLUDED.first_seen_at,
        last_seen_at = EXCLUDED.last_seen_at,
        aggregation_updated_at = EXCLUDED.aggregation_updated_at,
        event_type_secondary = EXCLUDED.event_type_secondary,
        source_list = EXCLUDED.source_list,
        updated_at = now()
    WHERE events.aggregation_updated_at < EXCLUDED.aggregation_updated_at
    RETURNING *
"""

# A re-flush of a still-active Event re-sends every membership it already wrote.
_INSERT_EVENT_ENTITY_MAP_SQL = """
    INSERT INTO event_entity_map (event_id, entity_event_id, created_at)
    VALUES ($1, $2, $3)
    ON CONFLICT DO NOTHING
"""

_SELECT_UNPUBLISHED_EVENTS_SQL = """
    SELECT * FROM events
    WHERE event_id = ANY($1)
      AND (published_aggregation_updated_at IS NULL
           OR published_aggregation_updated_at < aggregation_updated_at)
"""

# Only moves forward, so marking an older version never un-marks a newer one.
_MARK_PUBLISHED_SQL = """
    UPDATE events SET published_aggregation_updated_at = $2
    WHERE event_id = $1
      AND (published_aggregation_updated_at IS NULL OR published_aggregation_updated_at < $2)
"""

_SELECT_MAPPED_EVENT_ID_SQL = "SELECT event_id FROM event_entity_map WHERE entity_event_id = $1"


def _row_to_event(row: asyncpg.Record) -> Event:
    return Event(
        event_id=row["event_id"],
        exchange=row["exchange"],
        stock_code=row["stock_code"],
        event_type_primary=EventTypeEnum(row["event_type_primary"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        event_type_secondary=[
            EventTypeEnum(event_type) for event_type in json.loads(row["event_type_secondary"])
        ],
        source_list=[
            EntitySource(
                source_name=source["source_name"],
                url=source["url"],
                published_at=datetime.fromisoformat(source["published_at"]),
            )
            for source in json.loads(row["source_list"])
        ],
        aggregation_updated_at=row["aggregation_updated_at"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
    )


def _get_event_type_secondary_json(event: Event) -> str | None:
    if event.event_type_secondary is None:
        return None
    return json.dumps([str(event_type) for event_type in event.event_type_secondary])


def _get_source_list_json(event: Event) -> str | None:
    if event.source_list is None:
        return None
    return json.dumps(
        [
            {
                "source_name": source.source_name,
                "url": source.url,
                "published_at": source.published_at.isoformat(),
            }
            for source in event.source_list
        ]
    )


class EventRepository:
    """Reads and writes the events and event_entity_map tables."""

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def persist_events(self, active_events: list[Event]) -> list[Event]:
        """
        Write the active Events and their EntityEvent memberships as one unit: if any write
        fails, none is stored. Returns the Events written, fully populated: new ones, and ones
        whose merge state is newer than the stored one. An Event without event_type_secondary
        or source_list gets them computed from its EntityEvents.
        Raises ServiceUnavailableError or DatabaseError.
        """
        written = []
        now = datetime.now(UTC)
        async with self._db.transaction() as tx:
            for event in active_events:
                # Set on an active Event, which is all the caller passes.
                assert event.aggregation_updated_at is not None
                assert event.entity_event_ids is not None
                values = {
                    "event_id": event.event_id,
                    "exchange": event.exchange,
                    "stock_code": event.stock_code,
                    "event_type_primary": event.event_type_primary,
                    "first_seen_at": event.first_seen_at,
                    "last_seen_at": event.last_seen_at,
                    "aggregation_updated_at": event.aggregation_updated_at,
                    "event_type_secondary": _get_event_type_secondary_json(event),
                    "source_list": _get_source_list_json(event),
                    "entity_event_ids": event.entity_event_ids,
                }
                # values' key order is _UPSERT_EVENT_SQL's $1...$10 order.
                row = await tx.execute_returning(_UPSERT_EVENT_SQL, *values.values())
                for entity_event_id in event.entity_event_ids:
                    mapping = EventEntityMap(event.event_id, entity_event_id, now)
                    await tx.execute(
                        _INSERT_EVENT_ENTITY_MAP_SQL,
                        mapping.event_id,
                        mapping.entity_event_id,
                        mapping.created_at,
                    )
                if row is not None:
                    written.append(_row_to_event(row))
        return written

    async def fetch_unpublished_events(self, event_ids: list[UUID]) -> list[Event]:
        """
        Return the stored Events among event_ids whose current merge state has not been
        marked published.
        Raises ServiceUnavailableError or DatabaseError.
        """
        rows = await self._db.fetch_all(_SELECT_UNPUBLISHED_EVENTS_SQL, event_ids)
        return [_row_to_event(row) for row in rows]

    async def mark_published(self, event_id: UUID, aggregation_updated_at: datetime) -> None:
        """
        Record that the Event's merge state as of aggregation_updated_at was published; a
        newer version already marked stays marked.
        Raises ServiceUnavailableError or DatabaseError.
        """
        await self._db.execute(_MARK_PUBLISHED_SQL, event_id, aggregation_updated_at)

    async def fetch_mapped_event_id(self, entity_event_id: UUID) -> UUID | None:
        """
        Return the id of the stored Event the EntityEvent belongs to, or None if it belongs
        to none.
        Raises ServiceUnavailableError or DatabaseError.
        """
        row = await self._db.fetch_one(_SELECT_MAPPED_EVENT_ID_SQL, entity_event_id)
        return row["event_id"] if row is not None else None


_event_repository: EventRepository | None = None


def get_event_repository() -> EventRepository:
    """Return the process's one EventRepository instance."""
    if _event_repository is None:
        raise ServiceUnavailableError(
            "event repository is not initialized", category=ErrorCategory.DB
        )
    return _event_repository


def create_event_repository() -> EventRepository:
    """Create the EventRepository and register it as this module's singleton."""
    global _event_repository
    _event_repository = EventRepository(get_db_client())
    return _event_repository
