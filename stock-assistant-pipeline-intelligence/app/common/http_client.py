import functools
from collections.abc import Awaitable, Callable

import httpx
from pydantic import ValidationError

from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """
    Return the process's one shared httpx.AsyncClient for outbound REST calls.
    Raises ServiceUnavailableError if called before create_http_client() has run.
    """
    if _http_client is None:
        raise ServiceUnavailableError(
            "http client is not initialized", category=ErrorCategory.COMMON
        )
    return _http_client


def create_http_client(timeout_s: int) -> httpx.AsyncClient:
    """Create the shared httpx.AsyncClient with timeout_s and register it as this module's
    singleton. Closed by the lifespan via its aclose()."""
    global _http_client
    _http_client = httpx.AsyncClient(timeout=timeout_s)
    return _http_client


def translate_client_errors[**P, R](
    *, upstream: str, category: ErrorCategory
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate an async client method that calls the upstream service named upstream, so its
    failures are raised as SAPI errors: an httpx.HTTPError (a network failure, a timeout, an
    error status) as an unavailable-upstream ServiceUnavailableError, and a pydantic
    ValidationError (a response without the expected shape) as a ParseError."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
            except httpx.HTTPError as exc:
                raise ServiceUnavailableError.upstream_unavailable(
                    f"{upstream} request failed: {exc!r}", category=category
                ) from exc
            except ValidationError as exc:
                raise ParseError(
                    f"{upstream} response shape mismatch: {exc!r}", category=category
                ) from exc

        return wrapper

    return decorator
