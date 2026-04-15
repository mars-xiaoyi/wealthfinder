import asyncio
import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser

logger = logging.getLogger(__name__)


class FeedFetchError(Exception):
    """Raised when feedparser fails or returns an empty/invalid feed."""


@dataclass
class FeedEntry:
    title: str
    url: str
    published_at: Optional[datetime]  # UTC; None if not in feed


async def fetch_rss(url: str) -> list[FeedEntry]:
    """
    Fetch and parse an RSS feed. Returns a list of structured entries.
    Uses feedparser — synchronous library wrapped in asyncio.to_thread().

    Raises FeedFetchError if feedparser fails or returns an empty/invalid feed.
    """
    logger.info("[fetch_rss] Fetching RSS feed: %s", url)
    feed = await asyncio.to_thread(feedparser.parse, url)

    if feed.bozo and not feed.entries:
        raise FeedFetchError(
            f"Failed to parse RSS feed at {url}: {feed.bozo_exception}"
        )

    if not feed.entries:
        raise FeedFetchError(f"RSS feed at {url} returned no entries")

    logger.info("[fetch_rss] Feed returned %d raw entries from %s", len(feed.entries), url)

    entries: list[FeedEntry] = []
    skipped = 0
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            skipped += 1
            logger.warning("[fetch_rss] Skipping entry with missing title or link: %s", entry)
            continue

        published_at: Optional[datetime] = None
        if entry.get("published_parsed") is not None:
            timestamp = calendar.timegm(entry.published_parsed)
            published_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        logger.debug("[fetch_rss] Parsed entry: title=%s, url=%s, published_at=%s", title, link, published_at)
        entries.append(FeedEntry(title=title, url=link, published_at=published_at))

    logger.info("[fetch_rss] Completed: %d entries parsed, %d skipped from %s", len(entries), skipped, url)
    return entries
