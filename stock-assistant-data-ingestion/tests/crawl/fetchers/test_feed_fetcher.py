import time
from unittest.mock import MagicMock, patch

import pytest

from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchError, fetch_rss


def _make_feed(entries=None, bozo=False, bozo_exception=None):
    """Build a mock feedparser result."""
    feed = MagicMock()
    feed.bozo = bozo
    feed.bozo_exception = bozo_exception
    feed.entries = entries or []
    return feed


def _make_entry(title="Test Title", link="https://example.com/1", published_parsed=None):
    data = {
        "title": title,
        "link": link,
        "published_parsed": published_parsed,
    }
    entry = MagicMock()
    entry.get = lambda key, default="": data.get(key, default)
    entry.published_parsed = published_parsed
    return entry


# ---------------------------------------------------------------------------
# fetch_rss — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_rss_returns_entries():
    # 2026-01-15 12:00:00 UTC
    struct_time = time.strptime("2026-01-15 12:00:00", "%Y-%m-%d %H:%M:%S")
    entries = [_make_entry(published_parsed=struct_time)]
    feed = _make_feed(entries=entries)

    with patch("app.crawl.fetchers.feed_fetcher.feedparser.parse", return_value=feed):
        result = await fetch_rss("https://example.com/rss")

    assert len(result) == 1
    assert isinstance(result[0], FeedEntry)
    assert result[0].title == "Test Title"
    assert result[0].url == "https://example.com/1"
    assert result[0].published_at is not None
    assert result[0].published_at.year == 2026


@pytest.mark.asyncio
async def test_fetch_rss_no_published_at():
    entries = [_make_entry(published_parsed=None)]
    feed = _make_feed(entries=entries)

    with patch("app.crawl.fetchers.feed_fetcher.feedparser.parse", return_value=feed):
        result = await fetch_rss("https://example.com/rss")

    assert len(result) == 1
    assert result[0].published_at is None


# ---------------------------------------------------------------------------
# fetch_rss — error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_rss_bozo_no_entries_raises():
    feed = _make_feed(entries=[], bozo=True, bozo_exception=Exception("bad xml"))

    with patch("app.crawl.fetchers.feed_fetcher.feedparser.parse", return_value=feed):
        with pytest.raises(FeedFetchError, match="Failed to parse"):
            await fetch_rss("https://example.com/rss")


@pytest.mark.asyncio
async def test_fetch_rss_empty_entries_raises():
    feed = _make_feed(entries=[])

    with patch("app.crawl.fetchers.feed_fetcher.feedparser.parse", return_value=feed):
        with pytest.raises(FeedFetchError, match="returned no entries"):
            await fetch_rss("https://example.com/rss")


# ---------------------------------------------------------------------------
# fetch_rss — skips entries with missing title/link
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_rss_skips_entry_with_empty_title():
    entries = [
        _make_entry(title="", link="https://example.com/1"),
        _make_entry(title="Valid", link="https://example.com/2"),
    ]
    feed = _make_feed(entries=entries)

    with patch("app.crawl.fetchers.feed_fetcher.feedparser.parse", return_value=feed):
        result = await fetch_rss("https://example.com/rss")

    assert len(result) == 1
    assert result[0].title == "Valid"
