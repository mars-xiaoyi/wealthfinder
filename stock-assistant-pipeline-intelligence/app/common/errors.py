import logging
from enum import StrEnum

from app.common import error_codes


class ErrorType(StrEnum):
    """Failure category of an error."""

    REQUEST = "REQUEST"
    INTERNAL = "INTERNAL"
    STORAGE = "STORAGE"
    NETWORK = "NETWORK"
    PARSE = "PARSE"


class SAPIError(Exception):
    """Base for every error SAPI raises. Each class sets its own defaults for every field except
    dev_message; a raise site gives the dev_message and may override any default for that
    occurrence.

    error_code is one of app/common/error_codes.py's constants; message is the short public
    summary; dev_message is the technical description of this occurrence.
    """

    error_type: ErrorType = ErrorType.INTERNAL
    error_code: str = error_codes.INTERNAL_ERROR
    message: str = "Internal server error"
    log_level: int = logging.ERROR

    def __init__(
        self,
        dev_message: str,
        *,
        error_type: ErrorType | None = None,
        error_code: str | None = None,
        message: str | None = None,
        log_level: int | None = None,
    ) -> None:
        super().__init__(dev_message)
        self.dev_message = dev_message
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
    get_X() accessor called before its create_X() ran."""

    error_code = error_codes.SERVICE_UNAVAILABLE
    message = "Service unavailable"
