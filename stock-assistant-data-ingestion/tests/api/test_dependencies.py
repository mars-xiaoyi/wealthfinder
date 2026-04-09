from types import SimpleNamespace
from unittest.mock import MagicMock

from app.api.dependencies import get_config, get_crawl_service, get_db, get_stream_client


def _make_request(**state_attrs):
    """Build a fake Request with app.state attributes."""
    state = SimpleNamespace(**state_attrs)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app)


class TestGetDb:
    def test_returns_db_from_state(self):
        mock_db = MagicMock()
        request = _make_request(db=mock_db)
        assert get_db(request) is mock_db


class TestGetStreamClient:
    def test_returns_stream_client_from_state(self):
        mock_sc = MagicMock()
        request = _make_request(stream_client=mock_sc)
        assert get_stream_client(request) is mock_sc


class TestGetCrawlService:
    def test_returns_crawl_service_from_state(self):
        mock_cs = MagicMock()
        request = _make_request(crawl_service=mock_cs)
        assert get_crawl_service(request) is mock_cs


class TestGetConfig:
    def test_returns_config_from_state(self):
        mock_cfg = MagicMock()
        request = _make_request(config=mock_cfg)
        assert get_config(request) is mock_cfg
