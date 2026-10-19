import logging
import sys

import structlog


def configure_logging() -> None:
    """Configure structlog to write one JSON object per line to stdout. Call once at startup."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
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
