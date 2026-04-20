"""Create raw_news, cleaned_news, and crawl_error_log tables

Revision ID: 001
Revises: None
Create Date: 2026-04-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE raw_news (
            raw_id          UUID PRIMARY KEY,
            source_name     VARCHAR(50) NOT NULL,
            source_url      TEXT NOT NULL,
            title           TEXT NOT NULL,
            body            TEXT NOT NULL,
            published_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL,
            raw_hash        VARCHAR(64) NOT NULL,
            extra_metadata  JSONB,
            is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
            deleted_reason  VARCHAR(50),
            CONSTRAINT uq_raw_news_source_url UNIQUE (source_url),
            CONSTRAINT uq_raw_news_raw_hash   UNIQUE (raw_hash)
        )
    """)
    op.execute("CREATE INDEX idx_raw_news_is_deleted ON raw_news (is_deleted)")
    op.execute("CREATE INDEX idx_raw_news_created_at ON raw_news (created_at)")
    op.execute("CREATE INDEX idx_raw_news_extra_meta ON raw_news USING GIN (extra_metadata)")

    op.execute("""
        CREATE TABLE cleaned_news (
            cleaned_id      UUID PRIMARY KEY,
            raw_id          UUID NOT NULL REFERENCES raw_news(raw_id),
            title_cleaned   TEXT NOT NULL,
            body_cleaned    TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_cleaned_news_raw_id ON cleaned_news (raw_id)")

    op.execute("""
        CREATE TABLE crawl_error_log (
            error_id        UUID PRIMARY KEY,
            execution_id    VARCHAR(100),
            source_name     VARCHAR(50) NOT NULL,
            url             TEXT NOT NULL,
            error_type      VARCHAR(50) NOT NULL,
            error_code      VARCHAR(50) NOT NULL,
            attempt_count   INTEGER NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_crawl_error_log_url ON crawl_error_log (url)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS crawl_error_log")
    op.execute("DROP TABLE IF EXISTS cleaned_news")
    op.execute("DROP TABLE IF EXISTS raw_news")
