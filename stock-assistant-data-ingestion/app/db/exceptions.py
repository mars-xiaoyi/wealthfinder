"""
Driver-agnostic exceptions raised across the DatabaseClient boundary.

asyncpg is an implementation detail of app/db/connection.py. Callers outside
that module (service layer, API layer) must never need to import asyncpg or
reference its exception types directly — DatabaseClient translates every
asyncpg error into one of these before it crosses its boundary. A "database
unreachable" condition is raised as app.common.exceptions.ServiceUnavailableException
instead of a type defined here, since that exception already carries the
COMMON-5001 error code / 503 mapping used by the API layer.
"""


class DatabaseError(Exception):
    """Base class for all driver-agnostic errors raised by DatabaseClient."""


class UniqueConstraintError(DatabaseError):
    """A write violated a unique constraint (e.g. a duplicate insert)."""

    def __init__(self, constraint_name: str | None = None, *args):
        self.constraint_name = constraint_name
        super().__init__(*args)
