import logging
from enum import StrEnum
from typing import Self

from app.common import error_codes
from app.common.error_categories import ErrorCategory


class ErrorType(StrEnum):
    """Failure category of an error."""

    REQUEST = "REQUEST"
    GENERAL = "GENERAL"
    STORAGE = "STORAGE"
    PARSE = "PARSE"


class SAPIError(Exception):
    """Base for every error SAPI raises. Each class sets its own defaults for every field except
    dev_message and category; a raise site gives those two and may override any default for
    that occurrence.

    error_code is one of app/common/error_codes.py's constants; message is the short public
    summary; dev_message is the technical description of this occurrence; category is the
    part of SAPI it was raised from.
    """

    error_type: ErrorType = ErrorType.GENERAL
    error_code: str = error_codes.INTERNAL_ERROR
    message: str = "Internal server error"
    log_level: int = logging.ERROR

    def __init__(
        self,
        dev_message: str,
        *,
        category: ErrorCategory,
        error_type: ErrorType | None = None,
        error_code: str | None = None,
        message: str | None = None,
        log_level: int | None = None,
    ) -> None:
        super().__init__(dev_message)
        self.dev_message = dev_message
        self.category = category
        if error_type is not None:
            self.error_type = error_type
        if error_code is not None:
            self.error_code = error_code
        if message is not None:
            self.message = message
        if log_level is not None:
            self.log_level = log_level


class ServiceUnavailableError(SAPIError):
    """Raised when a process-wide resource is not usable: an unreachable DB/Redis, or a
    get_X() accessor called before its create_X() ran. An unreachable upstream
    (SADI/Admin/HKEXnews) is built with upstream_unavailable()."""

    error_code = error_codes.SERVICE_UNAVAILABLE
    message = "Service unavailable"

    @classmethod
    def upstream_unavailable(cls, dev_message: str, *, category: ErrorCategory) -> Self:
        """An unreachable upstream service: UPSTREAM_UNAVAILABLE, logged at WARNING since an
        outside outage needs no code or config change."""
        return cls(
            dev_message,
            category=category,
            error_code=error_codes.UPSTREAM_UNAVAILABLE,
            log_level=logging.WARNING,
        )


class ParseError(SAPIError):
    """Raised when structured or unstructured content could not be parsed: a code or schema
    issue, not an availability one."""

    error_type = ErrorType.PARSE
    error_code = error_codes.PARSE_ERROR
    message = "Content could not be parsed"
