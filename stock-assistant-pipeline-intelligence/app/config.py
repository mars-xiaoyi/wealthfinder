import os
from dataclasses import dataclass

from app.common.error_categories import ErrorCategory
from app.common.errors import SAPIError, ServiceUnavailableError


@dataclass
class DatabaseConfig:
    url: str  # env: DATABASE_URL
    pool_size: int  # env: DB_POOL_SIZE (default 10)
    close_timeout_s: int  # env: DB_CLOSE_TIMEOUT_S (default 10)
    max_retry: int  # env: DB_WRITE_MAX_RETRY (default 3)
    retry_base_wait_ms: int  # env: DB_RETRY_BASE_WAIT_MS (default 100)


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
    reconnect_interval_s: int  # env: REDIS_RECONNECT_INTERVAL_S (default 5)
    stream_claim_timeout_ms: int  # env: STREAM_CLAIM_TIMEOUT_MS (default 300000)


@dataclass
class LLMConfig:
    vertex_project: str  # env: VERTEX_AI_PROJECT
    vertex_location: str  # env: VERTEX_AI_LOCATION (default "global")
    api_timeout_s: int  # env: LLM_API_TIMEOUT_S (default 30)


@dataclass
class EntityAnalysisConfig:
    batch_size: int  # env: ENTITY_ANALYSIS_BATCH_SIZE (default 10)
    batch_timeout_ms: int  # env: ENTITY_ANALYSIS_BATCH_TIMEOUT_MS (default 5000)
    max_retry: int  # env: ENTITY_ANALYSIS_MAX_RETRY (default 3)
    max_input_chars: int  # env: ENTITY_ANALYSIS_MAX_INPUT_CHARS (default 30000), article body cap
    sadi_api_url: str  # env: SADI_API_URL (default http://localhost:8000), without "/v1"
    consumer_name: str  # env: HOSTNAME (default sapi-entity-analysis-1), unique per pod


@dataclass
class AppConfig:
    db: DatabaseConfig
    http: HttpConfig
    health: HealthConfig
    redis: RedisConfig
    llm: LLMConfig
    entity_analysis: EntityAnalysisConfig


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
            max_retry=int(os.environ.get("DB_WRITE_MAX_RETRY", "3")),
            retry_base_wait_ms=int(os.environ.get("DB_RETRY_BASE_WAIT_MS", "100")),
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
            reconnect_interval_s=int(os.environ.get("REDIS_RECONNECT_INTERVAL_S", "5")),
            stream_claim_timeout_ms=int(os.environ.get("STREAM_CLAIM_TIMEOUT_MS", "300000")),
        ),
        llm=LLMConfig(
            vertex_project=_require("VERTEX_AI_PROJECT"),
            vertex_location=os.environ.get("VERTEX_AI_LOCATION", "global"),
            api_timeout_s=int(os.environ.get("LLM_API_TIMEOUT_S", "30")),
        ),
        entity_analysis=EntityAnalysisConfig(
            batch_size=int(os.environ.get("ENTITY_ANALYSIS_BATCH_SIZE", "10")),
            batch_timeout_ms=int(os.environ.get("ENTITY_ANALYSIS_BATCH_TIMEOUT_MS", "5000")),
            max_retry=int(os.environ.get("ENTITY_ANALYSIS_MAX_RETRY", "3")),
            max_input_chars=int(os.environ.get("ENTITY_ANALYSIS_MAX_INPUT_CHARS", "30000")),
            sadi_api_url=os.environ.get("SADI_API_URL", "http://localhost:8000"),
            consumer_name=os.environ.get("HOSTNAME", "sapi-entity-analysis-1"),
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
