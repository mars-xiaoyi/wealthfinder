from uuid import UUID

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import ScoringConfig
from app.logger import get_logger
from app.persistent.entity_event_repository import get_entity_event_repository
from app.persistent.event_repository import get_event_repository

logger = get_logger(__name__)


class ScoringService:
    """Scores the Events aggregated from EntityEvents."""

    def __init__(self, config: ScoringConfig) -> None:
        self._event_repo = get_event_repository()
        self._entity_event_repo = get_entity_event_repository()
        self._config = config

    async def score_event(self, event_id: UUID) -> None:
        """
        Load the stored Event with this id and the EntityEvents it was merged from. An Event
        no longer stored is skipped.
        Raises ServiceUnavailableError or DatabaseError.
        """
        event = await self._event_repo.fetch_event(event_id)
        if event is None:
            logger.warning("scoring_event_missing", event_id=str(event_id))
            return
        await self._entity_event_repo.fetch_entity_events_for_event(event_id)


_scoring_service: ScoringService | None = None


def get_scoring_service() -> ScoringService:
    """Return the process's one ScoringService instance."""
    if _scoring_service is None:
        raise ServiceUnavailableError(
            "scoring service is not initialized", category=ErrorCategory.SCORING
        )
    return _scoring_service


def create_scoring_service(config: ScoringConfig) -> ScoringService:
    """Create the ScoringService and register it as this module's singleton."""
    global _scoring_service
    _scoring_service = ScoringService(config)
    return _scoring_service
