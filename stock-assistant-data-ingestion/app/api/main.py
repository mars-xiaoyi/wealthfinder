import logging

import redis.exceptions
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes.cleaned_news import router as cleaned_news_router
from app.api.routes.crawl import router as crawl_router
from app.api.routes.health import router as health_router
from app.common.error_codes import CommonErrorCode
from app.common.exceptions import (
    NotFoundException,
    SADIException,
    ServiceUnavailableException,
)

logger = logging.getLogger(__name__)


def _format_validation_errors(exc: RequestValidationError) -> list[dict]:
    """
    Map Pydantic's error list to the {field, issue} shape docs/api.md §1.4
    specifies for detail.errors.
    """
    _LOCATION_MARKERS = {"body", "query", "path", "header", "cookie"}
    errors = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in _LOCATION_MARKERS]
        errors.append({
            "field": ".".join(loc),
            "issue": err.get("msg", ""),
        })
    return errors


def create_app() -> FastAPI:
    """Create and return the configured FastAPI application with all routers and exception handlers."""
    app = FastAPI(title="SADI", version="1.0.0")

    app.include_router(crawl_router, prefix="/v1")
    app.include_router(health_router, prefix="/v1")
    app.include_router(cleaned_news_router, prefix="/v1")

    _HTTP_STATUS_MAP: dict[type[SADIException], int] = {
        NotFoundException: 404,
        ServiceUnavailableException: 503,
    }

    @app.exception_handler(SADIException)
    async def sadi_exception_handler(
        request: Request, exc: SADIException
    ) -> JSONResponse:
        status = _HTTP_STATUS_MAP.get(type(exc), 500)
        logger.warning("[api] SADIException: %s %s", exc.error_code.error_code, exc.detail)
        # exc.detail is always a safe, human-readable message set at the raise
        # site (e.g. "cleaned_id X not found", "Database is unavailable") —
        # SADIException subclasses must never pass a raw exception string or
        # anything else that could leak internal details (hostnames, ports,
        # stack fragments). See app/db/connection.py's ServiceUnavailableException
        # raises for the pattern to follow.
        return JSONResponse(
            status_code=status,
            content={
                "error_code": exc.error_code.error_code,
                "message": exc.error_code.message,
                "detail": exc.detail if exc.detail else {},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("[api] validation error: %s", exc.errors())
        # FastAPI routes a malformed-JSON body through this same exception type,
        # tagged with type "json_invalid" (see the note below) — docs/api.md §1.3
        # treats that as a distinct error (COMMON-4000), not a field validation
        # failure (COMMON-4001), so detect and split it here rather than
        # collapsing both into one code.
        if any(err.get("type") == "json_invalid" for err in exc.errors()):
            return JSONResponse(
                status_code=400,
                content={
                    "error_code": CommonErrorCode.MALFORMED_REQUEST.error_code,
                    "message": CommonErrorCode.MALFORMED_REQUEST.message,
                    "detail": CommonErrorCode.MALFORMED_REQUEST.dev_message,
                },
            )
        return JSONResponse(
            status_code=400,
            content={
                "error_code": CommonErrorCode.VALIDATION_FAILED.error_code,
                "message": CommonErrorCode.VALIDATION_FAILED.message,
                "detail": {"errors": _format_validation_errors(exc)},
            },
        )

    # Note: FastAPI catches json.JSONDecodeError internally while parsing the
    # request body and re-raises it as RequestValidationError before it ever
    # reaches the ASGI exception-handling layer (see fastapi/routing.py) — a
    # dedicated handler here would be unreachable dead code. Malformed JSON is
    # handled by validation_error_handler above, same as any other Pydantic
    # validation failure. See docs/spikes.md §3.3.

    # Note: a DB-unreachable condition surfaces as ServiceUnavailableException
    # (raised inside DatabaseClient) and is handled by sadi_exception_handler
    # above via _HTTP_STATUS_MAP — no dedicated handler needed here.

    @app.exception_handler(redis.exceptions.ConnectionError)
    async def redis_connection_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("[api] redis connection error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "error_code": CommonErrorCode.SERVICE_UNAVAILABLE.error_code,
                "message": CommonErrorCode.SERVICE_UNAVAILABLE.message,
                "detail": "Redis is unavailable",
            },
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("[api] unhandled exception: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": CommonErrorCode.INTERNAL_ERROR.error_code,
                "message": CommonErrorCode.INTERNAL_ERROR.message,
                # Generic on purpose — unlike ServiceUnavailableException/Redis's
                # handler, we don't know what actually failed here, and echoing
                # str(exc) risks leaking internal details. See server logs (above).
                "detail": "An unexpected internal error occurred",
            },
        )
        
    return app
