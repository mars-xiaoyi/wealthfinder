import logging
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger

from app.common.errors import SAPIError


def add_sapi_error_fields(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: when a record's exc_info is a SAPIError, add its category and
    error_code to the record. exc_info may be True (the exception being handled), the exception
    itself, or a sys.exc_info() tuple."""
    exc_info = event_dict.get("exc_info")
    if exc_info is True:
        exc = sys.exc_info()[1]
    elif isinstance(exc_info, tuple):
        exc = exc_info[1]
    else:
        exc = exc_info
    if isinstance(exc, SAPIError):
        event_dict["category"] = exc.category
        event_dict["error_code"] = exc.error_code
    return event_dict


def configure_logging() -> None:
    """Configure structlog to write one JSON object per line to stdout. Call once at startup."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_sapi_error_fields,  # before dict_tracebacks, which replaces exc_info
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a logger whose records carry `module=<name>`; pass the caller's __name__."""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(module=name)
    return logger
