import dataclasses
from uuid import UUID

from app.clients.news_client import get_news_client
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import EntityAnalysisConfig
from app.logger import get_logger
from app.models.entity_event import EntityEvent
from app.models.versioned_metadata import VersionedMetadata
from app.persistent.entity_event_repository import get_entity_event_repository
from app.redis.stream_client import STREAM_ENTITY_EVENT_COMPLETED, get_redis_stream_client
from app.skills.entity_analysis_skill import EntityAnalysisInput, EntityAnalysisSkill

logger = get_logger(__name__)


class EntityAnalysisService:
    """Turns one cleaned article into the events of the listed companies it mentions."""

    def __init__(self, config: EntityAnalysisConfig) -> None:
        self._news_client = get_news_client()
        self._redis_stream = get_redis_stream_client()
        self._repo = get_entity_event_repository()
        self._config = config

    async def find_entity_events(self, cleaned_id: UUID) -> list[EntityEvent]:
        """
        Store and announce one EntityEvent per verified listed company the article mentions,
        and return the stored ones. Returns an empty list if it names none, if SADI has no
        article with this id, or if the article's EntityEvents were already stored, in which
        case they are announced again.
        Raises the analysis' own LLMError subclass (e.g. LLMRateLimitError) if it failed,
        ServiceUnavailableError or ParseError if the article could not be fetched, or
        ServiceUnavailableError or DatabaseError if the EntityEvents could not be read or
        stored.
        """
        article = await self._news_client.fetch_cleaned_news(cleaned_id)
        if article is None:
            logger.warning("cleaned_news_not_found", cleaned_id=str(cleaned_id))
            return []

        existing_ids = await self._repo.fetch_entity_event_ids_by_source_url(article.source_url)
        if existing_ids:
            republished_ids = [str(entity_event_id) for entity_event_id in existing_ids]
            await self._publish(republished_ids)
            logger.info(
                "entity_analysis_republished",
                cleaned_id=str(cleaned_id),
                source_url=article.source_url,
                entity_event_ids=republished_ids,
            )
            return []

        result = await EntityAnalysisSkill(
            EntityAnalysisInput(article.title, article.body), self._config
        ).run()
        skill_log = result.skill_log
        if result.failure_reason is not None:
            logger.info(
                "entity_analysis_completed",
                **dataclasses.asdict(skill_log),
                cleaned_id=str(cleaned_id),
                source_url=article.source_url,
                entity_event_ids=[],
            )
            raise result.failure_reason

        persisted = await self._repo.persist_entities(
            [
                EntityEvent(
                    source_id=article.cleaned_id,
                    source_url=article.source_url,
                    source_name=article.source_name,
                    published_at=article.published_at,
                    analysis_outcome=VersionedMetadata(skill_log.skill_version, entity.outcome),
                    stock=entity.stock,
                )
                for entity in result.entity_events or []
            ]
        )
        entity_event_ids = [str(entity.entity_event_id) for entity in persisted]
        await self._publish(entity_event_ids)
        logger.info(
            "entity_analysis_completed",
            **dataclasses.asdict(skill_log),
            cleaned_id=str(cleaned_id),
            source_url=article.source_url,
            entity_event_ids=entity_event_ids,
        )
        return persisted

    async def _publish(self, entity_event_ids: list[str]) -> None:
        """Announce each EntityEvent on stream:entity_event_completed."""
        for entity_event_id in entity_event_ids:
            await self._redis_stream.publish(
                STREAM_ENTITY_EVENT_COMPLETED, {"entity_event_id": entity_event_id, "v": "1"}
            )


_entity_analysis_service: EntityAnalysisService | None = None


def get_entity_analysis_service() -> EntityAnalysisService:
    """
    Return the process's one EntityAnalysisService instance.
    Raises ServiceUnavailableError if called before create_entity_analysis_service() has run.
    """
    if _entity_analysis_service is None:
        raise ServiceUnavailableError(
            "entity analysis service is not initialized", category=ErrorCategory.ENTITY_ANALYSIS
        )
    return _entity_analysis_service


def create_entity_analysis_service(config: EntityAnalysisConfig) -> EntityAnalysisService:
    """Create the EntityAnalysisService and register it as this module's singleton."""
    global _entity_analysis_service
    _entity_analysis_service = EntityAnalysisService(config)
    return _entity_analysis_service
