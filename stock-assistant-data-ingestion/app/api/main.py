import json
import logging

import asyncpg
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
        return JSONResponse(
            status_code=status,
            content={
                "error_code": exc.error_code.error_code,
                "message": exc.error_code.message,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("[api] validation error: %s", exc.errors())
        return JSONResponse(
            status_code=400,
            content={
                "error_code": CommonErrorCode.VALIDATION_FAILED.error_code,
                "message": CommonErrorCode.VALIDATION_FAILED.message,
            },
        )

    @app.exception_handler(json.JSONDecodeError)
    async def json_decode_error_handler(
        request: Request, exc: json.JSONDecodeError
    ) -> JSONResponse:
        logger.warning("[api] malformed JSON: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": CommonErrorCode.MALFORMED_REQUEST.error_code,
                "message": CommonErrorCode.MALFORMED_REQUEST.message,
            },
        )

    @app.exception_handler(asyncpg.PostgresConnectionError)
    @app.exception_handler(asyncpg.InterfaceError)
    async def db_connection_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("[api] database connection error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "error_code": CommonErrorCode.SERVICE_UNAVAILABLE.error_code,
                "message": CommonErrorCode.SERVICE_UNAVAILABLE.message,
            },
        )

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
            },
        )
        
    return app
