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


async def upgrade_downgrade_success(infra: Infra, alembic):
    """From an empty schema, upgrade creates entity_events as specified, downgrade removes it,
    and upgrading again succeeds — all in SAPI's own sapi database."""
    alembic("downgrade", "base")
    conn = await asyncpg.connect(infra.env()["DATABASE_URL"])
    try:
        assert await conn.fetchval("SELECT current_database()") == "sapi"
        assert not await _table_exists(conn)

        alembic("upgrade", "head")

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
