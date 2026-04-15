"""Live integration test for YahooHKCrawler."""

import pytest

from app.crawl.crawlers.yahoo_hk_crawler import YahooHKCrawler

from .conftest import assert_success_shape, summarize


@pytest.mark.live
@pytest.mark.asyncio
async def test_yahoo_hk_live_end_to_end(source_config, page_crawler, stub_db):
    crawler = YahooHKCrawler(
        source_config=source_config,
        page_crawler=page_crawler,
        db=stub_db,
    )

    result = await crawler.run()
    summarize("YAHOO_HK", result)

    assert len(result.successes) >= 1, "no Yahoo HK articles crawled"
    assert_success_shape(
        result.successes[0], host_contains="hk.finance.yahoo.com"
    )
