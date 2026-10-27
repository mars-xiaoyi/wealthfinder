"""Create entity_events table

Revision ID: 001
Revises: None
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Unique on (source_url, stock_code), not source_url alone: one article yields one row per
    # verified entity.
    op.execute("""
        CREATE TABLE entity_events (
            entity_event_id     UUID PRIMARY KEY,
            exchange            VARCHAR(10) NOT NULL,
            source_url          TEXT NOT NULL,
            source_name         VARCHAR(50) NOT NULL,
            published_at        TIMESTAMPTZ,
            stock_code          VARCHAR(20) NOT NULL,
            event_type_primary  VARCHAR(50) NOT NULL,
            sentiment_label     VARCHAR(10) NOT NULL,
            sentiment_score     FLOAT NOT NULL,
            headline            TEXT NOT NULL,
            entity_summary      TEXT NOT NULL,
            additional_outcome  JSONB NOT NULL,
            metadata            JSONB NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_entity_events_source_url_stock_code UNIQUE (source_url, stock_code)
        )
    """)
    op.execute("CREATE INDEX idx_entity_events_exchange ON entity_events (exchange)")
    op.execute(
        "CREATE INDEX idx_entity_events_stock_event_type "
        "ON entity_events (stock_code, event_type_primary)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE entity_events")
