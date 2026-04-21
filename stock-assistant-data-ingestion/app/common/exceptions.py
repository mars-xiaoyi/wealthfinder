from app.common.error_codes import CommonErrorCode, ErrorCode


class SADIException(Exception):
    """Base exception for all SADI custom exceptions."""

    error_code: ErrorCode
    detail: str

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(self.error_code.message if detail == "" else detail)


class NotFoundException(SADIException):
    """Resource not found."""
    error_code = CommonErrorCode.NOT_FOUND


class ServiceUnavailableException(SADIException):
    """Dependency unreachable (DB or Redis)."""
    error_code = CommonErrorCode.SERVICE_UNAVAILABLE
