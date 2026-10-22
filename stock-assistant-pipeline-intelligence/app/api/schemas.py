from enum import StrEnum

from pydantic import BaseModel


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class HKStockListStatus(StrEnum):
    OK = "ok"
    NOT_READY = "not_ready"


# ── GET /v1/health ────────────────────────────────────────────────────────────


class LayerStatus(BaseModel):
    entity_analysis: ComponentStatus
    aggregation: ComponentStatus
    scoring: ComponentStatus
    cache: ComponentStatus


class HealthResponse(BaseModel):
    status: HealthStatus
    database: ComponentStatus
    redis_stream: ComponentStatus
    redis_state: ComponentStatus
    hk_stock_list: HKStockListStatus
    layers: LayerStatus


# ── POST /v1/admin/hk-stock-list-sync ─────────────────────────────────────────


class HKStockListSyncResponse(BaseModel):
    status: str = "success"
    entries_loaded: int


# ── Error Response (used by all routes) ───────────────────────────────────────


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: str | dict[str, object] = {}
