import importlib
import os
import sys

import pytest


def reload_config_module():
    """Reload app.config to reset the module-level _config singleton."""
    if "app.config" in sys.modules:
        del sys.modules["app.config"]
    if "app" in sys.modules:
        del sys.modules["app"]
    import app.config  # noqa: F401
    return sys.modules["app.config"]


REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://postgres:password@localhost:5432/sadi",
    "REDIS_URL": "redis://localhost:6379",
}

OPTIONAL_ENV = {
    "DB_POOL_SIZE": "10",
    "DB_MAX_RETRY": "3",
    "DB_RETRY_BASE_WAIT_MS": "100",
    "CRAWL_MAX_RETRY": "3",
    "CRAWL_RETRY_BASE_WAIT_MS": "500",
    "CRAWL_REQUEST_TIMEOUT_S": "10",
    "CRAWL_BROWSER_NAV_TIMEOUT_MS": "30000",
    "CLEAN_WORKER_CONCURRENCY": "5",
    "CLEAN_BODY_MIN_LENGTH": "50",
    "STREAM_CLAIM_TIMEOUT_MS": "30000",
}


@pytest.fixture(autouse=True)
def isolate_env_and_module(monkeypatch):
    """Clear relevant env vars and reload the config module before each test."""
    for key in {**REQUIRED_ENV, **OPTIONAL_ENV}:
        monkeypatch.delenv(key, raising=False)
    yield
    # Force reload so the singleton does not bleed between tests
    if "app.config" in sys.modules:
        del sys.modules["app.config"]
    if "app" in sys.modules:
        del sys.modules["app"]


def _set_env(monkeypatch, extra=None):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    if extra:
        for key, value in extra.items():
            monkeypatch.setenv(key, value)


class TestLoadConfig:
    def test_happy_path_required_only(self, monkeypatch):
        _set_env(monkeypatch)
        mod = reload_config_module()
        config = mod.load_config()

        assert config.db.url == REQUIRED_ENV["DATABASE_URL"]
        assert config.redis_url == REQUIRED_ENV["REDIS_URL"]

        # Defaults applied correctly
        assert config.db.pool_size == 10
        assert config.db.max_retry == 3
        assert config.db.retry_base_wait_ms == 100
        assert config.crawl.max_retry == 3
        assert config.crawl.retry_base_wait_ms == 500
        assert config.crawl.request_timeout_s == 10
        assert config.crawl.browser_navigation_timeout_ms == 15000
        assert config.clean.worker_concurrency == 5
        assert config.clean.body_min_length == 50
        assert config.clean.stream_claim_timeout_ms == 30000

    def test_happy_path_all_env_vars(self, monkeypatch):
        _set_env(monkeypatch, extra=OPTIONAL_ENV)
        mod = reload_config_module()
        config = mod.load_config()

        assert config.db.pool_size == 10
        assert config.crawl.max_retry == 3

    def test_sources_yaml_loaded(self, monkeypatch):
        _set_env(monkeypatch)
        mod = reload_config_module()
        config = mod.load_config()

        assert set(config.crawl.crawl_sources.keys()) == {"HKEX", "MINGPAO", "AASTOCKS", "YAHOO_HK"}

        hkex = config.crawl.crawl_sources["HKEX"]
        assert hkex.max_concurrent == 5
        assert hkex.request_interval_min_ms == 500
        assert hkex.request_interval_max_ms == 1000

        for name in ("MINGPAO", "AASTOCKS", "YAHOO_HK"):
            src = config.crawl.crawl_sources[name]
            assert src.max_concurrent == 3
            assert src.request_interval_min_ms == 500
            assert src.request_interval_max_ms == 1000

    def test_missing_database_url_raises(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", REQUIRED_ENV["REDIS_URL"])
        mod = reload_config_module()
        with pytest.raises(ValueError, match="DATABASE_URL"):
            mod.load_config()

    def test_missing_redis_url_raises(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", REQUIRED_ENV["DATABASE_URL"])
        mod = reload_config_module()
        with pytest.raises(ValueError, match="REDIS_URL"):
            mod.load_config()

    def test_returns_app_config_instance(self, monkeypatch):
        _set_env(monkeypatch)
        mod = reload_config_module()
        from app.config import AppConfig
        config = mod.load_config()
        assert isinstance(config, AppConfig)


class TestGetConfig:
    def test_raises_before_load_config(self, monkeypatch):
        mod = reload_config_module()
        with pytest.raises(RuntimeError, match="load_config"):
            mod.get_config()

    def test_returns_same_instance_after_load(self, monkeypatch):
        _set_env(monkeypatch)
        mod = reload_config_module()
        config1 = mod.load_config()
        config2 = mod.get_config()
        assert config1 is config2

    def test_singleton_identity_on_repeated_calls(self, monkeypatch):
        _set_env(monkeypatch)
        mod = reload_config_module()
        mod.load_config()
        assert mod.get_config() is mod.get_config()
