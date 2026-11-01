from typing import Final


class RawNewsCleanedStream:
    """The stream of cleaned articles, read by the Entity Analysis layer."""

    NAME: Final = "stream:raw_news_cleaned"
    VERSION: Final = "1"
    CONSUMER_GROUP: Final = "sapi-entity-analysis"


class EntityEventCompletedStream:
    """The stream of persisted entity events."""

    NAME: Final = "stream:entity_event_completed"
    VERSION: Final = "1"


class EntityAnalysisDeadLetterStream:
    """The stream of messages the Entity Analysis layer could not process."""

    NAME: Final = "stream:entity_analysis_dead_letter"
    VERSION: Final = "1"
