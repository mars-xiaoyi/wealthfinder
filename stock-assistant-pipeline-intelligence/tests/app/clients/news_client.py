from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from app.clients import news_client as news_client_module
from app.clients.news_client import CleanedNews, NewsClient
from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError

BASE_URL = "http://sadi:8000"
CLEANED_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
BODY = {
    "cleaned_id": str(CLEANED_ID),
    "raw_id": "660e8400-e29b-41d4-a716-446655440001",
    "title_cleaned": "騰訊控股公佈第四季業績",
    "body_cleaned": "騰訊控股第四季廣告收入錄得強勁增長...",
    "published_at": "2025-01-15T00:00:00Z",
    "source_name": "MINGPAO",
    "source_url": "https://finance.mingpao.com/article",
    "created_at": "2025-01-15T00:05:00Z",
}


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(news_client_module, "_news_client", None)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> NewsClient:
    return NewsClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), BASE_URL)


async def fetch_cleaned_news():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=BODY)

    news = await _client(handler).fetch_cleaned_news(CLEANED_ID)

    assert news == CleanedNews(
        cleaned_id=CLEANED_ID,
        title=BODY["title_cleaned"],
        body=BODY["body_cleaned"],
        source_url=BODY["source_url"],
        source_name=BODY["source_name"],
        published_at=datetime(2025, 1, 15, tzinfo=UTC),
    )
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", f"{BASE_URL}/v1/cleaned_news/{CLEANED_ID}")
    ]


async def fetch_cleaned_news_no_published_at():
    body = {**BODY, "published_at": None}

    news = await _client(lambda _r: httpx.Response(200, json=body)).fetch_cleaned_news(CLEANED_ID)

    assert news == CleanedNews(
        cleaned_id=CLEANED_ID,
        title=BODY["title_cleaned"],
        body=BODY["body_cleaned"],
        source_url=BODY["source_url"],
        source_name=BODY["source_name"],
        published_at=None,
    )


async def fetch_cleaned_news_not_found():
    body = {"error_code": "COMMON-4004", "message": "Not found", "detail": {}}

    news = await _client(lambda _r: httpx.Response(404, json=body)).fetch_cleaned_news(CLEANED_ID)

    assert news is None


async def fetch_cleaned_news_error_status():
    """SADI's other error statuses are an unavailable upstream, not an article to parse."""
    body = {"error_code": "COMMON-5001", "message": "Service unavailable", "detail": {}}

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await _client(lambda _r: httpx.Response(503, json=body)).fetch_cleaned_news(CLEANED_ID)

    assert exc_info.value.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert exc_info.value.category is ErrorCategory.NEWS_CLIENT
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, text="<html>"), id="not_json"),
        pytest.param(httpx.Response(200, json={**BODY, "source_url": None}), id="null_field"),
        pytest.param(
            httpx.Response(200, json={**BODY, "published_at": "2025-01-15T00:00:00"}),
            id="published_at_without_offset",
        ),
    ],
)
async def fetch_cleaned_news_unparseable(response):
    with pytest.raises(ParseError) as exc_info:
        await _client(lambda _r: response).fetch_cleaned_news(CLEANED_ID)

    assert exc_info.value.error_code == error_codes.PARSE_ERROR
    assert exc_info.value.category is ErrorCategory.NEWS_CLIENT


def get_news_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        news_client_module.get_news_client()


def create_news_client():
    http_client = httpx.AsyncClient()

    client = news_client_module.create_news_client(http_client, BASE_URL)

    assert news_client_module.get_news_client() is client
