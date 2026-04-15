import os
from dataclasses import dataclass
from typing import Optional

import yaml


@dataclass
class CrawlSourceConfig:
    max_concurrent: int
    request_interval_min_ms: int
    request_interval_max_ms: int


@dataclass
class CrawlConfig:
    max_retry: int
    retry_base_wait_ms: int
    request_timeout_s: int
    browser_navigation_timeout_ms: int
    crawl_sources: dict[str, CrawlSourceConfig]


@dataclass
class CleanConfig:
    worker_concurrency: int
    body_min_length: int
    stream_claim_timeout_ms: int


@dataclass
class DatabaseConfig:
    url: str
    pool_size: int
    max_retry: int
    retry_base_wait_ms: int


@dataclass
class AppConfig:
    db: DatabaseConfig
    redis_url: str
    crawl: CrawlConfig
    clean: CleanConfig


_config: Optional[AppConfig] = None


def load_config() -> AppConfig:
    """
    Read environment variables and config/sources.yaml; construct and store the AppConfig singleton.
    Called once at service startup in app/main.py lifespan.
    Raises ValueError if a required env var is missing.
    """
    global _config

    def _require(name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            raise ValueError(f"Required environment variable '{name}' is not set.")
        return value

    db_config = DatabaseConfig(
        url=_require("DATABASE_URL"),
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_retry=int(os.environ.get("DB_MAX_RETRY", "3")),
        retry_base_wait_ms=int(os.environ.get("DB_RETRY_BASE_WAIT_MS", "100")),
    )

    crawl_sources_yaml_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "crawl_sources.yaml"
    )
    with open(crawl_sources_yaml_path) as f:
        raw = yaml.safe_load(f)

    crawl_sources: dict[str, CrawlSourceConfig] = {
        name: CrawlSourceConfig(
            max_concurrent=values["max_concurrent"],
            request_interval_min_ms=values["request_interval_min_ms"],
            request_interval_max_ms=values["request_interval_max_ms"],
        )
        for name, values in raw["crawl_sources"].items()
    }

    crawl_config = CrawlConfig(
        max_retry=int(os.environ.get("CRAWL_MAX_RETRY", "3")),
        retry_base_wait_ms=int(os.environ.get("CRAWL_RETRY_BASE_WAIT_MS", "500")),
        request_timeout_s=int(os.environ.get("CRAWL_REQUEST_TIMEOUT_S", "10")),
        browser_navigation_timeout_ms=int(
            os.environ.get("CRAWL_BROWSER_NAV_TIMEOUT_MS", "30000")
        ),
        crawl_sources=crawl_sources,
    )

    clean_config = CleanConfig(
        worker_concurrency=int(os.environ.get("CLEAN_WORKER_CONCURRENCY", "5")),
        body_min_length=int(os.environ.get("CLEAN_BODY_MIN_LENGTH", "50")),
        stream_claim_timeout_ms=int(os.environ.get("STREAM_CLAIM_TIMEOUT_MS", "30000")),
    )

    _config = AppConfig(
        db=db_config,
        redis_url=_require("REDIS_URL"),
        crawl=crawl_config,
        clean=clean_config,
    )
    return _config


def get_config() -> AppConfig:
    """
    Return the loaded AppConfig singleton.
    Raises RuntimeError if load_config() has not been called yet.
    """
    if _config is None:
        raise RuntimeError(
            "AppConfig has not been initialised. Call load_config() at service startup."
        )
    return _config
