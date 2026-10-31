from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ErrorType, SAPIError


class DatabaseError(SAPIError):
    """Raised on a database failure that is not a connection failure."""

    error_type = ErrorType.STORAGE
    error_code = error_codes.DATABASE_ERROR


class UniqueConstraintError(DatabaseError):
    """Raised on a unique-constraint violation that the statement did not absorb itself."""

    error_code = error_codes.UNIQUE_CONSTRAINT_VIOLATED

    def __init__(self, constraint_name: str, *, category: ErrorCategory) -> None:
        super().__init__(f"unique constraint violated: {constraint_name}", category=category)
        self.constraint_name = constraint_name
