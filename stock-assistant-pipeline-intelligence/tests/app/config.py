import os

import pytest

from app import config as config_module
from app.common.errors import SAPIError, ServiceUnavailableError

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://postgres:password@localhost:5432/sapi",
    "REDIS_STREAM_URL": "redis://localhost:6379/0",
    "REDIS_STATE_URL": "redis://localhost:6379/1",
    "VERTEX_AI_PROJECT": "wealthfinder",
}


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Start from an environment holding only REQUIRED_ENV, so no variable from the shell
    running the tests can leak in."""
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config_module, "_config", None)


def load_config_defaults():
    config = config_module.load_config()

    assert config.db.url == REQUIRED_ENV["DATABASE_URL"]
    assert config.db.pool_size == 10
    assert config.db.close_timeout_s == 10
    assert config.http.timeout_s == 120
    assert config.health.check_timeout_s == 2
    assert config.redis.stream_url == REQUIRED_ENV["REDIS_STREAM_URL"]
    assert config.redis.state_url == REQUIRED_ENV["REDIS_STATE_URL"]
    assert config.redis.reconnect_interval_s == 5
    assert config.llm.vertex_project == REQUIRED_ENV["VERTEX_AI_PROJECT"]
    assert config.llm.vertex_location == "global"
    assert config.llm.api_timeout_s == 30
    assert config.entity_analysis.batch_size == 10
    assert config.entity_analysis.batch_timeout_ms == 5000
    assert config.entity_analysis.max_input_chars == 30000
    assert config.entity_analysis.sadi_api_url == "http://localhost:8000"
    assert config.entity_analysis.consumer_name == "sapi-entity-analysis-1"


def load_config_overrides(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "25")
    monkeypatch.setenv("DB_CLOSE_TIMEOUT_S", "3")
    monkeypatch.setenv("HTTP_TIMEOUT_S", "30")
    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_S", "5")
    monkeypatch.setenv("REDIS_RECONNECT_INTERVAL_S", "1")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "us-central1")
    monkeypatch.setenv("LLM_API_TIMEOUT_S", "10")
    monkeypatch.setenv("ENTITY_ANALYSIS_MAX_INPUT_CHARS", "500")
    monkeypatch.setenv("ENTITY_ANALYSIS_BATCH_SIZE", "5")
    monkeypatch.setenv("ENTITY_ANALYSIS_BATCH_TIMEOUT_MS", "200")
    monkeypatch.setenv("SADI_API_URL", "http://sadi:8000")
    monkeypatch.setenv("HOSTNAME", "sapi-pod-7")

    config = config_module.load_config()

    assert config.db.pool_size == 25
    assert config.db.close_timeout_s == 3
    assert config.http.timeout_s == 30
    assert config.health.check_timeout_s == 5
    assert config.redis.reconnect_interval_s == 1
    assert config.llm.vertex_location == "us-central1"
    assert config.llm.api_timeout_s == 10
    assert config.entity_analysis.max_input_chars == 500
    assert config.entity_analysis.batch_size == 5
    assert config.entity_analysis.batch_timeout_ms == 200
    assert config.entity_analysis.sadi_api_url == "http://sadi:8000"
    assert config.entity_analysis.consumer_name == "sapi-pod-7"


@pytest.mark.parametrize("missing", list(REQUIRED_ENV))
def load_config_missing_required_var(monkeypatch, missing):
    monkeypatch.delenv(missing)

    with pytest.raises(SAPIError, match=missing):
        config_module.load_config()


def get_config_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        config_module.get_config()


def get_config():
    loaded = config_module.load_config()

    assert config_module.get_config() is loaded
