import uuid

import asyncpg
import pytest

from tests.integration.conftest import Infra

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

# (column, type, nullable) of entity_events, in table order.
ENTITY_EVENTS_COLUMNS = [
    ("entity_event_id", "uuid", "NO"),
    ("exchange", "character varying(10)", "NO"),
    ("source_url", "text", "NO"),
    ("source_name", "character varying(50)", "NO"),
    ("published_at", "timestamp with time zone", "YES"),
    ("stock_code", "character varying(20)", "NO"),
    ("event_type_primary", "character varying(50)", "NO"),
    ("sentiment_label", "character varying(10)", "NO"),
    ("sentiment_score", "double precision", "NO"),
    ("headline", "text", "NO"),
    ("entity_summary", "text", "NO"),
    ("additional_outcome", "jsonb", "NO"),
    ("metadata", "jsonb", "NO"),
    ("created_at", "timestamp with time zone", "NO"),
    ("updated_at", "timestamp with time zone", "NO"),
]
ENTITY_EVENTS_INDEXES = {
    "entity_events_pkey": "CREATE UNIQUE INDEX entity_events_pkey ON public.entity_events "
    "USING btree (entity_event_id)",
    "uq_entity_events_source_url_stock_code": "CREATE UNIQUE INDEX "
    "uq_entity_events_source_url_stock_code ON public.entity_events "
    "USING btree (source_url, stock_code)",
    "idx_entity_events_exchange": "CREATE INDEX idx_entity_events_exchange ON "
    "public.entity_events USING btree (exchange)",
    "idx_entity_events_stock_event_type": "CREATE INDEX idx_entity_events_stock_event_type ON "
    "public.entity_events USING btree (stock_code, event_type_primary)",
}


async def _table_exists(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('entity_events') IS NOT NULL"))


async def upgrade_downgrade_001_success(infra: Infra, alembic):
    """From an empty schema, upgrade creates entity_events as specified, downgrade removes it,
    and upgrading again succeeds — all in SAPI's own sapi database."""
    alembic("downgrade", "base")
    conn = await asyncpg.connect(infra.env()["DATABASE_URL"])
    try:
        assert await conn.fetchval("SELECT current_database()") == "sapi"
        assert not await _table_exists(conn)

        alembic("upgrade", "001")

        columns = await conn.fetch(
            "SELECT column_name, format_type(atttypid, atttypmod) AS type, is_nullable"
            " FROM information_schema.columns"
            " JOIN pg_attribute ON attrelid = 'entity_events'::regclass AND attname = column_name"
            " WHERE table_name = 'entity_events' ORDER BY ordinal_position"
        )
        assert [tuple(row) for row in columns] == ENTITY_EVENTS_COLUMNS
        constraint = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'uq_entity_events_source_url_stock_code'"
        )
        assert constraint == "UNIQUE (source_url, stock_code)"
        indexes = await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'entity_events'"
        )
        assert dict(tuple(row) for row in indexes) == ENTITY_EVENTS_INDEXES
        assert await conn.fetch("SELECT version_num FROM alembic_version") == [("001",)]

        alembic("downgrade", "-1")

        assert not await _table_exists(conn)
        assert await conn.fetchval("SELECT count(*) FROM alembic_version") == 0

        alembic("upgrade", "head")

        assert await _table_exists(conn)
        assert await conn.fetchval("SELECT current_database()") == "sapi"
    finally:
        alembic("upgrade", "head")
        await conn.close()


# (column, type, nullable) of events and event_entity_map, in table order.
EVENTS_COLUMNS = [
    ("event_id", "uuid", "NO"),
    ("exchange", "character varying(10)", "NO"),
    ("stock_code", "character varying(20)", "NO"),
    ("event_type_primary", "character varying(50)", "NO"),
    ("event_type_secondary", "jsonb", "YES"),
    ("source_list", "jsonb", "NO"),
    ("first_seen_at", "timestamp with time zone", "NO"),
    ("last_seen_at", "timestamp with time zone", "NO"),
    ("brief_summary", "jsonb", "YES"),
    ("aggregation_updated_at", "timestamp with time zone", "NO"),
    ("published_aggregation_updated_at", "timestamp with time zone", "YES"),
    ("updated_at", "timestamp with time zone", "NO"),
    ("created_at", "timestamp with time zone", "NO"),
]
EVENT_ENTITY_MAP_COLUMNS = [
    ("event_id", "uuid", "NO"),
    ("entity_event_id", "uuid", "NO"),
    ("created_at", "timestamp with time zone", "NO"),
]
EVENTS_INDEXES = {
    "events_pkey": "CREATE UNIQUE INDEX events_pkey ON public.events USING btree (event_id)",
    "idx_events_exchange": "CREATE INDEX idx_events_exchange ON public.events "
    "USING btree (exchange)",
    "idx_events_stock_event_type": "CREATE INDEX idx_events_stock_event_type ON public.events "
    "USING btree (stock_code, event_type_primary)",
    "idx_events_aggregation_updated_at": "CREATE INDEX idx_events_aggregation_updated_at ON "
    "public.events USING btree (aggregation_updated_at)",
    "idx_events_stock_code": "CREATE INDEX idx_events_stock_code ON public.events "
    "USING btree (stock_code)",
}
EVENT_ENTITY_MAP_INDEXES = {
    "pk_event_entity_map": "CREATE UNIQUE INDEX pk_event_entity_map ON public.event_entity_map "
    "USING btree (event_id, entity_event_id)",
    "uq_event_entity_map_entity_event_id": "CREATE UNIQUE INDEX "
    "uq_event_entity_map_entity_event_id ON public.event_entity_map "
    "USING btree (entity_event_id)",
    "idx_event_entity_map_event_id": "CREATE INDEX idx_event_entity_map_event_id ON "
    "public.event_entity_map USING btree (event_id)",
}
_INSERT_ENTITY_EVENT_SQL = """
    INSERT INTO entity_events (
        entity_event_id, exchange, source_url, source_name, published_at, stock_code,
        event_type_primary, sentiment_label, sentiment_score, headline, entity_summary,
        additional_outcome, metadata, created_at, updated_at
    )
    VALUES ($1, 'HKEX', $2, 'MINGPAO', now(), '00700', 'EARNINGS', 'POSITIVE', 0.8, 'h', 's',
            '{}', '{}', now(), now())
"""
_INSERT_EVENT_SQL = """
    INSERT INTO events (
        event_id, exchange, stock_code, event_type_primary, source_list, first_seen_at,
        last_seen_at, aggregation_updated_at, updated_at, created_at
    )
    VALUES ($1, 'HKEX', '00700', 'EARNINGS', '[]', now(), now(), now(), now(), now())
"""
_INSERT_MAP_SQL = (
    "INSERT INTO event_entity_map (event_id, entity_event_id, created_at) VALUES ($1, $2, now())"
)


async def _columns(conn: asyncpg.Connection, table: str) -> list[tuple]:
    rows = await conn.fetch(
        "SELECT column_name, format_type(atttypid, atttypmod) AS type, is_nullable"
        " FROM information_schema.columns"
        " JOIN pg_attribute ON attrelid = $1::text::regclass AND attname = column_name"
        " WHERE table_name = $1::text ORDER BY ordinal_position",
        table,
    )
    return [tuple(row) for row in rows]


async def _indexes(conn: asyncpg.Connection, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = $1", table
    )
    return dict(tuple(row) for row in rows)


async def upgrade_downgrade_002_success(infra: Infra, alembic):
    """Upgrading from 001 creates events and event_entity_map as specified, whose constraints
    tie each EntityEvent to one stored Event. Downgrading removes both and leaves entity_events
    and its rows."""
    alembic("downgrade", "base")
    alembic("upgrade", "001")
    conn = await asyncpg.connect(infra.env()["DATABASE_URL"])
    try:
        entity_event_id, event_id, other_event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute(_INSERT_ENTITY_EVENT_SQL, entity_event_id, f"u{entity_event_id}")

        alembic("upgrade", "002")

        assert await _columns(conn, "events") == EVENTS_COLUMNS
        assert await _columns(conn, "event_entity_map") == EVENT_ENTITY_MAP_COLUMNS
        assert await _indexes(conn, "events") == EVENTS_INDEXES
        assert await _indexes(conn, "event_entity_map") == EVENT_ENTITY_MAP_INDEXES
        assert await conn.fetch("SELECT version_num FROM alembic_version") == [("002",)]
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(_INSERT_MAP_SQL, uuid.uuid4(), entity_event_id)
        await conn.execute(_INSERT_EVENT_SQL, event_id)
        await conn.execute(_INSERT_EVENT_SQL, other_event_id)
        await conn.execute(_INSERT_MAP_SQL, event_id, entity_event_id)
        with pytest.raises(asyncpg.UniqueViolationError) as excinfo:
            await conn.execute(_INSERT_MAP_SQL, other_event_id, entity_event_id)
        assert excinfo.value.constraint_name == "uq_event_entity_map_entity_event_id"

        alembic("downgrade", "-1")

        assert await conn.fetchval("SELECT to_regclass('events') IS NULL")
        assert await conn.fetchval("SELECT to_regclass('event_entity_map') IS NULL")
        assert await conn.fetch("SELECT entity_event_id FROM entity_events") == [(entity_event_id,)]
        assert await conn.fetch("SELECT version_num FROM alembic_version") == [("001",)]
    finally:
        alembic("downgrade", "base")
        alembic("upgrade", "head")
        await conn.close()


# (column, type, nullable) of event_scores, in table order.
EVENT_SCORES_COLUMNS = [
    ("score_id", "uuid", "NO"),
    ("event_id", "uuid", "NO"),
    ("abs_final_score", "double precision", "NO"),
    ("base_rule_score", "double precision", "NO"),
    ("stock_impact_score", "double precision", "NO"),
    ("direction", "smallint", "NO"),
    ("llm_fallback", "boolean", "NO"),
    ("rule_score_detail", "jsonb", "NO"),
    ("llm_score_detail", "jsonb", "NO"),
    ("scored_at", "timestamp with time zone", "NO"),
    ("scored_aggregation_updated_at", "timestamp with time zone", "NO"),
    ("updated_at", "timestamp with time zone", "NO"),
    ("created_at", "timestamp with time zone", "NO"),
]
EVENT_SCORES_INDEXES = {
    "event_scores_pkey": "CREATE UNIQUE INDEX event_scores_pkey ON public.event_scores "
    "USING btree (score_id)",
    "uq_event_scores_event_id": "CREATE UNIQUE INDEX uq_event_scores_event_id ON "
    "public.event_scores USING btree (event_id)",
    "idx_event_scores_abs_final_score": "CREATE INDEX idx_event_scores_abs_final_score ON "
    "public.event_scores USING btree (abs_final_score)",
    "idx_event_scores_base_rule_score": "CREATE INDEX idx_event_scores_base_rule_score ON "
    "public.event_scores USING btree (base_rule_score)",
    "idx_event_scores_llm_fallback": "CREATE INDEX idx_event_scores_llm_fallback ON "
    "public.event_scores USING btree (llm_fallback)",
}
_INSERT_EVENT_SCORE_SQL = """
    INSERT INTO event_scores (
        score_id, event_id, abs_final_score, base_rule_score, stock_impact_score, direction,
        llm_fallback, rule_score_detail, llm_score_detail, scored_at,
        scored_aggregation_updated_at, updated_at, created_at
    )
    VALUES ($1, $2, 4.29, 6.41, 0.0, 1, true, '{}', '{}', now(), now(), now(), now())
"""


async def upgrade_downgrade_003_success(infra: Infra, alembic):
    """Upgrading from 002 creates event_scores as specified, holding at most one score per
    stored Event. Downgrading removes it and leaves events and its rows."""
    alembic("downgrade", "base")
    alembic("upgrade", "002")
    conn = await asyncpg.connect(infra.env()["DATABASE_URL"])
    try:
        event_id = uuid.uuid4()
        await conn.execute(_INSERT_EVENT_SQL, event_id)

        alembic("upgrade", "head")

        assert await _columns(conn, "event_scores") == EVENT_SCORES_COLUMNS
        assert await _indexes(conn, "event_scores") == EVENT_SCORES_INDEXES
        assert await conn.fetch("SELECT version_num FROM alembic_version") == [("003",)]
        await conn.execute(_INSERT_EVENT_SCORE_SQL, uuid.uuid4(), event_id)
        with pytest.raises(asyncpg.UniqueViolationError) as excinfo:
            await conn.execute(_INSERT_EVENT_SCORE_SQL, uuid.uuid4(), event_id)
        assert excinfo.value.constraint_name == "uq_event_scores_event_id"
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(_INSERT_EVENT_SCORE_SQL, uuid.uuid4(), uuid.uuid4())

        alembic("downgrade", "-1")

        assert await conn.fetchval("SELECT to_regclass('event_scores') IS NULL")
        assert await conn.fetch("SELECT event_id FROM events") == [(event_id,)]
        assert await conn.fetch("SELECT version_num FROM alembic_version") == [("002",)]
    finally:
        alembic("downgrade", "base")
        alembic("upgrade", "head")
        await conn.close()
