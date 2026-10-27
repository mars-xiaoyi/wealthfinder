from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ErrorType, SAPIError


class UniqueConstraintError(SAPIError):
    """Raised on a unique-constraint violation that the statement did not absorb itself."""

    error_type = ErrorType.STORAGE
    error_code = error_codes.INTERNAL_ERROR

    def __init__(self, constraint_name: str, *, category: ErrorCategory) -> None:
        super().__init__(f"unique constraint violated: {constraint_name}", category=category)
        self.constraint_name = constraint_name


class DatabaseError(SAPIError):
    """Raised on a database failure that is not a connection failure."""

    error_type = ErrorType.STORAGE
    error_code = error_codes.INTERNAL_ERROR
