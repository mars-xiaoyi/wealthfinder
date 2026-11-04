"""Create event_scores table

Revision ID: 003
Revises: 002
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # scored_aggregation_updated_at is the events.aggregation_updated_at the score was
    # computed for.
    op.execute("""
        CREATE TABLE event_scores (
            score_id                       UUID PRIMARY KEY,
            event_id                       UUID NOT NULL REFERENCES events(event_id),
            abs_final_score                FLOAT NOT NULL,
            base_rule_score                FLOAT NOT NULL,
            stock_impact_score             FLOAT NOT NULL,
            direction                      SMALLINT NOT NULL,
            llm_fallback                   BOOLEAN NOT NULL,
            rule_score_detail              JSONB NOT NULL,
            llm_score_detail               JSONB NOT NULL,
            scored_at                      TIMESTAMPTZ NOT NULL,
            scored_aggregation_updated_at  TIMESTAMPTZ NOT NULL,
            updated_at                     TIMESTAMPTZ NOT NULL,
            created_at                     TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_event_scores_event_id UNIQUE (event_id)
        )
    """)
    op.execute("CREATE INDEX idx_event_scores_abs_final_score ON event_scores (abs_final_score)")
    op.execute("CREATE INDEX idx_event_scores_base_rule_score ON event_scores (base_rule_score)")
    op.execute("CREATE INDEX idx_event_scores_llm_fallback ON event_scores (llm_fallback)")


def downgrade() -> None:
    op.execute("DROP TABLE event_scores")
