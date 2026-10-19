from app.common import error_codes
from app.common.errors import ErrorType, SAPIError


class DatabaseError(SAPIError):
    """Raised on a database failure that is not a connection failure."""

    error_type = ErrorType.STORAGE
    error_code = error_codes.INTERNAL_ERROR
