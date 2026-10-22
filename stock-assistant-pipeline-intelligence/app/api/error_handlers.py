from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorResponse
from app.common.errors import ParseError, SAPIError, ServiceUnavailableError
from app.logger import get_logger

logger = get_logger(__name__)

# The HTTP status of each SAPIError class; any other SAPIError is a 500.
HTTP_STATUS: dict[type[SAPIError], int] = {
    ServiceUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    ParseError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def to_error_response(exc: SAPIError) -> ErrorResponse:
    """The response body for exc. dev_message stays inside SAPI, so detail is left empty."""
    return ErrorResponse(error_code=exc.error_code, message=exc.message)


async def sapi_error_handler(request: Request, exc: SAPIError) -> JSONResponse:
    """Log exc once, at its own log_level, and return it as an ErrorResponse with the status
    HTTP_STATUS gives its class or nearest listed base class, else 500."""
    logger.log(exc.log_level, exc.dev_message, path=request.url.path, exc_info=exc)
    status_code = next(
        (HTTP_STATUS[cls] for cls in type(exc).__mro__ if cls in HTTP_STATUS),
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    return JSONResponse(status_code=status_code, content=to_error_response(exc).model_dump())


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log an exception no other handler took, at ERROR, and return a 500 INTERNAL_ERROR
    ErrorResponse."""
    logger.error(f"Unhandled exception: {exc!r}", path=request.url.path, exc_info=exc)
    body = ErrorResponse(error_code=SAPIError.error_code, message=SAPIError.message)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump()
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register sapi_error_handler for every SAPIError, and unhandled_error_handler for any
    other exception."""
    app.exception_handler(SAPIError)(sapi_error_handler)
    app.exception_handler(Exception)(unhandled_error_handler)
