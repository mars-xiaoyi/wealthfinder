"""Create events and event_entity_map tables

Revision ID: 002
Revises: 001
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No source_count column: it is jsonb_array_length(source_list).
    # published_aggregation_updated_at is the last aggregation_updated_at announced on
    # stream:event_aggregated; NULL until the first one.
    op.execute("""
        CREATE TABLE events (
            event_id                          UUID PRIMARY KEY,
            exchange                          VARCHAR(10) NOT NULL,
            stock_code                        VARCHAR(20) NOT NULL,
            event_type_primary                VARCHAR(50) NOT NULL,
            event_type_secondary              JSONB,
            source_list                       JSONB NOT NULL,
            first_seen_at                     TIMESTAMPTZ NOT NULL,
            last_seen_at                      TIMESTAMPTZ NOT NULL,
            brief_summary                     JSONB,
            aggregation_updated_at            TIMESTAMPTZ NOT NULL,
            published_aggregation_updated_at  TIMESTAMPTZ,
            updated_at                        TIMESTAMPTZ NOT NULL,
            created_at                        TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_events_exchange ON events (exchange)")
    op.execute(
        "CREATE INDEX idx_events_stock_event_type ON events (stock_code, event_type_primary)"
    )
    op.execute("CREATE INDEX idx_events_aggregation_updated_at ON events (aggregation_updated_at)")
    op.execute("CREATE INDEX idx_events_stock_code ON events (stock_code)")
    # Unique on entity_event_id: an EntityEvent belongs to exactly one Event.
    op.execute("""
        CREATE TABLE event_entity_map (
            event_id         UUID NOT NULL REFERENCES events(event_id),
            entity_event_id  UUID NOT NULL REFERENCES entity_events(entity_event_id),
            created_at       TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_event_entity_map PRIMARY KEY (event_id, entity_event_id),
            CONSTRAINT uq_event_entity_map_entity_event_id UNIQUE (entity_event_id)
        )
    """)
    op.execute("CREATE INDEX idx_event_entity_map_event_id ON event_entity_map (event_id)")


def downgrade() -> None:
    op.execute("DROP TABLE event_entity_map")
    op.execute("DROP TABLE events")
