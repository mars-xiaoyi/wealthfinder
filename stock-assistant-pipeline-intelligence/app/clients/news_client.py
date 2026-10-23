from http import HTTPStatus
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.common.http_client import translate_client_errors


class CleanedNews(BaseModel):
    """SAPI's model of one cleaned article fetched from SADI."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    cleaned_id: UUID
    title: str = Field(validation_alias="title_cleaned")
    body: str = Field(validation_alias="body_cleaned")
    source_url: str
    source_name: str
    published_at: AwareDatetime | None  # null if the source has no publish time


class NewsClient:
    """Client for SADI's REST API."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http_client = http_client
        self._base_url = base_url

    @translate_client_errors(upstream="SADI", category=ErrorCategory.NEWS_CLIENT)
    async def fetch_cleaned_news(self, cleaned_id: UUID) -> CleanedNews | None:
        """
        Fetch one cleaned article from SADI. Returns None if SADI has no article with this id.
        Raises ServiceUnavailableError if SADI is unreachable or answers with any other error
        status, or ParseError if a successful response does not have the expected shape.
        """
        response = await self._http_client.get(f"{self._base_url}/v1/cleaned_news/{cleaned_id}")
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        response.raise_for_status()
        return CleanedNews.model_validate_json(response.content)


_news_client: NewsClient | None = None


def get_news_client() -> NewsClient:
    """
    Return the process's one NewsClient instance.
    Raises ServiceUnavailableError if called before create_news_client() has run.
    """
    if _news_client is None:
        raise ServiceUnavailableError(
            "news client is not initialized", category=ErrorCategory.NEWS_CLIENT
        )
    return _news_client


def create_news_client(http_client: httpx.AsyncClient, base_url: str) -> NewsClient:
    """Create the NewsClient on the shared http_client and register it as this module's
    singleton. base_url excludes "/v1"."""
    global _news_client
    _news_client = NewsClient(http_client, base_url)
    return _news_client
