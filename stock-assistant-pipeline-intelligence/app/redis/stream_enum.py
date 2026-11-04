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
    CONSUMER_GROUP: Final = "sapi-aggregation"


class EventAggregatedStream:
    """The stream of persisted Events whose merge state changed."""

    NAME: Final = "stream:event_aggregated"
    VERSION: Final = "1"
    CONSUMER_GROUP: Final = "sapi-scoring"


class EventScoredStream:
    """The stream of scored Events."""

    NAME: Final = "stream:event_scored"
    VERSION: Final = "1"


class EntityAnalysisDeadLetterStream:
    """The stream of messages the Entity Analysis layer could not process."""

    NAME: Final = "stream:entity_analysis_dead_letter"


class AggregationDeadLetterStream:
    """The stream of messages the Aggregation layer could not process."""

    NAME: Final = "stream:aggregation_dead_letter"
