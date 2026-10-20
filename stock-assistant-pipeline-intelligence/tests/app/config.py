import os

import pytest

from app import config as config_module
from app.common.errors import SAPIError, ServiceUnavailableError

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://postgres:password@localhost:5432/sapi",
    "REDIS_STREAM_URL": "redis://localhost:6379/0",
    "REDIS_STATE_URL": "redis://localhost:6379/1",
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


def load_config_overrides(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "25")
    monkeypatch.setenv("DB_CLOSE_TIMEOUT_S", "3")
    monkeypatch.setenv("HTTP_TIMEOUT_S", "30")
    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_S", "5")

    config = config_module.load_config()

    assert config.db.pool_size == 25
    assert config.db.close_timeout_s == 3
    assert config.http.timeout_s == 30
    assert config.health.check_timeout_s == 5


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
