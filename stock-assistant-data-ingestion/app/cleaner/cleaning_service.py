import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from app.cleaner.stream_handler import StreamHandler, StreamMessage
from app.common.text_utils import normalise
from app.config import CleanConfig
from app.db.connection import DatabaseClient
from app.models.cleaned_news import CleanedNews
from app.models.raw_news import RawNews

logger = logging.getLogger(__name__)


class CleaningService:
    """
    The heart of the cleaning layer. Runs the consumer loop and orchestrates
    the 7-step cleaning pipeline for each raw_news record.
    """

    def __init__(
        self,
        db: DatabaseClient,
        stream_handler: StreamHandler,
        config: CleanConfig,
    ):
        self._db = db
        self._stream_handler = stream_handler
        self._config = config

    async def start(self) -> None:
        """
        Entry point for the cleaning layer. Creates the internal asyncio.Queue,
        starts the stream reader coroutine, and launches CLEAN_WORKER_CONCURRENCY
        worker coroutines. All run concurrently via asyncio.gather().
        """
        queue: asyncio.Queue = asyncio.Queue()

        coroutines = [
            self._stream_reader(queue),
            self._reclaim_loop(queue),
        ]
        for i in range(self._config.worker_concurrency):
            coroutines.append(self._worker(queue, worker_id=i))

        logger.info(
            "[cleaning_service] starting: %d workers, reclaim_timeout=%d ms",
            self._config.worker_concurrency,
            self._config.stream_claim_timeout_ms,
        )
        await asyncio.gather(*coroutines)

    async def _stream_reader(self, queue: asyncio.Queue) -> None:
        """Loop forever: read new messages and enqueue them for workers."""
        while True:
            try:
                messages = await self._stream_handler.read_messages()
                for msg in messages:
                    await queue.put(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "[cleaning_service] stream_reader error; retrying in 5s",
                    exc_info=True,
                )
                await asyncio.sleep(5)

    async def _reclaim_loop(self, queue: asyncio.Queue) -> None:
        """Periodically reclaim pending messages and enqueue them."""
        interval_s = self._config.stream_claim_timeout_ms / 1000
        while True:
            try:
                await asyncio.sleep(interval_s)
                messages = await self._stream_handler.reclaim_pending(
                    self._config.stream_claim_timeout_ms
                )
                for msg in messages:
                    await queue.put(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "[cleaning_service] reclaim_loop error; retrying next cycle",
                    exc_info=True,
                )

    async def _worker(self, queue: asyncio.Queue, worker_id: int) -> None:
        """Loop forever: dequeue work items and process them."""
        while True:
            msg: StreamMessage = await queue.get()
            raw_id = msg.raw_id
            message_id = msg.message_id
            try:
                await self.process_record(UUID(raw_id))
                await self._stream_handler.ack(message_id)
                logger.debug(
                    "[cleaning_service] worker-%d acked raw_id=%s", worker_id, raw_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Leave unACKed — reclaim_loop will redeliver
                logger.error(
                    "[cleaning_service] worker-%d failed raw_id=%s; leaving unACKed",
                    worker_id,
                    raw_id,
                    exc_info=True,
                )
            finally:
                queue.task_done()

    async def process_record(self, raw_id: UUID) -> None:
        """
        Run the full 7-step cleaning pipeline for one raw_news record.

        On success: write cleaned_news, publish stream:raw_news_cleaned.
        On rejection: mark is_deleted=True in raw_news, log reason.
        On storage failure: raise exception (message remains unACKed for redelivery).
        """
        # 1. Fetch raw_news (query 1)
        raw = await self._fetch_raw_news(raw_id)
        if raw is None:
            logger.warning(
                "[cleaning_service] raw_id=%s not found in raw_news; skipping", raw_id
            )
            return

        # 2. Validate title and body
        if not raw.title or not raw.body:
            await self._mark_deleted(raw_id, "EMPTY_FIELD")
            logger.info(
                "[cleaning_service] raw_id=%s rejected: EMPTY_FIELD", raw_id
            )
            return

        # 3. Normalise title and body, check body length
        title_cleaned = normalise(raw.title)
        body_cleaned = normalise(raw.body)

        if len(body_cleaned) < self._config.body_min_length:
            await self._mark_deleted(raw_id, "BODY_TOO_SHORT")
            logger.info(
                "[cleaning_service] raw_id=%s rejected: BODY_TOO_SHORT (len=%d < %d)",
                raw_id,
                len(body_cleaned),
                self._config.body_min_length,
            )
            return

        # 4. Idempotency check (query 2)
        if await self._already_cleaned(raw_id):
            logger.debug(
                "[cleaning_service] raw_id=%s already cleaned; skipping", raw_id
            )
            return

        # 5. Insert cleaned_news (query 3) and publish stream signal
        cleaned = CleanedNews(
            cleaned_id=uuid4(),
            raw_id=raw_id,
            title_cleaned=title_cleaned,
            body_cleaned=body_cleaned,
            created_at=datetime.now(timezone.utc),
        )
        await self._insert_cleaned_news(cleaned)
        await self._stream_handler.publish_cleaned(cleaned.cleaned_id)
        logger.info(
            "[cleaning_service] raw_id=%s cleaned → cleaned_id=%s",
            raw_id,
            cleaned.cleaned_id,
        )

    async def _already_cleaned(self, raw_id: UUID) -> bool:
        """Check if a cleaned_news record already exists for this raw_id."""
        row = await self._db.fetch_one(
            "SELECT 1 FROM cleaned_news WHERE raw_id = $1",
            raw_id,
        )
        return row is not None

    async def _mark_deleted(self, raw_id: UUID, reason: str) -> None:
        """Set is_deleted=True and deleted_reason on a raw_news record."""
        await self._db.execute(
            "UPDATE raw_news SET is_deleted = TRUE, deleted_reason = $1 WHERE raw_id = $2",
            reason,
            raw_id,
        )

    async def _fetch_raw_news(self, raw_id: UUID) -> Optional[RawNews]:
        """Fetch a single raw_news record by primary key."""
        row = await self._db.fetch_one(
            "SELECT raw_id, source_name, source_url, title, body, published_at, "
            "created_at, raw_hash, extra_metadata, is_deleted, deleted_reason "
            "FROM raw_news WHERE raw_id = $1 AND is_deleted = FALSE",
            raw_id,
        )
        if row is None:
            return None
        return RawNews(
            raw_id=row["raw_id"],
            source_name=row["source_name"],
            source_url=row["source_url"],
            title=row["title"],
            body=row["body"],
            published_at=row["published_at"],
            created_at=row["created_at"],
            raw_hash=row["raw_hash"],
            extra_metadata=row["extra_metadata"],
            is_deleted=row["is_deleted"],
            deleted_reason=row["deleted_reason"],
        )

    async def _insert_cleaned_news(self, record: CleanedNews) -> None:
        """
        Insert the cleaned record into cleaned_news table.
        Must succeed before ACKing the stream message.
        """
        await self._db.execute(
            "INSERT INTO cleaned_news (cleaned_id, raw_id, title_cleaned, body_cleaned, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            record.cleaned_id,
            record.raw_id,
            record.title_cleaned,
            record.body_cleaned,
            record.created_at,
        )
