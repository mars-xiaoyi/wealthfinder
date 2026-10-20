import os
from dataclasses import dataclass

from app.common.error_categories import ErrorCategory
from app.common.errors import SAPIError, ServiceUnavailableError


@dataclass
class DatabaseConfig:
    url: str  # env: DATABASE_URL
    pool_size: int  # env: DB_POOL_SIZE (default 10)
    close_timeout_s: int  # env: DB_CLOSE_TIMEOUT_S (default 10)


@dataclass
class HttpConfig:
    """Timeout for every outbound REST call through the shared httpx.AsyncClient. Separate
    from the LLM adapter's own timeout: that adapter uses the google-genai SDK, not httpx."""

    timeout_s: int  # env: HTTP_TIMEOUT_S (default 120)


@dataclass
class HealthConfig:
    """Per-check bound for GET /v1/health's DB/Redis checks. Keep it below the orchestrator
    readiness probe's own timeout, so SAPI's 503 arrives before the probe gives up."""

    check_timeout_s: int  # env: HEALTH_CHECK_TIMEOUT_S (default 2)


@dataclass
class RedisConfig:
    stream_url: str  # env: REDIS_STREAM_URL
    state_url: str  # env: REDIS_STATE_URL


@dataclass
class AppConfig:
    db: DatabaseConfig
    http: HttpConfig
    health: HealthConfig
    redis: RedisConfig


_config: AppConfig | None = None


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise SAPIError(
            f"Required environment variable '{name}' is not set.", category=ErrorCategory.CONFIG
        )
    return value


def load_config() -> AppConfig:
    """
    Read environment variables, construct the AppConfig and store it as the module singleton.
    Called once at service startup in app/main.py lifespan.
    Raises SAPIError if a required env var is missing.
    """
    global _config
    _config = AppConfig(
        db=DatabaseConfig(
            url=_require("DATABASE_URL"),
            pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
            close_timeout_s=int(os.environ.get("DB_CLOSE_TIMEOUT_S", "10")),
        ),
        http=HttpConfig(
            timeout_s=int(os.environ.get("HTTP_TIMEOUT_S", "120")),
        ),
        health=HealthConfig(
            check_timeout_s=int(os.environ.get("HEALTH_CHECK_TIMEOUT_S", "2")),
        ),
        redis=RedisConfig(
            stream_url=_require("REDIS_STREAM_URL"),
            state_url=_require("REDIS_STATE_URL"),
        ),
    )
    return _config


def get_config() -> AppConfig:
    """
    Return the loaded AppConfig singleton.
    Raises ServiceUnavailableError if load_config() has not been called yet.
    """
    if _config is None:
        raise ServiceUnavailableError(
            "app config is not initialized", category=ErrorCategory.CONFIG
        )
    return _config
