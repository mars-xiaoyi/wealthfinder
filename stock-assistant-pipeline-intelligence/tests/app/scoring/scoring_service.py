from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.common.errors import ServiceUnavailableError
from app.config import ScoringConfig
from app.models.event import Event
from app.persistent import entity_event_repository as entity_event_repository_module
from app.persistent import event_repository as event_repository_module
from app.persistent.entity_event_repository import EntityEventRepository
from app.persistent.event_repository import EventRepository
from app.scoring import scoring_service as scoring_service_module
from app.scoring.scoring_service import ScoringService

CONFIG = ScoringConfig(batch_size=5, batch_timeout_ms=200, consumer_name="pod-1")
EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(scoring_service_module, "_scoring_service", None)


@pytest.fixture(autouse=True)
def event_repository(monkeypatch):
    """An EventRepository holding the Event, unless a test says otherwise."""
    repo = AsyncMock(spec=EventRepository)
    repo.fetch_event.return_value = MagicMock(spec=Event)
    monkeypatch.setattr(event_repository_module, "_event_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def entity_event_repository(monkeypatch):
    repo = AsyncMock(spec=EntityEventRepository)
    repo.fetch_entity_events_for_event.return_value = []
    monkeypatch.setattr(entity_event_repository_module, "_entity_event_repository", repo)
    return repo


async def score_event_success(event_repository, entity_event_repository):
    """The stored Event is loaded with the EntityEvents it was merged from."""
    await ScoringService(CONFIG).score_event(EVENT_ID)

    event_repository.fetch_event.assert_awaited_once_with(EVENT_ID)
    entity_event_repository.fetch_entity_events_for_event.assert_awaited_once_with(EVENT_ID)


async def score_event_missing(event_repository, entity_event_repository):
    """An Event no longer stored is skipped without loading anything else."""
    event_repository.fetch_event.return_value = None

    await ScoringService(CONFIG).score_event(EVENT_ID)

    event_repository.fetch_event.assert_awaited_once_with(EVENT_ID)
    entity_event_repository.fetch_entity_events_for_event.assert_not_awaited()


def get_scoring_service_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        scoring_service_module.get_scoring_service()


def create_scoring_service_success():
    service = scoring_service_module.create_scoring_service(CONFIG)

    assert scoring_service_module.get_scoring_service() is service
