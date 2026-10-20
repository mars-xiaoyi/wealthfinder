import httpx

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError

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
